"""英语口语练习会话（S2S 多模态，与默认 Session 并行）。

流程：设备 Opus 上行 → PCM 16k → 百炼 Qwen-Omni Realtime（音频进、音频出）
     → PCM 24k → Opus 下行。不再走 ASR + 文本 LLM + TTS 三段式。

Omni 侧使用 server_vad 自动断句；服务端只负责协议桥接与小智设备消息时序。
"""
import asyncio
import base64
import json
import logging
import time
import uuid
from asyncio import QueueEmpty
from typing import Optional

from dashscope.audio.qwen_omni import (
    AudioFormat,
    MultiModality,
    OmniRealtimeCallback,
    OmniRealtimeConversation,
)

from config import config
from .silence_vad import (
    EmptyTurnTracker,
    SilenceGate,
    WatchdogTask,
    silence_watchdog_loop,
)
from .english_diagnosis import diagnose_turn
from .english_history import (
    format_history_context,
    get_history_store,
)
from .english_profile import (
    EnglishProfile,
    build_instructions,
    get_store,
    looks_like_preference_request,
    maybe_update_from_turn,
    normalize_spelling_hyphens,
    refine_profile,
)
from .opus_codec import OpusDecoder, OpusEncoder

logger = logging.getLogger("english")

# 给 MCU 屏幕 / Web 弹层看的中文提示；tts 字幕同步用，便于用户理解
ERROR_USER_ZH = "云端口语服务暂时异常，请稍后再试"
ERROR_SPEECH = ERROR_USER_ZH
ALERT_STATUS_CLOUD = "服务端异常"
MAX_ERROR_DETAIL_LEN = 200
# Web 按住说话：至少约 180ms 有效 PCM 才允许 commit（3 帧 × 60ms）
MIN_WEB_OMNI_PCM_BYTES = 5760
# Omni 要求图片 Base64 后不超过 256KB；留一点余量
MAX_IMAGE_B64_LEN = 250_000
# 送图前先喂一小段静音（API：必须先有至少一次音频）
IMAGE_SEED_SILENCE_BYTES = 6400  # 16kHz mono s16le ≈ 200ms

# 下行播放队列控制标记（与 Opus/PCM 帧字节区分）
_DL_END = object()
_DL_ABORT = object()


def _omni_response_id(event: dict) -> Optional[str]:
    """从 Omni Realtime 事件里尽量取出 response id。"""
    if not isinstance(event, dict):
        return None
    rid = event.get("response_id")
    if isinstance(rid, str) and rid:
        return rid
    resp = event.get("response")
    if isinstance(resp, dict):
        rid = resp.get("id")
        if isinstance(rid, str) and rid:
            return rid
    rid = event.get("id")
    if isinstance(rid, str) and rid.startswith("resp"):
        return rid
    return None


class _OmniBridge(OmniRealtimeCallback):
    def __init__(self, session: "EnglishSession"):
        self._session = session

    def on_open(self) -> None:
        logger.info("[english][%s] Omni 连接已打开", self._session.session_id)

    def on_close(self, close_status_code, close_msg) -> None:
        asyncio.run_coroutine_threadsafe(
            self._session._on_omni_closed(close_status_code, close_msg),
            self._session.loop,
        )

    def on_event(self, message) -> None:
        if isinstance(message, str):
            try:
                message = json.loads(message)
            except json.JSONDecodeError:
                return
        asyncio.run_coroutine_threadsafe(
            self._session._on_omni_event(message), self._session.loop
        )


