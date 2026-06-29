"""大模型对话（LLM），基于 DashScope 的通义千问 Qwen 系列。

提供流式输出：边生成边返回增量文本，方便上层做“按句合成 TTS”。
这是同步阻塞的生成器，调用方应放到线程里跑（避免阻塞事件循环）。
"""
import logging
from typing import Iterator, List, Dict

from dashscope import Generation
from http import HTTPStatus

logger = logging.getLogger("llm")


def stream_chat(messages: List[Dict[str, str]], model: str) -> Iterator[str]:
    """流式对话。逐段 yield 增量文本（incremental）。"""
    logger.info("[pipeline] LLM 请求开始 model=%s messages=%d", model, len(messages))
    try:
        responses = Generation.call(
            model=model,
            messages=messages,
            result_format="message",
            stream=True,
            incremental_output=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("[pipeline] LLM 调用失败: %s", e)
        yield "抱歉，我现在有点问题，待会再聊吧。"
        return

    total_chars = 0
    for response in responses:
        if response.status_code != HTTPStatus.OK:
            logger.error(
                "[pipeline] LLM 返回错误 request_id=%s code=%s msg=%s",
                getattr(response, "request_id", "?"),
                getattr(response, "code", "?"),
                getattr(response, "message", "?"),
            )
            continue
        try:
            delta = response.output.choices[0].message.content
        except Exception:  # noqa: BLE001
            continue
        if delta:
            total_chars += len(delta)
            yield delta

    logger.info("[pipeline] LLM 流式输出结束，共 %d 字", total_chars)
