"""英语轮次路由：cheap TEXT（ASR+LLM+TTS） vs OMNI（听发音纠音）。"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from config import config
from xiaozhi import runtime_config as rc

logger = logging.getLogger("english.router")


class TurnRoute(str, Enum):
    TEXT = "text"
    OMNI = "omni"


@dataclass
class RouteDecision:
    route: TurnRoute
    reason: str
    source: str  # rule | sticky | llm | default | force | search
    web_search: bool = False


# --- 规则：强制 OMNI（需听原声：发音 / 拼写 / 跟读 / 纠音 / 口语练）---
_OMNI_PATTERNS = [
    # 发音 · 读音 · 跟读 · 朗读
    r"发音|读音|怎么读|读一遍|读给我|念一遍|朗读|跟读|领读|复读|"
    r"我读对|我读得|说得对|说得对吗|说对了吗|对不对|对吗|"
    r"纠正我|帮我纠正|纠音|纠错|改发音|发音纠正|哪里读错|读错|说错|不准|"
    r"重读|语调|语气|连读|弱读|浊化|略读|重音|"
    r"音标|国际音标|标准发音|美式发音|英式发音|"
    r"练发音|发音练习|跟读练习|朗读练习|"
    r"repeat\s+after|pronunciation|read\s+(this|it|aloud|out)|"
    r"how\s+do\s+i\s+say|say\s+it\s+(again|slowly)|"
    r"how\s+to\s+pronounce|pronounce\s+(this|it|the|that)?|"
    r"phonetic|intonation|accent|stress|IPA",
    # 拼写 · 字母
    r"拼写|拼读|拼出来|拼一下|怎么拼|怎么写|单词怎么写|单词怎么拼|"
    r"字母怎么读|逐个读|一个字母|拼给我|"
    r"spell(ing|s|ed)?|spell\s+out|how\s+(?:do\s+you|to)\s+spell|"
    r"letter\s+by\s+letter|spell\s+this",
    # 用户要开口 · 需听原声
    r"我读|我说|让我读|让我说|我来读|我来说|听我说|听听我|帮我听|"
    r"你听听|听我读|听我读|我说你听|"
    r"再来一遍|再说一遍|再读一遍|再念一遍|重复一遍|慢点读|慢一点|慢速读|说慢点|"
    r"模仿|复述|复诵|"
    r"口语练习|练口语|开口说|说英语|英语对话|对话练习|练对话|练英语说|"
    r"listen\s+to\s+me|check\s+my|correct\s+my|fix\s+my|"
    r"am\s+i\s+(?:saying|pronouncing)|did\s+i\s+(?:say|pronounce)|"
    r"repeat|say\s+again|one\s+more\s+time|slow(?:ly|er)",
]

# --- 规则：倾向 TEXT（讲解 / 翻译；拼写相关走 OMNI）---
_TEXT_PATTERNS = [
    r"翻译|什么意思|啥意思|是什么意思|用法|语法|造句|例句|"
    r"story|笑话|joke|聊天|闲聊|"
    r"explain|translate|what\s+does\s+.+\s+mean|how\s+to\s+use",
]

# --- 需联网实时信息：强制 TEXT + enable_search（新闻 / 天气 / 日期等）---
_SEARCH_PATTERNS = [
    r"今天|今日|现在|当前|最新|实时|新闻|头条|热搜|天气|气温|温度|"
    r"星期几|周几|几号|哪天|什么日子|日期|"
    r"what\s+day|what(?:'s|\s+is)\s+the\s+date|what\s+date|"
    r"today(?:'s|\s+is)?|today'?s?\s+news|"
    r"latest\s+news|current\s+news|breaking\s+news|headline|"
    r"weather|forecast|stock\s+market|exchange\s+rate",
]

_OMNI_RE = [re.compile(p, re.IGNORECASE) for p in _OMNI_PATTERNS]
_TEXT_RE = [re.compile(p, re.IGNORECASE) for p in _TEXT_PATTERNS]
_SEARCH_RE = [re.compile(p, re.IGNORECASE) for p in _SEARCH_PATTERNS]


def needs_web_search(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    return any(rx.search(text) for rx in _SEARCH_RE)


def route_turn(
    user_text: str,
    *,
    has_image: bool = False,
    sticky_omni: bool = False,
    profile_snippet: str = "",
) -> RouteDecision:
    text = (user_text or "").strip()
    if not text and not has_image:
        return RouteDecision(TurnRoute.TEXT, "empty", "default")

    if has_image:
        return RouteDecision(TurnRoute.OMNI, "has_image", "force")

    if needs_web_search(text):
        return RouteDecision(
            TurnRoute.TEXT,
            "realtime_info",
            "search",
            web_search=True,
        )

    if sticky_omni:
        return RouteDecision(TurnRoute.OMNI, "sticky_followup", "sticky")

    for rx in _OMNI_RE:
        if rx.search(text):
            return RouteDecision(TurnRoute.OMNI, f"match:{rx.pattern[:40]}", "rule")

    omni_hits = sum(1 for rx in _OMNI_RE if rx.search(text))
    text_hits = sum(1 for rx in _TEXT_RE if rx.search(text))
    if text_hits > 0 and omni_hits == 0:
        return RouteDecision(TurnRoute.TEXT, f"text_rules:{text_hits}", "rule")

    if rc.get_bool("ENGLISH_ROUTER_LLM") and text_hits == 0 and omni_hits == 0:
        llm_dec = _llm_classify(text, profile_snippet)
        if llm_dec is not None:
            return llm_dec

    default = (rc.get_str("ENGLISH_DEFAULT_ROUTE") or "text").lower()
    if default == "omni":
        return RouteDecision(TurnRoute.OMNI, "config_default_omni", "default")
    return RouteDecision(TurnRoute.TEXT, "config_default_text", "default")


def _llm_classify(text: str, profile_snippet: str) -> Optional[RouteDecision]:
    try:
        from dashscope import Generation
    except ImportError:
        return None

    system = (
        "你是路由分类器。判断学生这句话是否需要「听他的英语发音」才能回答。"
        "需要听原声/纠音/跟读/拼写带读/练口语 → omni；"
        "只需文字讲解/翻译/语法/聊天/故事 → text。"
        '只返回 JSON：{"route":"text"|"omni","reason":"简短中文"}'
    )
    user = f"画像摘要: {(profile_snippet or '')[:200]}\n学生说: {text[:500]}"
    try:
        resp = Generation.call(
            model=rc.get_str("ENGLISH_ROUTER_LLM_MODEL") or config.ENGLISH_ROUTER_LLM_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            result_format="message",
            temperature=0.0,
            max_tokens=80,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("[english.router] LLM 分类失败: %s", e)
        return None

    raw = ""
    try:
        raw = resp.output.choices[0].message.content  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None

    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    route_s = (data.get("route") or "").lower()
    reason = str(data.get("reason") or "llm")
    if route_s == "omni":
        return RouteDecision(TurnRoute.OMNI, reason, "llm")
    if route_s == "text":
        return RouteDecision(TurnRoute.TEXT, reason, "llm")
    return None
