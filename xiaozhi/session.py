"""单个设备连接的会话编排（与传输方式无关）。

负责把“设备音频 -> ASR -> LLM -> TTS -> 设备音频”整条链路串起来，
并维护对话状态、session_id、以及与设备之间的 JSON 控制消息时序。

传输无关：Session 只通过 transport 对象收发，不关心底层是 WebSocket 还是 MQTT+UDP。
  - transport.send_json(dict)            发送一条 JSON 控制消息
  - transport.send_audio(opus_bytes)     发送一个 Opus 音频包（下行）
  - transport.make_server_hello(sid)     生成 hello 回复（不同传输字段不同）

输入侧：
  - session.handle_text(json_str)        收到一条 JSON 控制消息
  - session.handle_binary(opus_bytes)    收到一个上行 Opus 音频包（同步入队）

线程模型：
  - 收发都在 asyncio 事件循环里。
  - DashScope 的 ASR 回调在它自己的线程里触发，用 run_coroutine_threadsafe 回流。
  - LLM（阻塞生成器）和 TTS（阻塞调用）放到线程池里执行。
"""
import asyncio
import json
import logging
import os
import time
import uuid
import wave
from typing import Optional

from config import config
from .opus_codec import OpusDecoder, OpusEncoder
from .text_utils import SentenceSplitter
from .audio_utils import is_speech, pcm_rms
from .providers import asr as asr_provider
from .providers import llm as llm_provider
from .providers import tts as tts_provider

logger = logging.getLogger("session")

MAX_HISTORY_TURNS = 10  # 保留最近多少轮对话作为上下文
ERROR_SPEECH = "抱歉，服务端出现了异常，连接已断开，请稍后再试。"
ASR_ERROR_SPEECH = "抱歉，语音识别出了问题，连接已断开，请稍后再试。"
MAX_ERROR_DETAIL_LEN = 200