class EnglishSession:
    def __init__(self, transport, loop: asyncio.AbstractEventLoop, device_id: str, client_id: str):
        self.transport = transport
        transport.session = self
        self.loop = loop
        self.device_id = device_id
        self.client_id = client_id
        self.session_id = uuid.uuid4().hex[:8]

        self.decoder = OpusDecoder(
            sample_rate=config.UPLINK_SAMPLE_RATE,
            channels=config.CHANNELS,
            frame_ms=config.FRAME_DURATION_MS,
        )
        self.encoder = OpusEncoder(
            sample_rate=config.DOWNLINK_SAMPLE_RATE,
            channels=config.CHANNELS,
            frame_ms=config.FRAME_DURATION_MS,
        )

        self._listening = False
        self._speaking = False
        self._cancel = False
        self._error_notified = False
        self._omni: Optional[OmniRealtimeConversation] = None
        self._audio_queue: asyncio.Queue = asyncio.Queue()  # 上行 Opus/PCM
        self._audio_task: Optional[asyncio.Task] = None
        # 下行：编码后入队，独立任务按时钟发送（MCU 需预缓冲，避免 Omni 突发抖动）
        self._downlink_q: asyncio.Queue = asyncio.Queue()
        self._downlink_task: Optional[asyncio.Task] = None
        self._downlink_idle = asyncio.Event()
        self._downlink_idle.set()
        self._active_response_id: Optional[str] = None
        self._uplink_reopen_task: Optional[asyncio.Task] = None
        self._turn_lock = asyncio.Lock()
        self._omni_lock = asyncio.Lock()
        self._vad = SilenceGate(
            channel="english",
            session_id=self.session_id,
            log_enabled=config.ENGLISH_LOG_AUDIO_RMS,
            track_utterance=True,  # 本地句末静音后等待 Omni 转写
        )
        self._silence_watchdog = WatchdogTask()
        self._empty_turns = EmptyTurnTracker(
            channel="english", session_id=self.session_id,
        )
        self._uplink_drop_log_counter: int = 0
        # 本轮 Omni 是否已出用户转写；本地句末后等待 Omni 的起点
        self._omni_turn_has_user_text = False
        self._pending_omni_transcript_since = None
        # 持续 speech=True 向 Omni 送音频、却一直无转写的起点（噪声场景）
        self._speech_no_text_since: Optional[float] = None
        self._no_text_log_at_sec: int = -1
        # Web 测试页走裸 PCM，避免浏览器端 Opus 编解码
        self._web_pcm_mode = False
        self._web_pcm_carry = b""
        self._uplink_open = True
        self._omni_pcm_bytes = 0

        # 用户画像 / 历史：按 device_id 加载，驱动 Omni instructions
        self._profile: EnglishProfile = get_store().get(device_id)
        self._history_context = ""
        self._last_user_text = ""
        self._last_tutor_text = ""
        self._user_turn_buf = ""
        self._tutor_stream_buf = ""
        self._tts_start_sent = False
        self._tts_start_at: Optional[float] = None
        self._response_had_audio = False
        self._omni_intentional_close = False
        self._omni_response_deadline: Optional[float] = None
        self._omni_response_task: Optional[asyncio.Task] = None
        # 云端故障锁定：同一次失败只提示一次；MCU goodbye 后自动 listen 不再重连/重提示
        self._cloud_fault_latched = False
        self._cloud_fault_log_suppress = False
        self._last_suppressed_listen_at = 0.0
        self._create_response_kick_task: Optional[asyncio.Task] = None
        self._turn_saved = False
        self._profile_task: Optional[asyncio.Task] = None
        self._history_task: Optional[asyncio.Task] = None
        self._diagnosis_task: Optional[asyncio.Task] = None
        # Web 看图学英语：会话内保留一张待注入图片（每轮 listen 重新 append）
        self._pending_image_b64: Optional[str] = None
        self._image_injected = False

    # ---------------- 消息分发 ----------------

    async def handle_text(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("收到非法 JSON: %s", raw[:200])
            return
        msg_type = msg.get("type")
        # 图片 base64 很长，日志只打类型与长度
        if msg_type == "image":
            data = msg.get("data") or ""
            logger.info(
                "[recv][english][%s] image format=%s b64_len=%d",
                self.session_id, msg.get("format"), len(data),
            )
        else:
            logger.info("[recv][english][%s] %s", self.session_id, raw[:300])

        if msg_type == "hello":
            await self._on_hello(msg)
        elif msg_type == "listen":
            await self._on_listen(msg)
        elif msg_type == "abort":
            await self._on_abort(msg)
        elif msg_type == "image":
            await self._on_image(msg)
        elif msg_type == "image_clear":
            await self._on_image_clear()
        elif msg_type == "goodbye":
            await self._close_turn()
        elif msg_type == "mcp":
            logger.debug("收到 MCP 消息（已忽略）")
        else:
            logger.debug("未处理的消息类型: %s", msg_type)

    def handle_binary(self, data: bytes):
        if not self._listening or self._speaking or not self._uplink_open:
            self._uplink_drop_log_counter += 1
            if self._uplink_drop_log_counter == 1 or self._uplink_drop_log_counter % 50 == 0:
                logger.info(
                    "[english][%s] 丢弃上行音频帧 #%d listening=%s speaking=%s uplink_open=%s opus_len=%d",
                    self.session_id,
                    self._uplink_drop_log_counter,
                    self._listening,
                    self._speaking,
                    self._uplink_open,
                    len(data or b""),
                )
            return
        try:
            self._audio_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("[english][%s] 上行音频队列已满，丢帧", self.session_id)

    async def _audio_consumer(self):
        while True:
            data = await self._audio_queue.get()
            if data is None:
                break
            if not self._listening or self._speaking or self._omni is None:
                continue
            pcm = data if self._web_pcm_mode else self.decoder.decode(data)
            if not pcm:
                continue
            result = self._vad.on_frame(pcm)
            if result.active_speech:
                self._vad.mark_utterance_active()
                # 句末等待中又开口：取消「静音后等转写」，改走「持续语音无转写」计时
                if self._pending_omni_transcript_since is not None:
                    self._pending_omni_transcript_since = None
                if (
                    not self._omni_turn_has_user_text
                    and self._speech_no_text_since is None
                ):
                    self._speech_no_text_since = time.time()
                    self._no_text_log_at_sec = -1
                    logger.info(
                        "[english][%s] 开始向 Omni 送有效语音，尚无用户转写"
                        "（持续 %.0fs 无转写将计空转）",
                        self.session_id,
                        config.EMPTY_OMNI_SPEECH_NO_TEXT_SEC,
                    )
            if result.frame_count == 1:
                logger.info(
                    "[english][%s] 收到首帧上行音频 opus/pcm_len=%d pcm=%dB RMS=%.0f 阈值=%.0f",
                    self.session_id,
                    len(data),
                    len(pcm),
                    result.rms,
                    config.SPEECH_RMS_THRESHOLD,
                )
            fed = await self._feed_pcm_to_omni(pcm)
            # 与 RMS 日志同频：提醒「在送音频但 Omni 仍无用户文本」
            if (
                fed
                and not self._omni_turn_has_user_text
                and result.frame_count % 17 == 0
            ):
                speech_wait = (
                    time.time() - self._speech_no_text_since
                    if self._speech_no_text_since is not None
                    else 0.0
                )
                logger.info(
                    "[english][%s] 已送Omni音频 #%d pcm_bytes=%d RMS=%.0f "
                    "speech=%s 尚无用户转写 已持续语音无文本=%.1fs/%.0fs",
                    self.session_id,
                    result.frame_count,
                    self._omni_pcm_bytes,
                    result.rms,
                    result.active_speech,
                    speech_wait,
                    config.EMPTY_OMNI_SPEECH_NO_TEXT_SEC,
                )

    async def _feed_pcm_to_omni(self, pcm: bytes) -> bool:
        if not pcm or self._omni is None:
            return False
        try:
            b64 = base64.b64encode(pcm).decode("ascii")
            await self.loop.run_in_executor(None, self._omni.append_audio, b64)
            self._omni_pcm_bytes += len(pcm)
            return True
        except Exception as e:  # noqa: BLE001
            err = str(e)
            low = err.lower()
            logger.warning("[english][%s] 喂 Omni 失败: %s", self.session_id, err)
            if any(
                key in low
                for key in (
                    "close", "closed", "connect", "socket", "websocket",
                    "timeout", "broken", "reset", "eof",
                )
            ):
                await self._handle_omni_error(f"云端连接中断: {err}")
            return False

    def _arm_omni_response_watch(self, reason: str = ""):
        """开始等待 Omni 回复；超时则通知客户端云端失败。"""
        self._omni_response_deadline = (
            time.time() + config.ENGLISH_OMNI_RESPONSE_TIMEOUT_SEC
        )
        t = self._omni_response_task
        if t is not None and not t.done():
            t.cancel()
        self._omni_response_task = asyncio.create_task(
            self._omni_response_timeout_task()
        )
        if reason:
            logger.info(
                "[english][%s] 开始等待 Omni 回复（%.0fs）reason=%s",
                self.session_id,
                config.ENGLISH_OMNI_RESPONSE_TIMEOUT_SEC,
                reason,
            )

    def _disarm_omni_response_watch(self):
        self._omni_response_deadline = None
        t = self._omni_response_task
        self._omni_response_task = None
        if t is not None and not t.done():
            t.cancel()

    async def _omni_response_timeout_task(self):
        try:
            await asyncio.sleep(config.ENGLISH_OMNI_RESPONSE_TIMEOUT_SEC)
            await self._check_omni_response_timeout()
        except asyncio.CancelledError:
            return

    async def _check_omni_response_timeout(self):
        deadline = self._omni_response_deadline
        if deadline is None:
            return
        # 已开始收到回复内容则视为成功在路上
        if self._response_had_audio or self._tts_start_sent or self._speaking:
            self._disarm_omni_response_watch()
            return
        if time.time() < deadline:
            return
        self._disarm_omni_response_watch()
        logger.error(
            "[english][%s] 等待 Omni 回复超时（%.0fs）",
            self.session_id,
            config.ENGLISH_OMNI_RESPONSE_TIMEOUT_SEC,
        )
        await self._handle_omni_error(
            f"云端回复超时（>{config.ENGLISH_OMNI_RESPONSE_TIMEOUT_SEC:.0f}s）"
        )

    async def _on_omni_closed(self, close_status_code, close_msg):
        """Omni WebSocket 被动断开时通知 MCU/Web，避免客户端一直干等。"""
        logger.warning(
            "[english][%s] Omni 连接关闭 code=%s msg=%s intentional=%s listening=%s",
            self.session_id,
            close_status_code,
            close_msg,
            self._omni_intentional_close,
            self._listening,
        )
        self._omni = None
        self._disarm_omni_response_watch()
        if self._omni_intentional_close or self._error_notified:
            return
        if not self._listening and not self._speaking and not self._tts_start_sent:
            return
        detail = "云端连接已断开"
        if close_msg:
            detail = f"{detail}: {close_msg}"
        elif close_status_code is not None:
            detail = f"{detail}（code={close_status_code}）"
        await self._handle_omni_error(detail)

    async def _silence_watchdog_loop(self):
        await silence_watchdog_loop(
            self._vad,
            should_run=lambda: self._listening,
            is_busy=lambda: self._speaking,
            on_idle_timeout=self._on_silence_timeout,
            on_utterance_end=self._on_local_utterance_quiet,
            break_on_utterance_end=False,  # 继续等 Omni 转写 / 空转计数
            on_tick=self._on_silence_watchdog_tick,
        )

    async def _on_silence_watchdog_tick(self) -> bool:
        if await self._check_omni_empty_conditions():
            return True
        await self._check_omni_response_timeout()
        return False

    async def _on_local_utterance_quiet(self):
        """本地 VAD：有过有效语音后静音达到句末阈值 → 开始等 Omni 用户转写。"""
        if self._omni_turn_has_user_text or self._speaking:
            return
        # 句末后改走「静音等待转写」；暂停「持续语音无转写」计时
        self._speech_no_text_since = None
        if self._pending_omni_transcript_since is not None:
            return
        self._pending_omni_transcript_since = time.time()
        logger.info(
            "[english][%s] 本地句末静音，等待 Omni 用户转写（超时 %.0fs 计空转）",
            self.session_id,
            config.EMPTY_OMNI_TRANSCRIPT_TIMEOUT_SEC,
        )

    async def _check_omni_empty_conditions(self) -> bool:
        """两类空转：①句末后等转写超时 ②持续送语音却一直无转写。"""
        if self._speaking or self._omni_turn_has_user_text:
            return False

        # ② 持续 speech、Omni 迟迟不给用户转写（你日志里的场景）
        if self._speech_no_text_since is not None:
            waited = time.time() - self._speech_no_text_since
            waited_i = int(waited)
            if waited_i != self._no_text_log_at_sec and waited_i > 0 and waited_i % 2 == 0:
                self._no_text_log_at_sec = waited_i
                logger.info(
                    "[english][%s] 持续向Omni送语音 %.1fs 仍无用户转写 "
                    "pcm_bytes=%d 空转=%d/%d（阈值 %.0fs）",
                    self.session_id,
                    waited,
                    self._omni_pcm_bytes,
                    self._empty_turns.streak,
                    self._empty_turns.limit,
                    config.EMPTY_OMNI_SPEECH_NO_TEXT_SEC,
                )
            if waited >= config.EMPTY_OMNI_SPEECH_NO_TEXT_SEC:
                logger.info(
                    "[english][%s] 判定：已送Omni有效语音 %.1fs 但无用户转写 → 计空转",
                    self.session_id,
                    waited,
                )
                # 重置窗口，便于连续噪声下累计多次空转
                self._speech_no_text_since = time.time()
                self._no_text_log_at_sec = -1
                self._vad.clear_utterance()
                if self._empty_turns.note_empty(
                    reason=f"持续送语音{waited:.1f}s无Omni转写"
                ):
                    await self._send_idle_goodbye()
                    return True

        # ① 句末静音后等待 Omni 转写超时
        if self._pending_omni_transcript_since is None:
            return False
        waited = time.time() - self._pending_omni_transcript_since
        if waited < config.EMPTY_OMNI_TRANSCRIPT_TIMEOUT_SEC:
            return False
        self._pending_omni_transcript_since = None
        self._vad.clear_utterance()
        logger.info(
            "[english][%s] 判定：句末后等待Omni转写 %.1fs 仍无文本 → 计空转",
            self.session_id,
            waited,
        )
        if self._empty_turns.note_empty(
            reason=f"等待Omni转写超时{waited:.1f}s"
        ):
            await self._send_idle_goodbye()
            return True
        return False

    async def _on_silence_timeout(self):
        if self._speaking or not self._listening:
            return
        if not self._vad.idle_timeout_due():
            return

        logger.info(
            "[english][%s] 静音超时 %.1fs（阈值 %.0fs），上行帧=%d 低RMS连续=%d，"
            "关闭 Omni 并下发 goodbye→idle",
            self.session_id,
            self._vad.quiet_sec,
            config.SILENCE_TIMEOUT_SEC,
            self._vad.frame_count,
            self._vad.low_rms_frames,
        )
        await self._send_idle_goodbye()

    async def _send_idle_goodbye(self):
        """下发 goodbye，让设备退出聆听回到 idle。

        注意：可能从 watchdog 回调同步 await 进来——先发 goodbye，再关 Omni；
        且不得 cancel 当前 watchdog 任务，否则 goodbye 发不出去。
        """
        already_idle = (not self._listening) and (not self._uplink_open)
        self._listening = False
        self._uplink_open = False
        self._pending_omni_transcript_since = None
        self._speech_no_text_since = None
        self._no_text_log_at_sec = -1
        self._stop_audio_consumer()
        # 先通知设备回 idle，再清理 Omni（避免 close 耗时/异常导致 MCU 一直聆听）
        try:
            await self._send_json({"type": "goodbye"})
            logger.info(
                "[english][%s] 已下发 goodbye（设备应回 idle） already_idle=%s",
                self.session_id,
                already_idle,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[english][%s] 发送 goodbye 失败: %s", self.session_id, e)
        self._silence_watchdog.stop()
        try:
            await self._close_omni()
        except Exception as e:  # noqa: BLE001
            logger.warning("[english][%s] idle 时关闭 Omni 失败: %s", self.session_id, e)

    def _note_omni_user_text(self, text: str):
        """Omni 产出用户转写（有效文本）。"""
        self._omni_turn_has_user_text = True
        self._pending_omni_transcript_since = None
        self._speech_no_text_since = None
        self._no_text_log_at_sec = -1
        self._empty_turns.note_text()
        self._vad.clear_utterance()
        # 关键：停喂上行。否则环境噪声会继续 append，flash/server_vad
        # 可能迟迟不 create response，MCU 表现为「识别了但没有语音回复」。
        self._uplink_open = False
        self._stop_silence_watchdog()
        # MCU server_vad：已有用户话，开始等导师回复
        self._arm_omni_response_watch("user_transcript")
        self._schedule_post_transcript_nudge()
        logger.info(
            "[english][%s] Omni用户转写=有效 text=%r 已暂停上行，等待回复",
            self.session_id,
            (text or "")[:120],
        )

    def _cancel_create_response_kick(self):
        t = self._create_response_kick_task
        self._create_response_kick_task = None
        if t is not None and not t.done():
            t.cancel()

    def _schedule_post_transcript_nudge(self):
        """转写完成后若迟迟无导师音频，按 Web / MCU 分别促发或解卡。"""
        self._cancel_create_response_kick()
        if self._web_pcm_mode:
            self._create_response_kick_task = asyncio.create_task(
                self._create_response_kick_loop()
            )
        else:
            self._create_response_kick_task = asyncio.create_task(
                self._mcu_response_recovery_loop()
            )

    def _schedule_create_response_kick(self):
        """兼容旧调用：等同 _schedule_post_transcript_nudge。"""
        self._schedule_post_transcript_nudge()

    async def _omni_cancel_and_create_response(self, *, reason: str) -> bool:
        """清掉云端卡住的 response 再建一轮；成功发起 create 返回 True。"""
        if self._omni is None:
            return False
        try:
            await self.loop.run_in_executor(None, self._omni.cancel_response)
        except Exception as e:  # noqa: BLE001
            logger.info(
                "[english][%s] cancel_response（%s）: %s",
                self.session_id, reason, e,
            )
        await asyncio.sleep(0.35)
        if self._response_had_audio or self._tts_start_sent or self._speaking:
            return False
        try:
            await self.loop.run_in_executor(None, self._omni.create_response)
            logger.info(
                "[english][%s] 已 create_response（%s）",
                self.session_id, reason,
            )
            return True
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "already has an active response" in msg.lower():
                logger.info(
                    "[english][%s] create_response 仍撞 active（%s），再 cancel 一次",
                    self.session_id, reason,
                )
                try:
                    await self.loop.run_in_executor(
                        None, self._omni.cancel_response
                    )
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(0.35)
                try:
                    await self.loop.run_in_executor(
                        None, self._omni.create_response
                    )
                    logger.info(
                        "[english][%s] 二次 create_response 成功（%s）",
                        self.session_id, reason,
                    )
                    return True
                except Exception as e2:  # noqa: BLE001
                    logger.warning(
                        "[english][%s] 二次 create_response 失败（%s）: %s",
                        self.session_id, reason, e2,
                    )
                    return False
            logger.warning(
                "[english][%s] create_response 失败（%s）: %s",
                self.session_id, reason, e,
            )
            return False

    async def _mcu_response_recovery_loop(self):
        """MCU + server_vad：云端偶发「有 active 但不下发 audio」，解卡。"""
        try:
            await asyncio.sleep(2.5)
            if self._waiting_reply_aborted():
                return
            if not self._response_had_audio and not self._tts_start_sent:
                logger.info(
                    "[english][%s] MCU 转写后 %.1fs 仍无 response/音频，尝试解卡",
                    self.session_id, 2.5,
                )
                await self._omni_cancel_and_create_response(
                    reason="mcu_recovery_1"
                )

            await asyncio.sleep(6.0)
            if self._waiting_reply_aborted():
                return
            if not self._response_had_audio and not self._tts_start_sent:
                logger.info(
                    "[english][%s] MCU 转写后 ~8.5s 仍无音频，再次解卡",
                    self.session_id,
                )
                await self._omni_cancel_and_create_response(
                    reason="mcu_recovery_2"
                )
        except asyncio.CancelledError:
            return

    def _waiting_reply_aborted(self) -> bool:
        return bool(
            self._cancel
            or self._speaking
            or self._response_had_audio
            or self._tts_start_sent
            or self._active_response_id
            or self._omni is None
        )

    async def _create_response_kick_loop(self):
        try:
            # 给 server_vad 自动建 response 留足时间；过早 kick 会撞上
            # "Conversation already has an active response" 并误杀会话
            await asyncio.sleep(4.0)
            if (
                self._cancel
                or self._speaking
                or self._response_had_audio
                or self._tts_start_sent
                or self._active_response_id
                or self._omni is None
            ):
                return
            logger.info(
                "[english][%s] 转写后仍无回复，主动 create_response",
                self.session_id,
            )
            await self.loop.run_in_executor(None, self._omni.create_response)
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "already has an active response" in msg.lower():
                logger.info(
                    "[english][%s] Web kick 遇 active response，改 cancel+create",
                    self.session_id,
                )
                await self._omni_cancel_and_create_response(
                    reason="web_kick_active"
                )
                return
            logger.warning(
                "[english][%s] create_response 促发失败: %s",
                self.session_id, e,
            )

    @staticmethod
    def _is_benign_omni_error(detail: str) -> bool:
        low = (detail or "").lower()
        return "already has an active response" in low

    async def _note_omni_empty_turn(self, *, reason: str):
        """Omni 一轮结束但无用户文本 → 空转计数。"""
        self._pending_omni_transcript_since = None
        self._speech_no_text_since = None
        self._no_text_log_at_sec = -1
        self._vad.clear_utterance()
        self._omni_turn_has_user_text = False
        logger.info(
            "[english][%s] Omni用户转写=无效/缺失 reason=%s",
            self.session_id,
            reason,
        )
        if self._empty_turns.note_empty(reason=reason):
            await self._send_idle_goodbye()

    def _start_silence_watchdog(self):
        self._silence_watchdog.start(self._silence_watchdog_loop())

    def _stop_silence_watchdog(self):
        self._silence_watchdog.stop()

    async def _on_hello(self, msg: dict):
        audio = msg.get("audio_params") or {}
        self._web_pcm_mode = (
            msg.get("client") == "web"
            or audio.get("format") == "pcm"
        )
        reply = self.transport.make_server_hello(self.session_id)
        if self._web_pcm_mode:
            reply["audio_params"] = {
                "format": "pcm",
                "sample_rate": config.DOWNLINK_SAMPLE_RATE,
                "channels": config.CHANNELS,
                "frame_duration": config.FRAME_DURATION_MS,
            }
        await self._send_json(reply)
        logger.info(
            "[english] 已回复 hello, session_id=%s web_pcm=%s",
            self.session_id, self._web_pcm_mode,
        )

    async def _on_listen(self, msg: dict):
        state = msg.get("state")
        if state == "start":
            await self._start_listening()
        elif state == "stop":
            if self._web_pcm_mode:
                await self._finish_user_utterance()
            else:
                await self._stop_listening()
        elif state == "detect":
            # 重新唤醒 = 用户新一轮尝试，允许再次提示云端错误
            self._clear_cloud_fault_latch("wake_detect")
            logger.info("[english] 唤醒词: %s", msg.get("text", ""))

    def _clear_cloud_fault_latch(self, reason: str = ""):
        if not self._cloud_fault_latched and not self._error_notified:
            return
        self._cloud_fault_latched = False
        self._cloud_fault_log_suppress = False
        self._error_notified = False
        logger.info(
            "[english][%s] 解除云端故障锁定 reason=%s",
            self.session_id, reason or "?",
        )

    async def _on_abort(self, msg: dict):
        logger.info("[english] 收到 abort, reason=%s", msg.get("reason"))
        # 先置取消并清空下行队列，再抢 omni 锁结束本轮
        self._cancel = True
        await self._abort_downlink()
        if self._omni is not None:
            try:
                await self.loop.run_in_executor(None, self._omni.cancel_response)
            except Exception:  # noqa: BLE001
                pass
        async with self._omni_lock:
            await self._finish_speaking(aborted=True)
        self._cancel = False
        logger.info("[english][%s] abort 处理完成", self.session_id)

    async def _on_image(self, msg: dict):
        """Web 端上传看图练英语的 JPEG（Base64）。"""
        data = (msg.get("data") or "").strip()
        if data.startswith("data:") and "," in data:
            data = data.split(",", 1)[1]
        data = "".join(data.split())  # 去掉换行/空白
        if not data:
            await self._send_json({
                "type": "image_ack", "ok": False, "message": "图片数据为空",
            })
            return
        if len(data) > MAX_IMAGE_B64_LEN:
            await self._send_json({
                "type": "image_ack",
                "ok": False,
                "message": "图片太大，请压缩后再试（建议小于 180KB）",
            })
            return
        # 粗校验：能否解码
        try:
            raw = base64.b64decode(data, validate=False)
        except Exception:  # noqa: BLE001
            await self._send_json({
                "type": "image_ack", "ok": False, "message": "图片编码无效",
            })
            return
        if len(raw) < 100:
            await self._send_json({
                "type": "image_ack", "ok": False, "message": "图片无效",
            })
            return
        # JPEG 魔数（也接受少数相机 HEIC 误传时给友好提示）
        if not (raw[:2] == b"\xff\xd8"):
            await self._send_json({
                "type": "image_ack",
                "ok": False,
                "message": "请使用 JPEG 格式图片",
            })
            return

        self._pending_image_b64 = data
        self._image_injected = False
        logger.info(
            "[english][%s] 已保存待注入图片 raw=%dB b64=%d",
            self.session_id, len(raw), len(data),
        )
        await self._send_json({
            "type": "image_ack",
            "ok": True,
            "message": "图片已添加，按住说话开始看图练英语",
        })

    async def _on_image_clear(self):
        self._pending_image_b64 = None
        self._image_injected = False
        await self._send_json({"type": "image_ack", "ok": True, "cleared": True})
        logger.info("[english][%s] 已清除会话图片", self.session_id)

    async def _inject_pending_image(self, *, seed_silence: bool = True, force: bool = False):
        """向 Omni 注入图片。开场可带静音种子；commit 前应再 force 注入一次。"""
        if not self._pending_image_b64 or self._omni is None:
            return False
        if self._image_injected and not force:
            return True
        try:
            if seed_silence and not self._image_injected:
                silence = b"\x00" * IMAGE_SEED_SILENCE_BYTES
                await self._feed_pcm_to_omni(silence)
            await self.loop.run_in_executor(
                None, self._omni.append_video, self._pending_image_b64
            )
            self._image_injected = True
            logger.info(
                "[english][%s] 已向 Omni 注入看图帧 force=%s seed=%s b64=%d",
                self.session_id, force, seed_silence, len(self._pending_image_b64),
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("[english][%s] 注入图片失败: %s", self.session_id, e)
            await self._send_json({
                "type": "image_ack",
                "ok": False,
                "message": f"图片发送失败: {e}",
            })
            return False

    async def _start_listening(self):
        if self._speaking:
            logger.info("[english][%s] 仍在播放，忽略 listen start", self.session_id)
            return

        # MCU 收到 goodbye 后常立刻再 listen start；锁定期间禁止重连 Omni，避免每秒刷屏提示
        if self._cloud_fault_latched and not self._web_pcm_mode:
            now = time.time()
            gap = now - self._last_suppressed_listen_at if self._last_suppressed_listen_at else 999.0
            self._last_suppressed_listen_at = now
            # 密集自动重连（<5s）一律忽略；间隔变长则视为用户再次尝试
            if gap < 5.0:
                if not self._cloud_fault_log_suppress:
                    self._cloud_fault_log_suppress = True
                    logger.info(
                        "[english][%s] 云端故障已提示过，忽略后续自动 listen start"
                        "（重新唤醒后再试）",
                        self.session_id,
                    )
                return
            self._clear_cloud_fault_latch("listen_after_gap")

        # Web 按住说话 = 用户主动再试，解除锁定
        if self._cloud_fault_latched and self._web_pcm_mode:
            self._clear_cloud_fault_latch("web_listen")

        await self._cancel_speaking()
        await self._close_omni()
        self._drain_audio_queue()

        self._listening = True
        self._speaking = False
        self._cancel = False
        self._error_notified = False
        self._omni_intentional_close = False
        self._response_had_audio = False
        self._disarm_omni_response_watch()
        self._vad.reset(mark_quiet_now=True)
        self._empty_turns.reset()
        self._omni_turn_has_user_text = False
        self._pending_omni_transcript_since = None
        self._speech_no_text_since = None
        self._no_text_log_at_sec = -1
        self._uplink_drop_log_counter = 0
        self._uplink_open = True
        self._omni_pcm_bytes = 0
        self._user_turn_buf = ""
        self._image_injected = False

        try:
            await self._open_omni()
            await self._inject_pending_image()
            self._audio_task = asyncio.create_task(self._audio_consumer())
            self._start_silence_watchdog()
            logger.info(
                "[english][%s] 开始聆听，Omni 已连接 model=%s voice=%s has_image=%s "
                "静音超时=%.0fs RMS阈值=%.0f",
                self.session_id,
                config.ENGLISH_OMNI_MODEL,
                config.ENGLISH_OMNI_VOICE,
                bool(self._pending_image_b64),
                config.SILENCE_TIMEOUT_SEC,
                config.SPEECH_RMS_THRESHOLD,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("[english] 启动 Omni 失败: %s", e)
            await self._handle_omni_error(f"无法连接云端口语模型: {e}")

    async def _stop_listening(self):
        self._listening = False
        self._uplink_open = False
        self._stop_silence_watchdog()
        self._stop_audio_consumer()
        await self._close_omni()

    async def _flush_audio_queue(self):
        """把队列里尚未喂给 Omni 的音频全部送出。"""
        while True:
            try:
                data = self._audio_queue.get_nowait()
            except QueueEmpty:
                break
            if data is None or self._omni is None:
                continue
            pcm = data if self._web_pcm_mode else self.decoder.decode(data)
            if not pcm:
                continue
            await self._feed_pcm_to_omni(pcm)

    async def _finish_user_utterance(self):
        """Web 按住说话松手：提交音频并等待 Omni 回复，不立刻断开。"""
        if not self._listening or self._omni is None:
            return
        self._uplink_open = False
        self._stop_silence_watchdog()
        # 等待异步 consumer 把队列里的帧喂完
        await asyncio.sleep(0.25)
        await self._flush_audio_queue()
        await asyncio.sleep(0.05)

        if self._omni_pcm_bytes < MIN_WEB_OMNI_PCM_BYTES:
            logger.info(
                "[english][%s] 音频过短 bytes=%d，跳过 commit",
                self.session_id, self._omni_pcm_bytes,
            )
            await self._notify_too_short()
            return

        try:
            # commit 前再送一次图：避免仅开场注入时被 VAD/缓冲策略丢掉
            if self._pending_image_b64:
                await self._inject_pending_image(seed_silence=False, force=True)
            await self.loop.run_in_executor(None, self._omni.commit)
            await self.loop.run_in_executor(None, self._omni.create_response)
            self._response_had_audio = False
            self._arm_omni_response_watch("web_commit")
            logger.info(
                "[english][%s] Web 松手，已 commit + create_response（%d bytes）has_image=%s，等待 Omni 回复",
                self.session_id, self._omni_pcm_bytes, bool(self._pending_image_b64),
            )
        except Exception as e:  # noqa: BLE001
            err = str(e).lower()
            if "buffer too small" in err or "no audio" in err:
                await self._notify_too_short()
            else:
                logger.warning("[english] commit/create_response 失败: %s", e)
                await self._handle_omni_error(f"提交语音失败: {e}")

    def _load_history_context(self) -> str:
        if not config.ENGLISH_HISTORY_ENABLED:
            return ""
        try:
            msgs = get_history_store().get_recent(self.device_id)
            return format_history_context(msgs)
        except Exception as e:  # noqa: BLE001
            logger.warning("[english] 加载对话历史失败: %s", e)
            return ""

    def _build_session_instructions(self) -> str:
        text = build_instructions(
            self._profile,
            history_context=self._history_context,
        )
        if self._pending_image_b64:
            text += (
                "\n\nPHOTO CONTEXT: The student has attached a photo for this turn. "
                "You WILL receive the image in the multimodal input. "
                "You can see the photo. Do not say you cannot see it. "
                "Describe or discuss what is actually in the image, teach useful English words, "
                "and help the student talk about the photo."
            )
        return text

    async def _open_omni(self):
        self._profile = get_store().get(self.device_id)
        self._history_context = await self.loop.run_in_executor(
            None, self._load_history_context
        )
        instructions = self._build_session_instructions()
        callback = _OmniBridge(self)
        conv = OmniRealtimeConversation(
            model=config.ENGLISH_OMNI_MODEL,
            callback=callback,
            url=config.ENGLISH_OMNI_WS_URL,
        )
        await self.loop.run_in_executor(None, conv.connect)

        # Web 按住说话走手动 commit；开着 server_vad 容易抢先提交，导致看图帧丢失
        use_server_vad = not self._web_pcm_mode

        def _configure():
            conv.update_session(
                output_modalities=[MultiModality.AUDIO, MultiModality.TEXT],
                voice=config.ENGLISH_OMNI_VOICE,
                input_audio_format=AudioFormat.PCM_16000HZ_MONO_16BIT,
                output_audio_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
                enable_turn_detection=use_server_vad,
                turn_detection_type="server_vad",
                instructions=instructions,
            )

        await self.loop.run_in_executor(None, _configure)
        self._omni = conv
        hist_chars = len(self._history_context or "")
        logger.info(
            "[english][%s] 画像已注入 turns=%d refine_at=%d history_chars=%d vad=%s text=%s",
            self.session_id,
            self._profile.turn_count,
            self._profile.last_refine_turn,
            hist_chars,
            "server_vad" if use_server_vad else "manual",
            (self._profile.profile_text or "")[:120],
        )

    async def _refresh_omni_instructions(self):
        """画像/历史变更后热更新当前 Omni session（若仍连接）。"""
        omni = self._omni
        if omni is None:
            return
        instructions = self._build_session_instructions()

        use_server_vad = not self._web_pcm_mode

        def _update():
            omni.update_session(
                output_modalities=[MultiModality.AUDIO, MultiModality.TEXT],
                voice=config.ENGLISH_OMNI_VOICE,
                input_audio_format=AudioFormat.PCM_16000HZ_MONO_16BIT,
                output_audio_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
                enable_turn_detection=use_server_vad,
                turn_detection_type="server_vad",
                instructions=instructions,
            )

        try:
            await self.loop.run_in_executor(None, _update)
            logger.info(
                "[english][%s] 已热更新 instructions profile_chars=%d",
                self.session_id,
                len(self._profile.profile_text or ""),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[english] 热更新 instructions 失败: %s", e)

    def _schedule_history_save(self):
        if not config.ENGLISH_HISTORY_ENABLED:
            return
        if self._turn_saved:
            return
        user_text = self._last_user_text
        tutor_text = self._last_tutor_text
        if not user_text and not tutor_text:
            return
        self._turn_saved = True
        session_id = self.session_id
        device_id = self.device_id

        async def _run():
            try:
                await self.loop.run_in_executor(
                    None,
                    lambda: get_history_store().append_turn(
                        device_id,
                        user_text,
                        tutor_text,
                        session_id=session_id,
                    ),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("[english] 保存对话历史失败: %s", e)
                self._turn_saved = False
                return
            lines = []
            if (user_text or "").strip():
                lines.append(f"Student: {user_text.strip().replace(chr(10), ' ')}")
            if (tutor_text or "").strip():
                lines.append(f"Tutor: {tutor_text.strip().replace(chr(10), ' ')}")
            if lines:
                extra = "\n".join(lines)
                if self._history_context:
                    merged = self._history_context + "\n" + extra
                    self._history_context = merged[-config.ENGLISH_HISTORY_MAX_CHARS :]
                else:
                    self._history_context = extra
            logger.info(
                "[english][%s] 历史已落库 user=%s tutor=%s",
                self.session_id,
                bool(user_text),
                bool(tutor_text),
            )

        self._history_task = asyncio.create_task(_run())

    def _schedule_explicit_profile_refine(self, user_text: str):
        """用户明确提偏好时，异步重写画像并热更新 instructions。"""

        async def _run():
            try:
                updated = await self.loop.run_in_executor(
                    None,
                    lambda: refine_profile(
                        self.device_id,
                        user_text=user_text,
                        tutor_text="",
                        reason="explicit_user_request",
                        force=True,
                    ),
                )
                if updated is None:
                    return
                old_text = self._profile.profile_text
                self._profile = updated
                if (updated.profile_text or "").strip() != (old_text or "").strip():
                    await self._refresh_omni_instructions()
            except Exception as e:  # noqa: BLE001
                logger.warning("[english] 显式画像更新失败: %s", e)

        asyncio.create_task(_run())

    def _schedule_profile_update(self):
        user_text = self._last_user_text
        tutor_text = self._last_tutor_text
        if not user_text:
            return
        if self._profile_task and not self._profile_task.done():
            return

        async def _run():
            try:
                updated = await self.loop.run_in_executor(
                    None,
                    lambda: maybe_update_from_turn(
                        self.device_id, user_text, tutor_text
                    ),
                )
                if updated is None:
                    return
                old_text = self._profile.profile_text
                self._profile = updated
                if (updated.profile_text or "").strip() != (old_text or "").strip():
                    await self._refresh_omni_instructions()
            except Exception as e:  # noqa: BLE001
                logger.warning("[english] 画像异步更新失败: %s", e)

        self._profile_task = asyncio.create_task(_run())

    def _schedule_diagnosis(self):
        """轮后轻量诊断，推送到页面，不影响主链路。"""
        user_text = self._last_user_text
        tutor_text = self._last_tutor_text
        if not user_text:
            return
        if self._diagnosis_task and not self._diagnosis_task.done():
            return

        async def _run():
            try:
                result = await self.loop.run_in_executor(
                    None, lambda: diagnose_turn(user_text, tutor_text)
                )
                if not result:
                    return
                await self._send_json({
                    "type": "correction",
                    "error_type": result.get("error_type"),
                    "zh_explain": result.get("zh_explain"),
                    "correct_en": result.get("correct_en"),
                    "severity": result.get("severity"),
                })
                logger.info(
                    "[english][%s] 诊断: %s | %s",
                    self.session_id,
                    result.get("error_type"),
                    (result.get("zh_explain") or "")[:80],
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("[english] 诊断失败: %s", e)

        self._diagnosis_task = asyncio.create_task(_run())

    async def _close_omni(self):
        omni = self._omni
        self._omni = None
        self._cancel_create_response_kick()
        self._disarm_omni_response_watch()
        if omni is None:
            return
        self._omni_intentional_close = True
        try:
            await self.loop.run_in_executor(None, omni.close)
        except Exception:  # noqa: BLE001
            pass

    def _append_user_transcript(self, text: str) -> str:
        """同一次按住说话内，server_vad 可能多段转写，拼成完整一句。"""
        part = (text or "").strip()
        if not part:
            return self._user_turn_buf
        if self._user_turn_buf:
            self._user_turn_buf = f"{self._user_turn_buf} {part}"
        else:
            self._user_turn_buf = part
        return self._user_turn_buf

    def _user_display_text(self, *, in_progress: str = "") -> str:
        preview = (in_progress or "").strip()
        if self._user_turn_buf and preview:
            return f"{self._user_turn_buf} {preview}"
        if self._user_turn_buf:
            return self._user_turn_buf
        return preview

    async def _on_omni_event(self, event: dict):
        etype = event.get("type", "")
        # 字幕/转写增量不走音频锁：避免被下行音频节流拖住，页面可即时刷新
        if etype == "response.audio_transcript.delta":
            try:
                await self._on_tutor_transcript_delta(event)
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "[english][%s] 处理字幕增量异常: %s", self.session_id, e
                )
            return
        if etype == "conversation.item.input_audio_transcription.delta":
            try:
                await self._on_user_transcript_delta(event)
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "[english][%s] 处理用户转写增量异常: %s", self.session_id, e
                )
            return
        async with self._omni_lock:
            await self._dispatch_omni_event(event)

    async def _on_user_transcript_delta(self, event: dict):
        preview = (event.get("text") or "") + (event.get("stash") or "")
        display = self._user_display_text(in_progress=preview)
        if display:
            await self._send_json({
                "type": "stt",
                "text": display,
                "partial": True,
            })

    async def _ensure_tts_start(self):
        if self._tts_start_sent:
            return
        self._tts_start_sent = True
        self._tts_start_at = time.time()
        await self._send_json({"type": "tts", "state": "start"})
        logger.info(
            "[english][%s] 已发 tts start，下行将等待 %.2fs 再推首包 UDP"
            "（等 MCU 进入 Speaking，避免开头丢包卡顿）",
            self.session_id,
            config.ENGLISH_TTS_START_LEAD_SEC if not self._web_pcm_mode else 0.0,
        )

    async def _on_tutor_transcript_delta(self, event: dict):
        delta = event.get("delta") or ""
        if not delta:
            return
        # 文本可能早于首包音频到达：先开字幕区
        await self._ensure_tts_start()
        self._tutor_stream_buf += delta
        self._tutor_stream_buf = normalize_spelling_hyphens(self._tutor_stream_buf)
        self._last_tutor_text = self._tutor_stream_buf
        await self._send_json({
            "type": "tts",
            "state": "delta",
            "text": self._tutor_stream_buf,
        })

    async def _dispatch_omni_event(self, event: dict):
        etype = event.get("type", "")
        try:
            if etype == "response.created":
                rid = _omni_response_id(event)
                if rid:
                    self._active_response_id = rid
                self._tutor_stream_buf = ""
                self._last_tutor_text = ""
                self._response_had_audio = False
                self._cancel_create_response_kick()
                self._uplink_open = False
                # 仍在播上一轮时不要重置 tts_start，避免中途再发一次 start 造成设备叠播
                if not self._speaking:
                    self._tts_start_sent = False
                self._arm_omni_response_watch("response.created")
                logger.info(
                    "[english][%s] Omni response.created id=%s",
                    self.session_id, rid or "?",
                )

            elif etype == "conversation.item.input_audio_transcription.completed":
                raw_tr = event.get("transcript")
                text = (raw_tr or "").strip()
                logger.info(
                    "[english][%s] Omni返回用户转写 completed raw=%r stripped=%r 有效=%s",
                    self.session_id,
                    (raw_tr if isinstance(raw_tr, str) else raw_tr)[:160]
                    if isinstance(raw_tr, str)
                    else raw_tr,
                    text[:160],
                    bool(text),
                )
                if text:
                    full = self._append_user_transcript(text)
                    logger.info("[english][%s] 用户说: %s", self.session_id, full)
                    self._last_user_text = full
                    self._last_tutor_text = ""
                    self._tutor_stream_buf = ""
                    self._tts_start_sent = False
                    self._turn_saved = False
                    self._note_omni_user_text(full)
                    await self._send_json({
                        "type": "stt",
                        "text": full,
                        "partial": False,
                    })
                    # 显式偏好：异步立刻沉淀画像，避免阻塞 Omni 事件循环
                    if looks_like_preference_request(full):
                        self._schedule_explicit_profile_refine(full)
                else:
                    logger.info(
                        "[english][%s] Omni用户转写=空字符串（本轮尚不计空转，"
                        "等 response.done / 等待超时再判定）",
                        self.session_id,
                    )

            elif etype == "response.audio.delta":
                rid = _omni_response_id(event)
                if (
                    rid
                    and self._active_response_id
                    and rid != self._active_response_id
                ):
                    logger.info(
                        "[english][%s] 忽略过期 audio.delta id=%s active=%s",
                        self.session_id, rid, self._active_response_id,
                    )
                    return
                if rid and not self._active_response_id:
                    self._active_response_id = rid
                delta_b64 = event.get("delta", "")
                if not delta_b64:
                    return
                pcm = base64.b64decode(delta_b64)
                self._response_had_audio = True
                self._cancel_create_response_kick()
                self._disarm_omni_response_watch()
                if not self._speaking:
                    self._speaking = True
                    self._uplink_open = False
                    self._cancel_uplink_reopen()
                    self._stop_silence_watchdog()
                    self.encoder.reset()
                    self._web_pcm_carry = b""
                    self._cancel = False
                    await self._ensure_tts_start()
                    # MCU：预缓冲代替固定 sleep，避免在 omni 锁里空等
                await self._enqueue_pcm_frames(pcm, flush=False)

            elif etype == "response.audio_transcript.done":
                text = event.get("transcript", "") or self._tutor_stream_buf
                text = normalize_spelling_hyphens(text)
                if text:
                    logger.info("[english][%s] 导师回复: %s", self.session_id, text)
                    self._last_tutor_text = text
                    self._tutor_stream_buf = text
                    await self._send_json({
                        "type": "tts",
                        "state": "sentence_start",
                        "text": text,
                    })

            elif etype == "response.audio.done":
                # 残余 PCM 在 response.done 时统一 flush + 单一 END，避免双重结束标记
                return

            elif etype == "response.done":
                rid = _omni_response_id(event)
                if (
                    rid
                    and self._active_response_id
                    and rid != self._active_response_id
                ):
                    logger.info(
                        "[english][%s] 忽略过期 response.done id=%s active=%s",
                        self.session_id, rid, self._active_response_id,
                    )
                    return
                self._cancel_create_response_kick()
                self._disarm_omni_response_watch()
                resp = event.get("response") if isinstance(event.get("response"), dict) else {}
                status = (resp.get("status") or event.get("status") or "").lower()
                status_details = resp.get("status_details") or event.get("status_details")
                # 只看本轮：勿用 _last_user_text（可能是上一轮残留）
                buf = (self._user_turn_buf or "").strip()
                had_user_text = self._omni_turn_has_user_text or bool(buf)
                had_reply = (
                    self._response_had_audio
                    or self._tts_start_sent
                    or bool((self._tutor_stream_buf or self._last_tutor_text or "").strip())
                )
                logger.info(
                    "[english][%s] response.done status=%s 有效用户文本=%s "
                    "had_reply=%s flag=%s buf=%r",
                    self.session_id,
                    status or "?",
                    had_user_text,
                    had_reply,
                    self._omni_turn_has_user_text,
                    buf[:120],
                )

                cancel_reason = ""
                fail_msg = ""
                if isinstance(status_details, dict):
                    cancel_reason = str(status_details.get("reason") or "")
                    err = status_details.get("error") or {}
                    if isinstance(err, dict):
                        fail_msg = str(err.get("message") or err.get("code") or "")
                    elif status_details.get("type"):
                        fail_msg = str(status_details.get("type"))

                # server_vad 常见：空噪声段 / 新一轮打断旧 response → cancelled+turn_detected
                # 这不是云端故障，绝不能 alert+goodbye，否则刚唤醒就报异常
                if status == "cancelled" and not self._cancel:
                    logger.info(
                        "[english][%s] response 取消（忽略）reason=%s "
                        "had_user=%s had_reply=%s",
                        self.session_id,
                        cancel_reason or "?",
                        had_user_text,
                        had_reply,
                    )
                    if self._speaking and not had_reply:
                        await self._enqueue_pcm_frames(b"", flush=True)
                        await self._finish_speaking(aborted=True)
                    self._active_response_id = None
                    self._response_had_audio = False
                    if had_user_text and not had_reply:
                        # 用户话还在，继续等下一轮自动回复
                        self._uplink_open = False
                        self._arm_omni_response_watch("after_cancelled")
                    elif self._listening and not self._uplink_open and not self._speaking:
                        self._uplink_open = True
                        self._start_silence_watchdog()
                    return

                if status == "failed" and had_user_text and not had_reply and not self._cancel:
                    detail = "云端生成回复失败"
                    if fail_msg:
                        detail = f"{detail}: {fail_msg}"
                    elif cancel_reason:
                        detail = f"{detail}: {cancel_reason}"
                    if self._speaking:
                        await self._enqueue_pcm_frames(b"", flush=True)
                        await self._finish_speaking(aborted=True)
                    await self._handle_omni_error(detail)
                    self._omni_turn_has_user_text = False
                    self._user_turn_buf = ""
                    self._active_response_id = None
                    return

                if had_user_text and not had_reply and not self._cancel:
                    if self._speaking:
                        await self._enqueue_pcm_frames(b"", flush=True)
                        await self._finish_speaking(aborted=True)
                    await self._handle_omni_error("云端未返回有效语音回复")
                    self._omni_turn_has_user_text = False
                    self._user_turn_buf = ""
                    self._active_response_id = None
                    return

                if self._speaking:
                    await self._enqueue_pcm_frames(b"", flush=True)
                await self._finish_speaking()
                if had_user_text:
                    self._empty_turns.note_text()
                    self._schedule_history_save()
                    self._schedule_profile_update()
                    self._schedule_diagnosis()
                else:
                    # 音频进了 Omni、模型还回了 response，但没有用户转写 → 空转
                    await self._note_omni_empty_turn(
                        reason="response.done但无用户转写"
                    )
                # 下一轮重新计数本轮用户文本
                self._omni_turn_has_user_text = False
                self._user_turn_buf = ""
                self._active_response_id = None

            elif etype == "error":
                err = event.get("error", {})
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                code = err.get("code") if isinstance(err, dict) else None
                if self._is_benign_omni_error(msg):
                    logger.info(
                        "[english][%s] 忽略无害 Omni 提示 code=%s msg=%s",
                        self.session_id, code, msg,
                    )
                    return
                logger.error(
                    "[english][%s] Omni 错误 code=%s msg=%s",
                    self.session_id, code, msg,
                )
                await self._handle_omni_error(msg or "云端返回错误")

            elif etype in (
                "input_audio_buffer.speech_started",
                "input_audio_buffer.speech_stopped",
                "input_audio_buffer.committed",
                "conversation.item.created",
                "session.created",
                "session.updated",
            ):
                logger.info(
                    "[english][%s] Omni事件 type=%s",
                    self.session_id, etype,
                )

            elif etype and not etype.endswith(".delta"):
                # 便于排查 flash 模型事件差异（忽略高频 delta）
                logger.info(
                    "[english][%s] Omni事件 type=%s keys=%s",
                    self.session_id, etype, list(event.keys())[:12],
                )

        except Exception as e:  # noqa: BLE001
            logger.exception("[english][%s] 处理 Omni 事件异常: %s", self.session_id, e)
            await self._handle_omni_error(f"处理云端事件异常: {e}")

    def _iter_web_pcm_frames(self, pcm: bytes, *, flush: bool):
        frame_bytes = (
            int(config.DOWNLINK_SAMPLE_RATE * config.FRAME_DURATION_MS / 1000)
            * 2
            * config.CHANNELS
        )
        self._web_pcm_carry += pcm
        while len(self._web_pcm_carry) >= frame_bytes:
            chunk = self._web_pcm_carry[:frame_bytes]
            self._web_pcm_carry = self._web_pcm_carry[frame_bytes:]
            yield chunk
        if flush and self._web_pcm_carry:
            yield self._web_pcm_carry
            self._web_pcm_carry = b""

    def _downlink_pacing_sec(self) -> float:
        if self._web_pcm_mode:
            return config.DOWNLINK_PACING_SEC
        return config.ENGLISH_DOWNLINK_PACING_SEC

    def _downlink_prebuffer_frames(self) -> int:
        if self._web_pcm_mode:
            return 1
        return max(1, config.ENGLISH_DOWNLINK_PREBUFFER_FRAMES)

    def _ensure_downlink_task(self):
        if self._downlink_task is None or self._downlink_task.done():
            self._downlink_task = asyncio.create_task(self._downlink_sender_loop())

    def _drain_downlink_queue(self):
        while True:
            try:
                self._downlink_q.get_nowait()
            except QueueEmpty:
                break

    async def _abort_downlink(self):
        """丢弃未播完的下行帧，并唤醒发送循环。"""
        self._drain_downlink_queue()
        try:
            self._downlink_q.put_nowait(_DL_ABORT)
        except asyncio.QueueFull:
            pass
        self._ensure_downlink_task()
        self._downlink_idle.set()

    async def _enqueue_pcm_frames(self, pcm: bytes, *, flush: bool):
        """Omni PCM → 编码后入下行队列（不在此处 sleep，避免拖住 omni 锁）。"""
        if self._cancel:
            return
        frames = (
            self._iter_web_pcm_frames(pcm, flush=flush)
            if self._web_pcm_mode
            else self.encoder.encode_pcm_stream(pcm, flush=flush)
        )
        n = 0
        for frame in frames:
            if self._cancel:
                break
            self._downlink_idle.clear()
            await self._downlink_q.put(frame)
            n += 1
        if flush:
            # 即使没有残余帧也要清 idle，让 _finish_speaking 等到 END 真正发完
            self._downlink_idle.clear()
            await self._downlink_q.put(_DL_END)
        self._ensure_downlink_task()
        if n:
            logger.debug(
                "[english][%s] 下行入队 %d 帧 flush=%s q=%d",
                self.session_id, n, flush, self._downlink_q.qsize(),
            )

    async def _wait_tts_start_lead(self):
        """等 MCU 处理完 tts start 进入 Speaking，再发 UDP，否则固件会丢包。"""
        if self._web_pcm_mode:
            return
        lead = max(0.0, float(config.ENGLISH_TTS_START_LEAD_SEC))
        if lead <= 0:
            return
        started_at = self._tts_start_at or time.time()
        remain = lead - (time.time() - started_at)
        if remain > 0:
            await asyncio.sleep(remain)

    async def _downlink_sender_loop(self):
        """独立时钟推送下行帧：MCU 先预缓冲再按近实时节奏发送。"""
        pending = []
        started = False
        pacing = self._downlink_pacing_sec()
        prebuffer = self._downlink_prebuffer_frames()
        burst_left = 0
        sent = 0

        async def _send_one(frame: bytes) -> bool:
            nonlocal sent, pacing, prebuffer, burst_left
            if self._cancel:
                return False
            try:
                await self.transport.send_audio(frame)
                sent += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("[english] 发送音频帧失败: %s", e)
                return False
            if burst_left > 0:
                burst_left -= 1
                await asyncio.sleep(config.ENGLISH_DOWNLINK_BURST_PACING_SEC)
            else:
                await asyncio.sleep(pacing)
            return True

        try:
            while True:
                item = await self._downlink_q.get()
                if item is _DL_ABORT:
                    pending.clear()
                    started = False
                    burst_left = 0
                    self._drain_downlink_queue()
                    self._downlink_idle.set()
                    continue

                if item is _DL_END:
                    if pending and not started:
                        # 短回复：预缓冲未满也要开播，先等 MCU Speaking
                        await self._wait_tts_start_lead()
                        started = True
                        pacing = self._downlink_pacing_sec()
                        burst_left = max(0, config.ENGLISH_DOWNLINK_BURST_FRAMES)
                    for frame in pending:
                        if not await _send_one(frame):
                            break
                    pending.clear()
                    started = False
                    burst_left = 0
                    self._downlink_idle.set()
                    if sent:
                        logger.info(
                            "[english][%s] 下行发送完毕 frames=%d pacing=%.3f "
                            "prebuffer=%d lead=%.2f web=%s",
                            self.session_id,
                            sent,
                            pacing,
                            prebuffer,
                            config.ENGLISH_TTS_START_LEAD_SEC,
                            self._web_pcm_mode,
                        )
                        sent = 0
                    continue

                if not isinstance(item, (bytes, bytearray)):
                    continue

                if not started:
                    pending.append(bytes(item))
                    if len(pending) >= prebuffer:
                        await self._wait_tts_start_lead()
                        started = True
                        pacing = self._downlink_pacing_sec()
                        prebuffer = self._downlink_prebuffer_frames()
                        burst_left = max(0, config.ENGLISH_DOWNLINK_BURST_FRAMES)
                        logger.info(
                            "[english][%s] 下行开播 prebuffer=%d burst=%d pacing=%.3f",
                            self.session_id,
                            len(pending),
                            burst_left,
                            pacing,
                        )
                        for frame in pending:
                            if not await _send_one(frame):
                                break
                        pending.clear()
                else:
                    if not await _send_one(bytes(item)):
                        pending.clear()
                        started = False
                        burst_left = 0
                        self._downlink_idle.set()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("[english][%s] 下行发送循环异常: %s", self.session_id, e)
            self._downlink_idle.set()

    def _cancel_uplink_reopen(self):
        t = self._uplink_reopen_task
        self._uplink_reopen_task = None
        if t is not None and not t.done():
            t.cancel()

    async def _delayed_reopen_uplink(self, delay_sec: float):
        try:
            if delay_sec > 0:
                await asyncio.sleep(delay_sec)
            if self._cancel or self._speaking or not self._listening:
                return
            self._vad.mark_after_playback()
            self._pending_omni_transcript_since = None
            self._speech_no_text_since = None
            self._no_text_log_at_sec = -1
            self._omni_turn_has_user_text = False
            self._uplink_open = True
            self._start_silence_watchdog()
            logger.info(
                "[english][%s] 播放结束，恢复聆听与静音计时（延迟 %.2fs；"
                "%.0fs 无有效语音将 goodbye→idle）",
                self.session_id,
                delay_sec,
                config.SILENCE_TIMEOUT_SEC,
            )
        except asyncio.CancelledError:
            return

    async def _finish_speaking(self, *, aborted: bool = False):
        if not self._speaking and not self._tts_start_sent:
            return

        if aborted:
            await self._abort_downlink()
        else:
            # 等预缓冲发送队列排空，再发 tts stop（避免设备仍在播时就重开麦）
            if not self._downlink_idle.is_set():
                try:
                    await asyncio.wait_for(self._downlink_idle.wait(), timeout=45.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "[english][%s] 等待下行发送排空超时，强制结束",
                        self.session_id,
                    )
                    await self._abort_downlink()

        try:
            await self._send_json({"type": "tts", "state": "stop"})
        except Exception:  # noqa: BLE001
            pass
        self._speaking = False
        self._tts_start_sent = False
        self._tts_start_at = None
        self.encoder.reset()
        self._web_pcm_carry = b""

        if self._listening:
            self._uplink_open = False
            self._cancel_uplink_reopen()
            delay = (
                0.0
                if (aborted or self._web_pcm_mode)
                else config.ENGLISH_UPLINK_REOPEN_DELAY_SEC
            )
            self._uplink_reopen_task = asyncio.create_task(
                self._delayed_reopen_uplink(delay)
            )

    async def _cancel_speaking(self):
        self._cancel = True
        await self._abort_downlink()
        if self._omni is not None:
            try:
                await self.loop.run_in_executor(None, self._omni.cancel_response)
            except Exception:  # noqa: BLE001
                pass
        async with self._omni_lock:
            await self._finish_speaking(aborted=True)
        self._cancel = False

    async def _handle_omni_error(self, detail: str):
        low = (detail or "").lower()
        if "buffer too small" in low or "no audio" in low:
            await self._notify_too_short()
            return
        if self._is_benign_omni_error(detail):
            logger.info(
                "[english][%s] 忽略无害 Omni 错误 detail=%r",
                self.session_id, (detail or "")[:160],
            )
            return
        async with self._turn_lock:
            # 同一轮云端故障只通知一次（含 goodbye 后 MCU 自动重连触发的重复关闭）
            if self._error_notified or self._cloud_fault_latched:
                logger.info(
                    "[english][%s] 云端错误已提示过，忽略重复通知 detail=%r",
                    self.session_id, (detail or "")[:120],
                )
                return
            self._error_notified = True
            self._cloud_fault_latched = True
        await self._notify_error(ERROR_USER_ZH, detail=detail)

    async def _notify_too_short(self):
        """Web 端说话太短：友好提示，不断开 WebSocket。"""
        self._disarm_omni_response_watch()
        self._listening = False
        self._uplink_open = False
        self._stop_silence_watchdog()
        self._stop_audio_consumer()
        await self._close_omni()
        try:
            await self._send_json({
                "type": "alert",
                "status": "Too Short",
                "message": "未检测到足够语音，请按住按钮至少 1 秒再说话",
                "emotion": "neutral",
            })
        except Exception:  # noqa: BLE001
            pass

    async def _notify_error(self, speech_text: str, detail: str = ""):
        """通知 MCU / Web：云端异常。

        MCU 固件里 goodbye→idle 会 ClearChatMessages；若 alert 后立刻 goodbye，
        屏幕上一闪就没。因此先 alert，停留 ALERT_DISPLAY_SEC，再 goodbye。
        """
        detail = (detail or "").strip()
        if len(detail) > MAX_ERROR_DETAIL_LEN:
            detail = detail[: MAX_ERROR_DETAIL_LEN - 3] + "..."
        # 屏幕只用短中文，细节进日志，避免小屏刷一长串看不清
        screen_msg = speech_text or ERROR_USER_ZH
        hold_sec = max(1.0, float(config.ALERT_DISPLAY_SEC))

        logger.error(
            "[english][%s] 通知客户端云端异常（本轮仅此一次，屏显 %.1fs）"
            " screen=%r detail=%r",
            self.session_id, hold_sec, screen_msg, detail,
        )

        self._cloud_fault_latched = True
        self._cancel_create_response_kick()
        self._disarm_omni_response_watch()
        self._listening = False
        self._uplink_open = False
        self._stop_silence_watchdog()
        self._stop_audio_consumer()
        self._cancel = True
        await self._finish_speaking(aborted=True)
        await self._close_omni()
        self._cancel = False

        try:
            await self._send_json({
                "type": "alert",
                "status": ALERT_STATUS_CLOUD,
                "message": screen_msg,
                "emotion": "sad",
                # 供后续固件识别；当前官方固件尚未读此字段，实际靠延迟 goodbye 控时长
                "duration_ms": int(hold_sec * 1000),
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("[english][%s] 发送 alert 失败: %s", self.session_id, e)

        # 不发 tts start/stop：会把 MCU 切到 speaking→listening 并自动 listen start，
        # 更容易冲掉 alert。屏显靠 alert 的 SetChatMessage，停留后再 goodbye。
        try:
            await asyncio.sleep(hold_sec)
        except asyncio.CancelledError:
            pass

        try:
            await self._send_json({"type": "goodbye"})
        except Exception as e:  # noqa: BLE001
            logger.warning("[english][%s] 发送 goodbye 失败: %s", self.session_id, e)

        # 锁定期间忽略 MCU 因 goodbye 触发的自动 listen；重新唤醒（detect）后再试
        self._cloud_fault_latched = True
        self._error_notified = True

    def _stop_audio_consumer(self):
        if self._audio_task is not None and not self._audio_task.done():
            self._audio_task.cancel()
        self._audio_task = None

    def _stop_downlink_task(self):
        self._cancel_uplink_reopen()
        self._drain_downlink_queue()
        if self._downlink_task is not None and not self._downlink_task.done():
            self._downlink_task.cancel()
        self._downlink_task = None
        self._downlink_idle.set()

    def _drain_audio_queue(self):
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _close_turn(self):
        await self._cancel_speaking()
        self._stop_silence_watchdog()
        self._stop_audio_consumer()
        self._stop_downlink_task()
        self._listening = False
        await self._close_omni()

    async def _send_json(self, obj: dict):
        obj.setdefault("session_id", self.session_id)
        logger.info("[send][english][%s] %s", self.session_id, json.dumps(obj, ensure_ascii=False)[:300])
        await self.transport.send_json(obj)

    async def cleanup(self):
        await self._close_turn()
        try:
            self.transport.on_session_closed()
        except Exception:  # noqa: BLE001
            pass
