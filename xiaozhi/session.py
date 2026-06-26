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
from .providers import asr as asr_provider
from .providers import llm as llm_provider
from .providers import tts as tts_provider

logger = logging.getLogger("session")

MAX_HISTORY_TURNS = 10  # 保留最近多少轮对话作为上下文


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

        # 录音保存（可选）
        self._save_pcm = bytearray() if config.SAVE_AUDIO else None

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
        """顺序消费上行音频：解码 -> 喂 ASR。"""
        while True:
            data = await self._audio_queue.get()
            if data is None:  # 停止信号
                break
            if self._responding or self._asr is None:
                continue
            pcm = self.decoder.decode(data)
            if not pcm:
                continue
            if self._save_pcm is not None:
                self._save_pcm.extend(pcm)
            try:
                await self.loop.run_in_executor(None, self._asr.send, pcm)
            except Exception as e:  # noqa: BLE001
                logger.debug("喂 ASR 失败: %s", e)

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

    # ---------------- 监听 / 识别 ----------------

    async def _start_listening(self):
        await self._cancel_response()
        self._stop_asr()
        self._stop_audio_consumer()
        self._drain_audio_queue()

        self._listening = True
        self._responding = False
        self._cancel = False
        if self._save_pcm is not None:
            self._save_pcm = bytearray()

        def on_final(text: str):
            asyncio.run_coroutine_threadsafe(self._on_user_final(text), self.loop)

        try:
            self._asr = asr_provider.AsrStream(
                model=config.ASR_MODEL,
                sample_rate=config.UPLINK_SAMPLE_RATE,
                on_final=on_final,
            )
            await self.loop.run_in_executor(None, self._asr.start)
            self._audio_task = asyncio.create_task(self._audio_consumer())
            logger.info("[%s] 开始监听，ASR 已启动", self.session_id)
        except Exception as e:  # noqa: BLE001
            logger.error("启动 ASR 失败: %s", e)
            self._asr = None

    async def _stop_listening(self):
        self._listening = False
        self._stop_asr()  # 停止 ASR 会触发最后一次识别结果（通过回调）

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
        if not text or self._responding:
            return
        self._responding = True
        self._listening = False
        self._stop_asr()
        self._maybe_save_audio(text)

        await self._send_json({"type": "stt", "text": text})

        self._cancel = False
        self._response_task = asyncio.create_task(self._respond(text))

    # ---------------- 回应：LLM + TTS ----------------

    async def _respond(self, user_text: str):
        try:
            self.history.append({"role": "user", "content": user_text})
            self._trim_history()

            await self._send_json({"type": "tts", "state": "start"})
            # 给设备一点时间把状态切到 speaking，否则前几帧音频会被丢弃
            await asyncio.sleep(0.2)

            splitter = SentenceSplitter()
            full_reply = ""

            queue: asyncio.Queue = asyncio.Queue()
            self.loop.run_in_executor(None, self._run_llm, list(self.history), queue)

            while True:
                delta = await queue.get()
                if delta is None:
                    break
                if self._cancel:
                    break
                full_reply += delta
                for sentence in splitter.feed(delta):
                    if self._cancel:
                        break
                    await self._speak_sentence(sentence)

            if not self._cancel:
                last = splitter.flush()
                if last:
                    await self._speak_sentence(last)

            if full_reply.strip():
                self.history.append({"role": "assistant", "content": full_reply.strip()})

        except asyncio.CancelledError:
            logger.info("[%s] 回应被取消", self.session_id)
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("回应流程异常: %s", e)
        finally:
            try:
                await self._send_json({"type": "tts", "state": "stop"})
            except Exception:  # noqa: BLE001
                pass
            self._responding = False
            logger.info("[%s] 本轮回应结束", self.session_id)

    def _run_llm(self, messages, queue: asyncio.Queue):
        try:
            for delta in llm_provider.stream_chat(messages, model=config.LLM_MODEL):
                self.loop.call_soon_threadsafe(queue.put_nowait, delta)
        except Exception as e:  # noqa: BLE001
            logger.error("LLM 线程异常: %s", e)
        finally:
            self.loop.call_soon_threadsafe(queue.put_nowait, None)

    async def _speak_sentence(self, sentence: str):
        sentence = sentence.strip()
        if not sentence or self._cancel:
            return
        logger.info("[%s] << %s", self.session_id, sentence)
        await self._send_json({"type": "tts", "state": "sentence_start", "text": sentence})

        pcm = await self.loop.run_in_executor(
            None, tts_provider.synthesize, sentence, config.TTS_MODEL, config.TTS_VOICE
        )
        if not pcm:
            return

        for frame in self.encoder.encode_pcm_stream(pcm):
            if self._cancel:
                break
            try:
                await self.transport.send_audio(frame)
            except Exception as e:  # noqa: BLE001
                logger.warning("发送音频帧失败: %s", e)
                self._cancel = True
                break
            await asyncio.sleep(config.DOWNLINK_PACING_SEC)

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
        self._stop_asr()
        self._stop_audio_consumer()
        self._listening = False

    # ---------------- 工具 ----------------

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