class Session:
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

        # 对话历史（system + 多轮 user/assistant）
        self.history = [{"role": "system", "content": config.LLM_SYSTEM_PROMPT}]

        # 当前轮状态
        self._asr: Optional[asr_provider.AsrStream] = None
        self._listening = False
        self._responding = False
        self._response_task: Optional[asyncio.Task] = None
        self._cancel = False

        # 上行音频队列 + 顺序消费任务（保证喂给 ASR 的帧顺序正确）
        self._audio_queue: asyncio.Queue = asyncio.Queue()
        self._audio_task: Optional[asyncio.Task] = None

        # 静音超时：基于 VAD 连续静音时长（秒），不受 ASR partial 干扰
        self._quiet_since: Optional[float] = None
        self._consecutive_speech_frames: int = 0
        self._silence_watchdog: Optional[asyncio.Task] = None
        self._turn_lock = asyncio.Lock()
        self._silence_closing = False
        self._rms_log_counter: int = 0
        self._asr_feed_logged = False
        self._turn_t0: float = 0.0
        self._utterance_had_speech = False
        self._utterance_quiet_since: Optional[float] = None
        self._last_asr_partial: str = ""
        self._utterance_finalizing = False
        self._empty_utterance_streak = 0

        # 录音保存（可选）
        self._save_pcm = bytearray() if config.SAVE_AUDIO else None
        self._error_notified = False

    # ---------------- 消息分发 ----------------

    async def handle_text(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("收到非法 JSON: %s", raw[:200])
            return
        msg_type = msg.get("type")
        logger.info("[recv][%s] %s", self.session_id, raw[:300])

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
        """设备上行音频帧（已是裸 Opus 包）。同步入队，由消费任务顺序处理。"""
        if not self._listening or self._responding:
            return
        try:
            self._audio_queue.put_nowait(data)
        except asyncio.QueueFull:
            pass

    async def _audio_consumer(self):
        """顺序消费上行音频：VAD 门控 -> 仅有效语音送 ASR。"""
        while True:
            data = await self._audio_queue.get()
            if data is None:
                break
            if self._responding or not self._listening or self._asr is None:
                continue

            pcm = self.decoder.decode(data)
            if not pcm:
                continue

            rms = pcm_rms(pcm)
            if config.LOG_AUDIO_RMS:
                self._rms_log_counter += 1
                if self._rms_log_counter % 17 == 0:
                    quiet_sec = (
                        time.time() - self._quiet_since
                        if self._quiet_since is not None
                        else 0.0
                    )
                    logger.info(
                        "[%s] 上行 RMS=%.0f 阈值=%.0f 连续语音帧=%d 静音=%.0fs",
                        self.session_id,
                        rms,
                        config.SPEECH_RMS_THRESHOLD,
                        self._consecutive_speech_frames,
                        quiet_sec,
                    )

            if is_speech(pcm, config.SPEECH_RMS_THRESHOLD):
                self._consecutive_speech_frames += 1
                if self._consecutive_speech_frames >= config.SPEECH_FRAMES_REQUIRED:
                    self._quiet_since = None
                    self._utterance_quiet_since = None
            else:
                if self._consecutive_speech_frames >= config.SPEECH_FRAMES_REQUIRED:
                    now = time.time()
                    self._quiet_since = now
                    if self._utterance_had_speech and self._utterance_quiet_since is None:
                        self._utterance_quiet_since = now
                self._consecutive_speech_frames = 0

            if self._consecutive_speech_frames < config.SPEECH_FRAMES_REQUIRED:
                continue

            if not self._asr_feed_logged:
                self._asr_feed_logged = True
                self._utterance_had_speech = True
                self._turn_t0 = time.time()
                logger.info(
                    "[pipeline][%s] ② 检测到有效语音，开始送入 ASR",
                    self.session_id,
                )

            if self._save_pcm is not None:
                self._save_pcm.extend(pcm)
            try:
                await self.loop.run_in_executor(None, self._asr.send, pcm)
            except Exception as e:  # noqa: BLE001
                logger.debug("喂 ASR 失败: %s", e)

    async def _silence_watchdog_loop(self):
        """检查：① 说完静音 → 送 LLM；② 长期无语音 → goodbye。"""
        try:
            while self._listening and self._asr is not None:
                await asyncio.sleep(0.3)
                if not self._listening or self._responding or self._asr is None:
                    break
                now = time.time()
                if (
                    self._utterance_had_speech
                    and self._utterance_quiet_since is not None
                    and not self._utterance_finalizing
                    and now - self._utterance_quiet_since >= config.UTTERANCE_END_SILENCE_SEC
                ):
                    await self._finalize_utterance()
                    break
                if self._quiet_since is None:
                    continue
                if now - self._quiet_since >= config.SILENCE_TIMEOUT_SEC:
                    await self._on_silence_timeout()
                    break
        except asyncio.CancelledError:
            pass

    async def _finalize_utterance(self):
        """VAD 判定用户说完：停止 ASR，用最新识别文本送 LLM。"""
        async with self._turn_lock:
            if self._responding or not self._listening or self._utterance_finalizing:
                return
            if not self._utterance_had_speech:
                return
            self._utterance_finalizing = True
            partial = self._last_asr_partial.strip()

        quiet = time.time() - (self._utterance_quiet_since or time.time())
        logger.info(
            "[pipeline][%s] ②→③ 说完静音 %.1fs，结束 ASR（partial=%s）",
            self.session_id,
            quiet,
            partial or "(空)",
        )

        self._stop_asr()
        await asyncio.sleep(0.3)

        async with self._turn_lock:
            if self._responding:
                return

        if partial:
            async with self._turn_lock:
                self._empty_utterance_streak = 0
            await self._on_user_final(partial)
        else:
            async with self._turn_lock:
                self._empty_utterance_streak += 1
                streak = self._empty_utterance_streak
                self._utterance_finalizing = False
                self._utterance_had_speech = False
                self._utterance_quiet_since = None
                self._asr_feed_logged = False
                self._last_asr_partial = ""

            if streak >= config.EMPTY_UTTERANCE_LIMIT:
                logger.info(
                    "[%s] 连续 %d 次说完但无识别文本，视为静音，通知设备 idle",
                    self.session_id,
                    streak,
                )
                await self._send_idle_goodbye()
            else:
                logger.info(
                    "[%s] 说完但无识别文本 (%d/%d)，重新开启 ASR",
                    self.session_id,
                    streak,
                    config.EMPTY_UTTERANCE_LIMIT,
                )
                await self._reopen_asr()

    async def _on_silence_timeout(self):
        """静音超时：仅在没有待处理识别/回应时关闭通道。"""
        async with self._turn_lock:
            if self._responding or not self._listening:
                return
            if self._quiet_since is None:
                return
            if time.time() - self._quiet_since < config.SILENCE_TIMEOUT_SEC:
                return
            # 注意：不能在这里 _stop_silence_watchdog()——本函数运行在 watchdog 任务内，cancel 会中断自身
            self._silence_closing = True
            self._stop_asr()
            self._stop_audio_consumer()

        # 释放锁后再等 ASR stop 可能触发的 final 回调
        await asyncio.sleep(1.0)

        async with self._turn_lock:
            if self._responding:
                logger.info(
                    "[%s] 静音超时期间收到识别结果，取消 goodbye，继续 LLM",
                    self.session_id,
                )
                self._silence_closing = False
                return

            logger.info(
                "[%s] 静音超时 %.0fs，关闭 ASR 并通知设备退出聆听",
                self.session_id,
                config.SILENCE_TIMEOUT_SEC,
            )
            self._listening = False
            self._silence_closing = False

        try:
            await self._send_json({"type": "goodbye"})
        except Exception as e:  # noqa: BLE001
            logger.warning("发送 goodbye 失败: %s", e)

    async def _send_idle_goodbye(self):
        """下发 goodbye，让客户端退出「聆听中」回到 idle。"""
        self._stop_silence_watchdog()
        self._stop_asr()
        self._stop_audio_consumer()
        self._listening = False
        self._utterance_finalizing = False
        try:
            await self._send_json({"type": "goodbye"})
        except Exception as e:  # noqa: BLE001
            logger.warning("发送 goodbye 失败: %s", e)

    # ---------------- 各类控制消息 ----------------

    async def _on_hello(self, msg: dict):
        reply = self.transport.make_server_hello(self.session_id)
        await self._send_json(reply)
        logger.info("已回复 hello, session_id=%s", self.session_id)

    async def _on_listen(self, msg: dict):
        state = msg.get("state")
        if state == "start":
            await self._start_listening()
        elif state == "stop":
            await self._stop_listening()
        elif state == "detect":
            wake = msg.get("text", "")
            logger.info("唤醒词检测: %s", wake)

    async def _on_abort(self, msg: dict):
        logger.info("收到 abort, reason=%s", msg.get("reason"))
        await self._cancel_response()

    def _make_asr_callbacks(self):
        def on_partial(text: str):
            self._last_asr_partial = text
            logger.info("[pipeline][%s] ② ASR 识别中: %s", self.session_id, text)

        def on_final(text: str):
            asyncio.run_coroutine_threadsafe(self._on_user_final(text), self.loop)

        def on_error(msg: str):
            asyncio.run_coroutine_threadsafe(self._handle_asr_error(msg), self.loop)

        return on_partial, on_final, on_error

    async def _handle_asr_error(self, detail: str):
        async with self._turn_lock:
            if self._error_notified or self._responding:
                return
            self._error_notified = True
            self._listening = False
            self._utterance_finalizing = False
        self._stop_silence_watchdog()
        self._stop_asr()
        self._stop_audio_consumer()
        await self._notify_error(ASR_ERROR_SPEECH, detail=f"ASR: {detail}")

    async def _handle_pipeline_error(self, detail: str, tts_started: bool = False):
        async with self._turn_lock:
            if self._error_notified:
                return
            self._error_notified = True
        self._cancel = True
        if self.history and self.history[-1].get("role") == "user":
            self.history.pop()
        await self._notify_error(
            ERROR_SPEECH,
            detail=detail,
            disconnect=True,
            tts_started=tts_started,
        )

    # ---------------- 监听 / 识别 ----------------

    async def _start_listening(self):
        await self._cancel_response()
        self._stop_asr()
        self._stop_audio_consumer()
        self._drain_audio_queue()

        self._listening = True
        self._responding = False
        self._cancel = False
        self._silence_closing = False
        self._quiet_since = time.time()
        self._consecutive_speech_frames = 0
        self._rms_log_counter = 0
        self._asr_feed_logged = False
        self._utterance_had_speech = False
        self._utterance_quiet_since = None
        self._last_asr_partial = ""
        self._utterance_finalizing = False
        self._empty_utterance_streak = 0
        self._error_notified = False
        if self._save_pcm is not None:
            self._save_pcm = bytearray()

        on_partial, on_final, on_error = self._make_asr_callbacks()

        try:
            self._asr = asr_provider.AsrStream(
                model=config.ASR_MODEL,
                sample_rate=config.UPLINK_SAMPLE_RATE,
                on_final=on_final,
                on_partial=on_partial,
                on_error=on_error,
            )
            await self.loop.run_in_executor(None, self._asr.start)
            self._audio_task = asyncio.create_task(self._audio_consumer())
            self._start_silence_watchdog()
            logger.info(
                "[pipeline][%s] ① 开始聆听，ASR 已连接 (model=%s)",
                self.session_id,
                config.ASR_MODEL,
            )
            logger.info(
                "[%s] 开始监听，ASR 已启动（句末静音=%ss，会话静音=%ss，能量阈值=%s）",
                self.session_id,
                config.UTTERANCE_END_SILENCE_SEC,
                config.SILENCE_TIMEOUT_SEC,
                config.SPEECH_RMS_THRESHOLD,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("启动 ASR 失败: %s", e)
            self._asr = None
            await self._handle_asr_error(f"启动失败: {e}")

    async def _reopen_asr(self):
        """句末判定后无文本时，重新建立 ASR 连接（仍在同一次 listen 内）。"""
        if not self._listening or self._responding:
            return

        on_partial, on_final, on_error = self._make_asr_callbacks()

        try:
            self._asr = asr_provider.AsrStream(
                model=config.ASR_MODEL,
                sample_rate=config.UPLINK_SAMPLE_RATE,
                on_final=on_final,
                on_partial=on_partial,
                on_error=on_error,
            )
            await self.loop.run_in_executor(None, self._asr.start)
            if self._audio_task is None or self._audio_task.done():
                self._audio_task = asyncio.create_task(self._audio_consumer())
            self._start_silence_watchdog()
            logger.info("[pipeline][%s] ASR 已重新连接", self.session_id)
        except Exception as e:  # noqa: BLE001
            logger.error("重新开启 ASR 失败: %s", e)
            self._asr = None
            await self._handle_asr_error(f"重新连接失败: {e}")

    async def _stop_listening(self):
        self._listening = False
        self._stop_silence_watchdog()
        self._stop_asr()  # 停止 ASR 会触发最后一次识别结果（通过回调）

    def _start_silence_watchdog(self):
        self._stop_silence_watchdog()
        self._silence_watchdog = asyncio.create_task(self._silence_watchdog_loop())

    def _stop_silence_watchdog(self):
        task = self._silence_watchdog
        if task is not None and not task.done():
            task.cancel()
        self._silence_watchdog = None

    def _stop_asr(self):
        if self._asr is not None:
            try:
                self._asr.stop()
            except Exception:  # noqa: BLE001
                pass
            self._asr = None

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

    async def _on_user_final(self, text: str):
        text = (text or "").strip()
        async with self._turn_lock:
            if not text or self._responding or self._error_notified:
                return
            if self._silence_closing:
                logger.info(
                    "[%s] 静音超时期间收到 ASR 结果，改走 LLM: %s",
                    self.session_id,
                    text,
                )
                self._silence_closing = False
            self._empty_utterance_streak = 0
            self._responding = True
            self._listening = False
            self._stop_silence_watchdog()
            if self._asr is not None:
                self._stop_asr()
            self._stop_audio_consumer()
            self._maybe_save_audio(text)

        elapsed = time.time() - self._turn_t0 if self._turn_t0 else 0
        logger.info(
            "[pipeline][%s] ③ ASR 识别完成 (%.1fs): %s",
            self.session_id,
            elapsed,
            text,
        )

        await self._send_json({"type": "stt", "text": text})

        self._cancel = False
        self._response_task = asyncio.create_task(self._respond(text))

    # ---------------- 回应：LLM + TTS ----------------

    async def _respond(self, user_text: str):
        t0 = time.time()
        try:
            self.history.append({"role": "user", "content": user_text})
            self._trim_history()

            logger.info(
                "[pipeline][%s] ④ 调用 LLM (model=%s)，用户: %s",
                self.session_id,
                config.LLM_MODEL,
                user_text,
            )

            await self._send_json({"type": "tts", "state": "start"})
            await asyncio.sleep(0.2)

            splitter = SentenceSplitter(min_chars=config.TTS_SPLIT_MIN_CHARS)
            full_reply = ""
            llm_started = False
            llm_queue: asyncio.Queue = asyncio.Queue()
            tts_text_queue: asyncio.Queue = asyncio.Queue()
            llm_error: list = [None]

            async def _llm_reader():
                nonlocal full_reply, llm_started
                self.loop.run_in_executor(
                    None, self._run_llm, list(self.history), llm_queue, llm_error
                )
                try:
                    while True:
                        delta = await llm_queue.get()
                        if delta is None:
                            break
                        if self._cancel:
                            break
                        if not llm_started:
                            llm_started = True
                            logger.info(
                                "[pipeline][%s] ⑤ LLM 开始流式输出 (首 token %.1fs)",
                                self.session_id,
                                time.time() - t0,
                            )
                        full_reply += delta
                        for sentence in splitter.feed(delta):
                            if self._cancel:
                                break
                            await tts_text_queue.put(sentence)
                            logger.info(
                                "[pipeline][%s] ⑤→⑦ 分句就绪 (%.1fs, %d字): %s",
                                self.session_id,
                                time.time() - t0,
                                len(sentence),
                                sentence,
                            )
                    if self._cancel:
                        return
                    if llm_error[0]:
                        await self._handle_pipeline_error(
                            f"LLM 错误: {llm_error[0]}", tts_started=True
                        )
                        return
                    if not self._cancel:
                        last = splitter.flush()
                        if last:
                            await tts_text_queue.put(last)
                            logger.info(
                                "[pipeline][%s] ⑤→⑦ 末句就绪 (%.1fs): %s",
                                self.session_id,
                                time.time() - t0,
                                last,
                            )
                    if not llm_started:
                        await self._handle_pipeline_error(
                            "LLM 无有效输出", tts_started=True
                        )
                finally:
                    await tts_text_queue.put(None)

            async def _tts_player():
                """与 LLM 读取并行；播放上一句时预合成下一句。"""
                synth_task = None
                synth_sentence = ""
                while True:
                    sentence = await tts_text_queue.get()
                    if sentence is None:
                        if synth_task and not self._cancel:
                            await self._play_pcm(
                                synth_sentence, await synth_task, t0
                            )
                        break
                    if self._cancel:
                        continue
                    if synth_task is not None:
                        pcm = await synth_task
                        next_synth = asyncio.create_task(
                            self._synthesize_pcm(sentence)
                        )
                        await self._play_pcm(synth_sentence, pcm, t0)
                        synth_task = next_synth
                        synth_sentence = sentence
                    else:
                        synth_task = asyncio.create_task(
                            self._synthesize_pcm(sentence)
                        )
                        synth_sentence = sentence

            await asyncio.gather(_llm_reader(), _tts_player())

            if full_reply.strip() and not self._error_notified:
                self.history.append({"role": "assistant", "content": full_reply.strip()})
                logger.info(
                    "[pipeline][%s] ⑥ LLM 完成 (%.1fs)，回复: %s",
                    self.session_id,
                    time.time() - t0,
                    full_reply.strip(),
                )

        except asyncio.CancelledError:
            logger.info("[pipeline][%s] 回应被取消", self.session_id)
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("[pipeline][%s] 回应流程异常: %s", self.session_id, e)
            await self._handle_pipeline_error(str(e), tts_started=True)
        finally:
            try:
                await self._send_json({"type": "tts", "state": "stop"})
            except Exception:  # noqa: BLE001
                pass
            self._responding = False
            logger.info(
                "[pipeline][%s] ⑨ 本轮对话结束 (总耗时 %.1fs)",
                self.session_id,
                time.time() - t0,
            )

    def _run_llm(self, messages, queue: asyncio.Queue, llm_error: list):
        try:
            for delta in llm_provider.stream_chat(messages, model=config.LLM_MODEL):
                self.loop.call_soon_threadsafe(queue.put_nowait, delta)
        except llm_provider.LlmError as e:
            llm_error[0] = str(e)
            logger.error("LLM 错误: %s", e)
        except Exception as e:  # noqa: BLE001
            llm_error[0] = str(e)
            logger.error("LLM 线程异常: %s", e)
        finally:
            self.loop.call_soon_threadsafe(queue.put_nowait, None)

    async def _synthesize_pcm(self, sentence: str) -> bytes:
        return await self.loop.run_in_executor(
            None, tts_provider.synthesize, sentence, config.TTS_MODEL, config.TTS_VOICE
        )

    async def _play_pcm(self, sentence: str, pcm: bytes, t0: float):
        sentence = sentence.strip()
        if not sentence or self._cancel or not pcm:
            if sentence and not pcm:
                logger.warning("[pipeline][%s] ⑦ TTS 合成失败，跳过: %s", self.session_id, sentence)
            return
        logger.info(
            "[pipeline][%s] ⑦ TTS 播放 (%.1fs): %s",
            self.session_id,
            time.time() - t0 if t0 else 0,
            sentence,
        )
        await self._send_json({"type": "tts", "state": "sentence_start", "text": sentence})

        t_tts = time.time()
        frame_count = 0
        for frame in self.encoder.encode_pcm_stream(pcm):
            if self._cancel:
                break
            try:
                await self.transport.send_audio(frame)
                frame_count += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("发送音频帧失败: %s", e)
                self._cancel = True
                break
            await asyncio.sleep(config.DOWNLINK_PACING_SEC)

        logger.info(
            "[pipeline][%s] ⑧ 下行音频已发送 %d 帧 (pcm=%d bytes, tts=%.1fs): %s",
            self.session_id,
            frame_count,
            len(pcm),
            time.time() - t_tts,
            sentence,
        )

    async def _speak_sentence(self, sentence: str, t0: float = 0):
        sentence = sentence.strip()
        if not sentence or self._cancel:
            return
        logger.info(
            "[pipeline][%s] ⑦ TTS 合成 (model=%s, voice=%s, %.1fs): %s",
            self.session_id,
            config.TTS_MODEL,
            config.TTS_VOICE,
            time.time() - t0 if t0 else 0,
            sentence,
        )
        pcm = await self._synthesize_pcm(sentence)
        await self._play_pcm(sentence, pcm, t0)

    async def _cancel_response(self):
        self._cancel = True
        task = self._response_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._response_task = None
        self._responding = False

    async def _close_turn(self):
        await self._cancel_response()
        self._stop_silence_watchdog()
        self._stop_asr()
        self._stop_audio_consumer()
        self._listening = False

    # ---------------- 工具 ----------------

    def _truncate_detail(self, detail: str) -> str:
        detail = (detail or "").strip()
        if len(detail) <= MAX_ERROR_DETAIL_LEN:
            return detail
        return detail[: MAX_ERROR_DETAIL_LEN - 3] + "..."

    async def _notify_error(
        self,
        speech_text: str,
        detail: str = "",
        disconnect: bool = True,
        tts_started: bool = False,
    ):
        """TTS 语音播报 + 屏幕 alert + 可选 goodbye 断开。"""
        detail = self._truncate_detail(detail)
        logger.error(
            "[%s] 通知设备错误 speech=%r detail=%r disconnect=%s",
            self.session_id,
            speech_text,
            detail,
            disconnect,
        )

        self._stop_silence_watchdog()
        self._stop_asr()
        self._stop_audio_consumer()
        self._listening = False
        self._utterance_finalizing = False

        try:
            await self._send_json({
                "type": "alert",
                "status": "服务端异常",
                "message": detail or speech_text,
                "emotion": "sad",
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("发送 alert 失败: %s", e)

        saved_cancel = self._cancel
        self._cancel = False
        try:
            if not tts_started:
                await self._send_json({"type": "tts", "state": "start"})
                await asyncio.sleep(0.2)
            await self._speak_sentence(speech_text)
            if not tts_started:
                await self._send_json({"type": "tts", "state": "stop"})
        except Exception as e:  # noqa: BLE001
            logger.warning("错误语音提示失败: %s", e)
            if not tts_started:
                try:
                    await self._send_json({"type": "tts", "state": "stop"})
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self._cancel = saved_cancel

        if disconnect:
            try:
                await self._send_json({"type": "goodbye"})
            except Exception as e:  # noqa: BLE001
                logger.warning("发送 goodbye 失败: %s", e)

    async def _send_json(self, obj: dict):
        obj.setdefault("session_id", self.session_id)
        logger.info("[send][%s] %s", self.session_id, json.dumps(obj, ensure_ascii=False)[:300])
        await self.transport.send_json(obj)

    def _trim_history(self):
        max_msgs = 1 + MAX_HISTORY_TURNS * 2
        if len(self.history) > max_msgs:
            self.history = [self.history[0]] + self.history[-(max_msgs - 1):]

    def _maybe_save_audio(self, text: str):
        if self._save_pcm is None or len(self._save_pcm) == 0:
            return
        try:
            os.makedirs(config.SAVE_AUDIO_DIR, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(config.SAVE_AUDIO_DIR, f"{ts}_{self.session_id}.wav")
            with wave.open(path, "wb") as wf:
                wf.setnchannels(config.CHANNELS)
                wf.setsampwidth(2)
                wf.setframerate(config.UPLINK_SAMPLE_RATE)
                wf.writeframes(bytes(self._save_pcm))
            logger.info("已保存上行音频: %s (识别=%s)", path, text)
        except Exception as e:  # noqa: BLE001
            logger.warning("保存音频失败: %s", e)

    async def cleanup(self):
        await self._close_turn()
        try:
            self.transport.on_session_closed()
        except Exception:  # noqa: BLE001
            pass
