"""英语口语轮后轻量诊断（P1 预埋）。

在 Omni 回复结束后，用文本 LLM 提炼本轮纠错点，供页面展示与后续错题账本。
失败时静默忽略，不影响主对话。
"""
from __future__ import annotations

import json
import logging
import re
from http import HTTPStatus
from typing import Any, Optional

from dashscope import Generation

from config import config

logger = logging.getLogger("english.diagnosis")


def _parse_json(raw: str) -> Optional[dict]:
    text = (raw or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def diagnose_turn(user_text: str, tutor_text: str) -> Optional[dict[str, Any]]:
    """返回结构化纠错摘要；无明显错误时返回 None。"""
    user_text = (user_text or "").strip()
    tutor_text = (tutor_text or "").strip()
    if not user_text:
        return None

    system = (
        "你是英语口语诊断助手。根据学生原话和导师回复，判断本轮是否存在值得记录的纠错。"
        "只关注：发音提示、用词、时态、句子结构。"
        "若导师已经纠正，请提炼要点；若学生几乎没错，返回 has_correction=false。"
        "只返回 JSON："
        '{"has_correction": bool, "error_type": "pronunciation|vocab|tense|syntax|mixed|none", '
        '"zh_explain": "一句中文说明", "correct_en": "正确英文", "severity": "low|medium|high"}'
    )
    user = f"学生说：{user_text}\n导师说：{tutor_text or '(无)'}\n"
    try:
        resp = Generation.call(
            model=config.ENGLISH_PROFILE_LLM_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            result_format="message",
            temperature=0.1,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("[english.diagnosis] LLM 失败: %s", e)
        return None

    if resp.status_code != HTTPStatus.OK:
        return None
    try:
        content = resp.output.choices[0].message.content
    except Exception:  # noqa: BLE001
        return None
    data = _parse_json(content)
    if not data or not data.get("has_correction"):
        return None
    zh = (data.get("zh_explain") or "").strip()
    en = (data.get("correct_en") or "").strip()
    if not zh and not en:
        return None
    return {
        "error_type": data.get("error_type") or "mixed",
        "zh_explain": zh[:200],
        "correct_en": en[:200],
        "severity": data.get("severity") or "medium",
    }
