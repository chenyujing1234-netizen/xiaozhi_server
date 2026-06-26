"""流式语音识别（ASR），基于 DashScope Paraformer 实时模型。

用法：每一轮对话创建一个 AsrStream，把解码后的 16k PCM 不断喂进去，
识别到一句完整的话时通过 on_final 回调返回文本。

注意：DashScope 的回调发生在 SDK 自己的线程里，不在 asyncio 事件循环里，
所以回调里不要直接 await，调用方需要自己用 loop.call_soon_threadsafe 之类做桥接。
"""
import logging
from typing import Callable, Optional

from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult

logger = logging.getLogger("asr")


class _Callback(RecognitionCallback):
    def __init__(self, on_partial, on_final, on_error):
        self._on_partial = on_partial
        self._on_final = on_final
        self._on_error = on_error

    def on_open(self) -> None:
        logger.debug("ASR 连接已打开")

    def on_close(self) -> None:
        logger.debug("ASR 连接已关闭")

    def on_error(self, result) -> None:
        # 注意：不要对 result 直接 str()，DashScope 的 RecognitionResult.__str__
        # 在某些错误下会抛 AttributeError。这里安全地提取错误信息。
        msg = "unknown"
        try:
            msg = result.message
        except Exception:  # noqa: BLE001
            try:
                msg = repr(getattr(result, "request_id", "")) or "error"
            except Exception:  # noqa: BLE001
                msg = "error"
        logger.error("ASR 错误: %s", msg)
        if self._on_error:
            self._on_error(msg)

    def on_event(self, result: RecognitionResult) -> None:
        sentence = result.get_sentence()
        if not sentence or not isinstance(sentence, dict):
            return
        text = sentence.get("text", "")
        if not text:
            return
        if RecognitionResult.is_sentence_end(sentence):
            logger.info("ASR 一句结束: %s", text)
            if self._on_final:
                self._on_final(text)
        else:
            if self._on_partial:
                self._on_partial(text)


class AsrStream:
    def __init__(
        self,
        model: str,
        sample_rate: int,
        on_final: Callable[[str], None],
        on_partial: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ):
        self._closed = False
        self._recognition = Recognition(
            model=model,
            format="pcm",
            sample_rate=sample_rate,
            semantic_punctuation_enabled=True,
            callback=_Callback(on_partial, on_final, on_error),
        )

    def start(self) -> None:
        self._recognition.start()

    def send(self, pcm_bytes: bytes) -> None:
        if self._closed or not pcm_bytes:
            return
        try:
            self._recognition.send_audio_frame(pcm_bytes)
        except Exception as e:  # noqa: BLE001
            logger.warning("ASR 发送音频失败: %s", e)

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._recognition.stop()
        except Exception as e:  # noqa: BLE001
            logger.debug("ASR 停止异常（通常可忽略）: %s", e)
