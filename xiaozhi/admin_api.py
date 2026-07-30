"""小智后台：运行时配置 API。"""
from __future__ import annotations

import logging
import time
from typing import Optional

from aiohttp import web

from config import config
from xiaozhi import runtime_config as rc

logger = logging.getLogger("admin.api")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, PATCH, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Admin-Token",
}


def _json(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers=CORS_HEADERS)


def _auth_ok(request: web.Request) -> bool:
    token = (config.ADMIN_TOKEN or "").strip()
    if not token:
        return False
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[7:].strip() == token:
        return True
    hdr = request.headers.get("X-Admin-Token", "").strip()
    return hdr == token


def _require_auth(request: web.Request) -> Optional[web.Response]:
    if not (config.ADMIN_TOKEN or "").strip():
        return _json(
            {
                "ok": False,
                "error": "未设置 ADMIN_TOKEN，请在服务端 .env 中配置后重启",
            },
            status=503,
        )
    if not _auth_ok(request):
        return _json({"ok": False, "error": "未授权"}, status=401)
    return None


async def _handle_options(_request: web.Request) -> web.Response:
    return web.Response(status=204, headers=CORS_HEADERS)


async def handle_get_config(request: web.Request) -> web.Response:
    denied = _require_auth(request)
    if denied is not None:
        return denied
    return _json(
        {
            "ok": True,
            "values": rc.get_public_snapshot(),
            "schema": rc.get_schema(),
            "server_time": int(time.time()),
            "public_host": config.PUBLIC_HOST,
        }
    )


async def handle_patch_config(request: web.Request) -> web.Response:
    denied = _require_auth(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json({"ok": False, "error": "无效 JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"ok": False, "error": "请求体须为 JSON 对象"}, status=400)
    patch = body.get("values") if "values" in body else body
    if not isinstance(patch, dict) or not patch:
        return _json({"ok": False, "error": "缺少 values"}, status=400)
    try:
        values = rc.update_values(patch)
    except ValueError as e:
        return _json({"ok": False, "error": str(e)}, status=400)
    except Exception as e:  # noqa: BLE001
        logger.exception("[admin] 更新配置失败")
        return _json({"ok": False, "error": str(e)}, status=500)
    return _json(
        {
            "ok": True,
            "message": "配置已保存并立即生效（新 listen / 新轮次起）",
            "values": values,
        }
    )


def setup_admin_routes(app: web.Application) -> None:
    app.router.add_route("OPTIONS", "/api/admin/config", _handle_options)
    app.router.add_get("/api/admin/config", handle_get_config)
    app.router.add_patch("/api/admin/config", handle_patch_config)
    logger.info("小智后台 API: /api/admin/config")
