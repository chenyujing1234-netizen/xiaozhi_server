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
from .audio_utils import is_speech
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
    refine_profile,
)
from .opus_codec import OpusDecoder, OpusEncoder

logger = logging.getLogger("english")

ERROR_SPEECH = "Sorry, the server encountered an error. The connection will close."
MAX_ERROR_DETAIL_LEN = 200
# Web 按住说话：至少约 180ms 有效 PCM 才允许 commit（3 帧 × 60ms）
MIN_WEB_OMNI_PCM_BYTES = 5760


class _OmniBridge(OmniRealtimeCallback):
    def __init__(self, session: "EnglishSession"):
        self._session = session

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
        self._play_task: Optional[asyncio.Task] = None
        self._audio_queue: asyncio.Queue = asyncio.Queue()
        self._audio_task: Optional[asyncio.Task] = None
        self._turn_lock = asyncio.Lock()
        self._omni_lock = asyncio.Lock()
        self._quiet_since: Optional[float] = None
        self._consecutive_speech_frames: int = 0
        self._silence_watchdog: Optional[asyncio.Task] = None
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
        self._tutor_stream_buf = ""
        self._tts_start_sent = False
        self._turn_saved = False
        self._profile_task: Optional[asyncio.Task] = None
        self._history_task: Optional[asyncio.Task] = None
        self._diagnosis_task: Optional[asyncio.Task] = None

    # ---------------- 消息分发 ----------------

    async def handle_text(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("收到非法 JSON: %s", raw[:200])
            return
        msg_type = msg.get("type")
        logger.info("[recv][english][%s] %s", self.session_id, raw[:300])

        if msg_type == "hello":
            await self._on_hello(msg)
        elif msg_type == "listen":
            await self._on_listen(msg)
        elif msg_type == "abort":
            await self._on_abort(msg)
        elif msg_type == "goodbye":
            await self._close_turn()
        elif msg_type == "mcp":
            logger.debug("收到 MCP 消息（已忽略）")
        else:
            logger.debug("未处理的消息类型: %s", msg_type)

    def handle_binary(self, data: bytes):
        if not self._listening or self._speaking or not self._uplink_open:
            return
        try:
            self._audio_queue.put_nowait(data)
        except asyncio.QueueFull:
            pass

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
            self._update_vad(pcm)
            await self._feed_pcm_to_omni(pcm)

    async def _feed_pcm_to_omni(self, pcm: bytes) -> bool:
        if not pcm or self._omni is None:
            return False
        try:
            b64 = base64.b64encode(pcm).decode("ascii")
            await self.loop.run_in_executor(None, self._omni.append_audio, b64)
            self._omni_pcm_bytes += len(pcm)
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug("喂 Omni 失败: %s", e)
            return False

    def _update_vad(self, pcm: bytes):
        """本地 VAD：用于静音超时，与 Omni server_vad 独立。"""
        if is_speech(pcm, config.SPEECH_RMS_THRESHOLD):
            self._consecutive_speech_frames += 1
            if self._consecutive_speech_frames >= config.SPEECH_FRAMES_REQUIRED:
                self._quiet_since = None
        else:
            if self._consecutive_speech_frames >= config.SPEECH_FRAMES_REQUIRED:
                self._quiet_since = time.time()
            self._consecutive_speech_frames = 0

    async def _silence_watchdog_loop(self):
        try:
            while self._listening:
                await asyncio.sleep(0.3)
                if not self._listening or self._speaking:
                    continue
                if self._quiet_since is None:
                    continue
                if time.time() - self._quiet_since >= config.SILENCE_TIMEOUT_SEC:
                    await self._on_silence_timeout()
                    break
        except asyncio.CancelledError:
            pass

    async def _on_silence_timeout(self):
        if self._speaking or not self._listening:
            return
        if self._quiet_since is None:
            return
        if time.time() - self._quiet_since < config.SILENCE_TIMEOUT_SEC:
            return

        logger.info(
            "[english][%s] 静音超时 %.0fs，关闭 Omni 并通知设备退出聆听",
            self.session_id,
            config.SILENCE_TIMEOUT_SEC,
        )
        self._listening = False
        self._stop_silence_watchdog()
        self._stop_audio_consumer()
        await self._close_omni()
        try:
            await self._send_json({"type": "goodbye"})
        except Exception as e:  # noqa: BLE001
            logger.warning("[english] 发送 goodbye 失败: %s", e)

    def _start_silence_watchdog(self):
        self._stop_silence_watchdog()
        self._silence_watchdog = asyncio.create_task(self._silence_watchdog_loop())

    def _stop_silence_watchdog(self):
        task = self._silence_watchdog
        if task is not None and not task.done():
            task.cancel()
        self._silence_watchdog = None

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
            logger.info("[english] 唤醒词: %s", msg.get("text", ""))

    async def _on_abort(self, msg: dict):
        logger.info("[english] 收到 abort, reason=%s", msg.get("reason"))
        # 先置取消，再抢 omni 锁，确保正在节流发送的音频循环能尽快退出
        self._cancel = True
        if self._omni is not None:
            try:
                await self.loop.run_in_executor(None, self._omni.cancel_response)
            except Exception:  # noqa: BLE001
                pass
        async with self._omni_lock:
            await self._finish_speaking()
        self._cancel = False
        logger.info("[english][%s] abort 处理完成", self.session_id)

    async def _start_listening(self):
        if self._speaking:
            logger.info("[english][%s] 仍在播放，忽略 listen start", self.session_id)
            return
        await self._cancel_speaking()
        await self._close_omni()
        self._drain_audio_queue()

        self._listening = True
        self._speaking = False
        self._cancel = False
        self._error_notified = False
        self._quiet_since = time.time()
        self._consecutive_speech_frames = 0
        self._uplink_open = True
        self._omni_pcm_bytes = 0

        try:
            await self._open_omni()
            self._audio_task = asyncio.create_task(self._audio_consumer())
            self._start_silence_watchdog()
            logger.info(
                "[english][%s] 开始聆听，Omni 已连接 model=%s voice=%s",
                self.session_id,
                config.ENGLISH_OMNI_MODEL,
                config.ENGLISH_OMNI_VOICE,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("[english] 启动 Omni 失败: %s", e)
            await self._notify_error(ERROR_SPEECH, detail=f"Omni: {e}")

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
            await self.loop.run_in_executor(None, self._omni.commit)
            await self.loop.run_in_executor(None, self._omni.create_response)
            logger.info(
                "[english][%s] Web 松手，已 commit + create_response（%d bytes），等待 Omni 回复",
                self.session_id, self._omni_pcm_bytes,
            )
        except Exception as e:  # noqa: BLE001
            err = str(e).lower()
            if "buffer too small" in err or "no audio" in err:
                await self._notify_too_short()
            else:
                logger.warning("[english] commit/create_response 失败: %s", e)
                await self._notify_error(ERROR_SPEECH, detail=f"commit: {e}")

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
        return build_instructions(
            self._profile,
            history_context=self._history_context,
        )

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

        def _configure():
            conv.update_session(
                output_modalities=[MultiModality.AUDIO, MultiModality.TEXT],
                voice=config.ENGLISH_OMNI_VOICE,
                input_audio_format=AudioFormat.PCM_16000HZ_MONO_16BIT,
                output_audio_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
                enable_turn_detection=True,
                turn_detection_type="server_vad",
                instructions=instructions,
            )

        await self.loop.run_in_executor(None, _configure)
        self._omni = conv
        hist_chars = len(self._history_context or "")
        logger.info(
            "[english][%s] 画像已注入 turns=%d refine_at=%d history_chars=%d text=%s",
            self.session_id,
            self._profile.turn_count,
            self._profile.last_refine_turn,
            hist_chars,
            (self._profile.profile_text or "")[:120],
        )

    async def _refresh_omni_instructions(self):
        """画像/历史变更后热更新当前 Omni session（若仍连接）。"""
        omni = self._omni
        if omni is None:
            return
        instructions = self._build_session_instructions()

        def _update():
            omni.update_session(
                output_modalities=[MultiModality.AUDIO, MultiModality.TEXT],
                voice=config.ENGLISH_OMNI_VOICE,
                input_audio_format=AudioFormat.PCM_16000HZ_MONO_16BIT,
                output_audio_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
                enable_turn_detection=True,
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
        if omni is None:
            return
        try:
            await self.loop.run_in_executor(None, omni.close)
        except Exception:  # noqa: BLE001
            pass

    async def _on_omni_event(self, event: dict):
        etype = event.get("type", "")
        # 字幕增量不走音频锁：避免被下行音频节流拖住，页面可即时刷新
        if etype == "response.audio_transcript.delta":
            try:
                await self._on_tutor_transcript_delta(event)
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "[english][%s] 处理字幕增量异常: %s", self.session_id, e
                )
            return
        async with self._omni_lock:
            await self._dispatch_omni_event(event)

    async def _ensure_tts_start(self):
        if self._tts_start_sent:
            return
        self._tts_start_sent = True
        await self._send_json({"type": "tts", "state": "start"})

    async def _on_tutor_transcript_delta(self, event: dict):
        delta = event.get("delta") or ""
        if not delta:
            return
        # 文本可能早于首包音频到达：先开字幕区
        await self._ensure_tts_start()
        self._tutor_stream_buf += delta
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
                self._tutor_stream_buf = ""
                self._last_tutor_text = ""
                self._tts_start_sent = False

            elif etype == "conversation.item.input_audio_transcription.completed":
                text = event.get("transcript", "")
                if text:
                    logger.info("[english][%s] 用户说: %s", self.session_id, text)
                    self._last_user_text = text
                    self._last_tutor_text = ""
                    self._tutor_stream_buf = ""
                    self._tts_start_sent = False
                    self._turn_saved = False
                    await self._send_json({"type": "stt", "text": text})
                    # 显式偏好：异步立刻沉淀画像，避免阻塞 Omni 事件循环
                    if looks_like_preference_request(text):
                        self._schedule_explicit_profile_refine(text)

            elif etype == "response.audio.delta":
                delta_b64 = event.get("delta", "")
                if not delta_b64:
                    return
                pcm = base64.b64decode(delta_b64)
                if not self._speaking:
                    self._speaking = True
                    self.encoder.reset()
                    self._web_pcm_carry = b""
                    await self._ensure_tts_start()
                    await asyncio.sleep(0.2)
                await self._send_pcm_frames(pcm, flush=False)

            elif etype == "response.audio_transcript.done":
                text = event.get("transcript", "") or self._tutor_stream_buf
                if text:
                    logger.info("[english][%s] 导师回复: %s", self.session_id, text)
                    self._last_tutor_text = text
                    self._tutor_stream_buf = text
                    await self._send_json({
                        "type": "tts",
                        "state": "sentence_start",
                        "text": text,
                    })

            elif etype in ("response.audio.done", "response.done"):
                if self._speaking:
                    await self._send_pcm_frames(b"", flush=True)
                if etype == "response.done":
                    await self._finish_speaking()
                    self._schedule_history_save()
                    self._schedule_profile_update()
                    self._schedule_diagnosis()

            elif etype == "error":
                err = event.get("error", {})
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                logger.error("[english][%s] Omni 错误: %s", self.session_id, msg)
                await self._handle_omni_error(msg)

        except Exception as e:  # noqa: BLE001
            logger.exception("[english][%s] 处理 Omni 事件异常: %s", self.session_id, e)

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

    async def _send_pcm_frames(self, pcm: bytes, *, flush: bool):
        frame_count = 0
        frames = (
            self._iter_web_pcm_frames(pcm, flush=flush)
            if self._web_pcm_mode
            else self.encoder.encode_pcm_stream(pcm, flush=flush)
        )
        for frame in frames:
            if self._cancel:
                break
            try:
                await self.transport.send_audio(frame)
                frame_count += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("[english] 发送音频帧失败: %s", e)
                break
            if self._cancel:
                break
            await asyncio.sleep(config.DOWNLINK_PACING_SEC)
        if frame_count:
            logger.debug("[english][%s] 下行发送 %d 帧 flush=%s", self.session_id, frame_count, flush)

    async def _finish_speaking(self):
        if not self._speaking and not self._tts_start_sent:
            return
        try:
            await self._send_json({"type": "tts", "state": "stop"})
        except Exception:  # noqa: BLE001
            pass
        self._speaking = False
        self._tts_start_sent = False
        if self._listening:
            self._quiet_since = time.time()
            self._consecutive_speech_frames = 0
            self._uplink_open = True
            if self._web_pcm_mode:
                self._start_silence_watchdog()

    async def _cancel_speaking(self):
        self._cancel = True
        if self._omni is not None:
            try:
                await self.loop.run_in_executor(None, self._omni.cancel_response)
            except Exception:  # noqa: BLE001
                pass
        async with self._omni_lock:
            await self._finish_speaking()
        self._cancel = False

    async def _handle_omni_error(self, detail: str):
        low = (detail or "").lower()
        if "buffer too small" in low or "no audio" in low:
            await self._notify_too_short()
            return
        async with self._turn_lock:
            if self._error_notified:
                return
            self._error_notified = True
        await self._notify_error(ERROR_SPEECH, detail=detail)

    async def _notify_too_short(self):
        """Web 端说话太短：友好提示，不断开 WebSocket。"""
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
        detail = (detail or "").strip()
        if len(detail) > MAX_ERROR_DETAIL_LEN:
            detail = detail[: MAX_ERROR_DETAIL_LEN - 3] + "..."

        self._listening = False
        self._stop_silence_watchdog()
        self._stop_audio_consumer()
        await self._close_omni()
        await self._finish_speaking()

        try:
            await self._send_json({
                "type": "alert",
                "status": "Server Error",
                "message": detail or speech_text,
                "emotion": "sad",
            })
        except Exception:  # noqa: BLE001
            pass

        saved_cancel = self._cancel
        self._cancel = False
        try:
            await self._send_json({"type": "tts", "state": "start"})
            await asyncio.sleep(0.2)
            # 错误提示仍走 CosyVoice 会复杂；此处仅发文本字幕，音频由 alert 振动音提示
            await self._send_json({
                "type": "tts",
                "state": "sentence_start",
                "text": speech_text,
            })
            await self._send_json({"type": "tts", "state": "stop"})
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._cancel = saved_cancel

        try:
            await self._send_json({"type": "goodbye"})
        except Exception:  # noqa: BLE001
            pass

    def _stop_audio_consumer(self):
        if self._audio_task is not None and not self._audio_task.done():
            self._audio_task.cancel()
        self._audio_task = None

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
