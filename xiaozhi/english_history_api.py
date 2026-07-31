"""SpeakPal 对话历史 API（按登录用户 device_id 读取）。"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from config import config
from xiaozhi.auth_api import user_from_request
from xiaozhi import auth_user
from xiaozhi.english_history import ChatMessage, get_history_store

logger = logging.getLogger("english.history.api")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}


def _json(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers=CORS_HEADERS)


def _serialize_message(m: ChatMessage) -> dict:
    item = {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "session_id": m.session_id,
        "created_at": m.created_at,
    }
    if m.role == "image" and m.id:
        item["image_url"] = f"/api/english/history/image/{m.id}"
        item["content"] = ""
    return item


async def handle_get_history(request: web.Request) -> web.Response:
    user = user_from_request(request)
    if not user:
        return _json({"ok": False, "error": "未登录"}, 401)

    try:
        limit = int(request.rel_url.query.get("limit", "0"))
    except ValueError:
        limit = 0
    default_limit = max(
        int(config.ENGLISH_HISTORY_MAX_MESSAGES),
        int(config.ENGLISH_HISTORY_UI_MAX_MESSAGES),
    )
    limit = limit if limit > 0 else default_limit
    limit = max(1, min(limit, 100))

    device_id = auth_user.device_id_for_user(int(user["id"]))
    try:
        msgs = await asyncio.to_thread(
            get_history_store().get_recent,
            device_id,
            max_messages=limit,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[english.history] 读取失败 device_id=%s", device_id)
        return _json({"ok": False, "error": f"读取历史失败: {e}"}, 500)

    return _json(
        {
            "ok": True,
            "device_id": device_id,
            "messages": [_serialize_message(m) for m in msgs],
        }
    )


async def handle_get_history_image(request: web.Request) -> web.Response:
    user = user_from_request(request)
    if not user:
        return _json({"ok": False, "error": "未登录"}, 401)
    try:
        message_id = int(request.match_info["message_id"])
    except (KeyError, TypeError, ValueError):
        return _json({"ok": False, "error": "无效图片 ID"}, 400)

    device_id = auth_user.device_id_for_user(int(user["id"]))
    try:
        data = await asyncio.to_thread(
            get_history_store().get_image_bytes,
            device_id,
            message_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[english.history] 读取图片失败 id=%s", message_id)
        return _json({"ok": False, "error": str(e)}, 500)

    if not data:
        return _json({"ok": False, "error": "图片不存在"}, 404)

    return web.Response(
        body=data,
        content_type="image/jpeg",
        headers={
            **CORS_HEADERS,
            "Cache-Control": "private, max-age=86400",
        },
    )


async def _handle_options(_request: web.Request) -> web.Response:
    return web.Response(status=204, headers=CORS_HEADERS)


def setup_english_history_routes(app: web.Application) -> None:
    app.router.add_route("OPTIONS", "/api/english/history", _handle_options)
    app.router.add_route(
        "OPTIONS", "/api/english/history/image/{message_id}", _handle_options
    )
    app.router.add_get("/api/english/history", handle_get_history)
    app.router.add_get(
        "/api/english/history/image/{message_id}", handle_get_history_image
    )
    logger.info(
        "SpeakPal 历史 API: GET /api/english/history, "
        "GET /api/english/history/image/{id}"
    )
