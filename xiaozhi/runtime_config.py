"""运行时配置：支持后台热更新，持久化到 data/admin_config.json。"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import config

logger = logging.getLogger("runtime_config")

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_PERSIST_PATH = _DATA_DIR / "admin_config.json"
_lock = threading.RLock()
_overrides: Dict[str, Any] = {}


# 可在后台修改并立即生效的配置项（下一 listen / 下一 turn 起生效）
SCHEMA: List[Dict[str, Any]] = [
    {
        "key": "english_service_mode",
        "type": "enum",
        "label": "English 服务模式",
        "description": (
            "纯 Omni：聆听时直连多模态语音模型（旧行为，适合纠音/口语）。"
            "智能路由：先 ASR 再按规则分流 cheap TEXT 与 OMNI，省钱。"
        ),
        "group": "english",
        "options": [
            {"value": "router", "label": "智能路由（TEXT + OMNI）"},
            {"value": "omni_only", "label": "纯 Omni（始终多模态）"},
        ],
        "hot": True,
        "maps": {"ENGLISH_ROUTE_ENABLED": True},
        "maps_alt": {"ENGLISH_ROUTE_ENABLED": False},
        "map_key": "english_service_mode",
    },
    {
        "key": "ENGLISH_OMNI_MODEL",
        "type": "enum",
        "label": "Omni 模型",
        "description": "下次建立 Omni 连接时生效。",
        "group": "english",
        "options": [
            {
                "value": "qwen3.5-omni-flash-realtime",
                "label": "Flash（快、省）",
            },
            {
                "value": "qwen3.5-omni-plus-realtime",
                "label": "Plus（更强）",
            },
        ],
        "hot": True,
        "when": {"english_service_mode": "omni_only"},
        "nested_under": "english_service_mode",
    },
    {
        "key": "ENGLISH_DEFAULT_ROUTE",
        "type": "enum",
        "label": "路由默认路径",
        "description": "智能路由模式下，规则均未命中时的默认模型路径。",
        "group": "english",
        "options": [
            {"value": "text", "label": "TEXT（LLM + TTS，便宜）"},
            {"value": "omni", "label": "OMNI（多模态语音）"},
        ],
        "hot": True,
        "when": {"english_service_mode": "router"},
    },
    {
        "key": "ENGLISH_ROUTER_LLM",
        "type": "bool",
        "label": "LLM 辅助路由",
        "description": "规则未命中时，用 LLM 判断走 TEXT 还是 OMNI（略增延迟与费用）。",
        "group": "english",
        "hot": True,
        "when": {"english_service_mode": "router"},
    },
    {
        "key": "ENGLISH_OMNI_STICKY_SEC",
        "type": "float",
        "label": "Omni 粘性（秒）",
        "description": "上一轮走 OMNI 后，短句跟读仍走 Omni 的秒数。",
        "group": "english",
        "min": 0,
        "max": 300,
        "hot": True,
        "when": {"english_service_mode": "router"},
    },
    {
        "key": "ENGLISH_ASR_UTTERANCE_GAP_SEC",
        "type": "float",
        "label": "ASR 句末防抖（秒）",
        "description": "路由聆听模式下，ASR 句末后等待多久无新句再提交 LLM。",
        "group": "english",
        "min": 0.2,
        "max": 5,
        "hot": True,
        "when": {"english_service_mode": "router"},
    },
    {
        "key": "ENGLISH_OMNI_VOICE",
        "type": "str",
        "label": "Omni 音色",
        "description": "下次建立 Omni 连接时生效。",
        "group": "english",
        "hot": True,
    },
    {
        "key": "ENGLISH_TEXT_LLM_MODEL",
        "type": "str",
        "label": "TEXT LLM 模型",
        "description": "TEXT 路由使用的文本大模型。",
        "group": "english",
        "hot": True,
        "when": {"english_service_mode": "router"},
    },
    {
        "key": "ENGLISH_TEXT_TTS_VOICE",
        "type": "str",
        "label": "TEXT TTS 音色",
        "description": "TEXT 路由使用的 CosyVoice 音色。",
        "group": "english",
        "hot": True,
        "when": {"english_service_mode": "router"},
    },
    {
        "key": "ENGLISH_TEXT_ENABLE_SEARCH",
        "type": "bool",
        "label": "TEXT 联网搜索",
        "description": "命中「新闻/天气/日期」等路由时，TEXT LLM 开启 enable_search。",
        "group": "english",
        "hot": True,
    },
    {
        "key": "ENGLISH_TEXT_SEARCH_STRATEGY",
        "type": "enum",
        "label": "TEXT 搜索策略",
        "description": "百炼 search_strategy：turbo 日常、max 高精度。",
        "group": "english",
        "options": [
            {"value": "turbo", "label": "turbo（快）"},
            {"value": "max", "label": "max（准）"},
        ],
        "hot": True,
        "when": {"english_service_mode": "router"},
    },
    {
        "key": "ENGLISH_OMNI_ENABLE_SEARCH",
        "type": "bool",
        "label": "Omni 联网搜索",
        "description": "纯 Omni / OMNI 回放时在 session.update 开启 enable_search。",
        "group": "english",
        "hot": True,
    },
    {
        "key": "ENGLISH_OMNI_SEARCH_STRATEGY",
        "type": "enum",
        "label": "Omni 搜索策略",
        "description": "Omni Realtime 需使用 agent 策略（百炼要求）。",
        "group": "english",
        "options": [
            {"value": "agent", "label": "agent（Omni 推荐）"},
        ],
        "hot": True,
    },
    {
        "key": "LOG_LEVEL",
        "type": "enum",
        "label": "日志级别",
        "description": "修改后立即调整服务端日志 verbosity。",
        "group": "system",
        "options": [
            {"value": "DEBUG", "label": "DEBUG"},
            {"value": "INFO", "label": "INFO"},
            {"value": "WARNING", "label": "WARNING"},
            {"value": "ERROR", "label": "ERROR"},
        ],
        "hot": True,
    },
]

_SCHEMA_BY_KEY = {item["key"]: item for item in SCHEMA}


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _coerce_float(value: Any) -> float:
    return float(value)


def _coerce_str(value: Any) -> str:
    return str(value).strip()


def _coerce_value(key: str, value: Any) -> Any:
    spec = _SCHEMA_BY_KEY.get(key)
    if spec is None:
        return value
    t = spec.get("type")
    if t == "bool":
        return _coerce_bool(value)
    if t == "float":
        return _coerce_float(value)
    if t == "enum":
        allowed = {opt["value"] for opt in spec.get("options", [])}
        v = _coerce_str(value).lower() if key == "ENGLISH_DEFAULT_ROUTE" else _coerce_str(value)
        if key == "ENGLISH_DEFAULT_ROUTE":
            v = v.lower()
        if v not in allowed:
            raise ValueError(f"{key} 无效值: {value}")
        return v
    return _coerce_str(value)


def english_service_mode() -> str:
    """router | omni_only"""
    if get_bool("ENGLISH_ROUTE_ENABLED"):
        return "router"
    return "omni_only"


def _apply_english_service_mode(mode: str) -> None:
    mode = (mode or "").strip().lower()
    if mode not in ("router", "omni_only"):
        raise ValueError("english_service_mode 必须是 router 或 omni_only")
    _overrides["english_service_mode"] = mode
    _overrides["ENGLISH_ROUTE_ENABLED"] = mode == "router"


def _bootstrap_overrides() -> None:
    global _overrides
    _overrides = {}
    if _PERSIST_PATH.is_file():
        try:
            raw = json.loads(_PERSIST_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k == "english_service_mode":
                        _apply_english_service_mode(str(v))
                    elif k in _SCHEMA_BY_KEY:
                        _overrides[k] = _coerce_value(k, v)
        except Exception as e:  # noqa: BLE001
            logger.warning("读取 admin_config.json 失败: %s", e)
    if "english_service_mode" not in _overrides:
        _apply_english_service_mode(
            "router" if config.ENGLISH_ROUTE_ENABLED else "omni_only"
        )


def _persist() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        payload = dict(_overrides)
    _PERSIST_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _baseline(key: str) -> Any:
    if key == "english_service_mode":
        return "router" if config.ENGLISH_ROUTE_ENABLED else "omni_only"
    if not hasattr(config, key):
        return None
    return getattr(config, key)


def get_raw(key: str) -> Any:
    with _lock:
        if key == "english_service_mode":
            return english_service_mode()
        if key in _overrides:
            return _overrides[key]
    return _baseline(key)


def get_bool(key: str) -> bool:
    return _coerce_bool(get_raw(key))


def get_str(key: str) -> str:
    return _coerce_str(get_raw(key))


def get_float(key: str) -> float:
    return _coerce_float(get_raw(key))


def apply_log_level(level: str) -> None:
    import logging as _logging

    lvl = getattr(_logging, str(level).upper(), _logging.INFO)
    _logging.getLogger().setLevel(lvl)
    for name in ("english", "english.router", "english.text", "english.session", "ota", "auth.api"):
        _logging.getLogger(name).setLevel(lvl)


def update_values(patch: Dict[str, Any]) -> Dict[str, Any]:
    """批量更新，返回当前有效值。"""
    changed: Dict[str, Any] = {}
    with _lock:
        if "english_service_mode" in patch:
            _apply_english_service_mode(str(patch.pop("english_service_mode")))
            changed["english_service_mode"] = english_service_mode()
            changed["ENGLISH_ROUTE_ENABLED"] = get_bool("ENGLISH_ROUTE_ENABLED")

        for key, value in patch.items():
            if key not in _SCHEMA_BY_KEY:
                raise ValueError(f"未知配置项: {key}")
            coerced = _coerce_value(key, value)
            spec = _SCHEMA_BY_KEY[key]
            if spec.get("type") == "float":
                mn, mx = spec.get("min"), spec.get("max")
                if mn is not None and coerced < mn:
                    raise ValueError(f"{key} 不能小于 {mn}")
                if mx is not None and coerced > mx:
                    raise ValueError(f"{key} 不能大于 {mx}")
            _overrides[key] = coerced
            changed[key] = coerced

        if "LOG_LEVEL" in changed:
            apply_log_level(changed["LOG_LEVEL"])

        _persist()

    logger.info("[admin] 运行时配置已更新: %s", changed)
    return get_public_snapshot()


def get_public_snapshot() -> Dict[str, Any]:
    """供 API / 页面使用的当前值 + 元数据。"""
    values: Dict[str, Any] = {}
    for item in SCHEMA:
        key = item["key"]
        raw = get_raw(key)
        if item["type"] == "bool":
            values[key] = get_bool(key)
        elif item["type"] == "float":
            values[key] = get_float(key)
        else:
            values[key] = raw if raw is not None else ""
    values["ENGLISH_ROUTE_ENABLED"] = get_bool("ENGLISH_ROUTE_ENABLED")
    return values


def get_schema() -> List[Dict[str, Any]]:
    return SCHEMA


_bootstrap_overrides()
