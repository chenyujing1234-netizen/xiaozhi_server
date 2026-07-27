"""SpeakPal 登录 API：微信 openid / 手机号验证码（对齐 huanDa）。"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from aiohttp import web
import jwt

from config import config
from xiaozhi import auth_user, sms

logger = logging.getLogger("auth.api")

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

# phone -> {code, expire_at, last_sent}
_codes: dict[str, dict] = {}
_codes_lock = asyncio.Lock()

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}


def _json(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers=CORS_HEADERS)


def _client_ip(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote or ""


def create_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(days=config.JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> Optional[int]:
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
        return int(payload["sub"])
    except Exception:  # noqa: BLE001
        return None


def user_from_request(request: web.Request) -> Optional[dict]:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    uid = decode_token(auth[7:].strip())
    if uid is None:
        return None
    return auth_user.get_user(uid)



def _login_error(request: web.Request, message: str, status: int = 400) -> web.Response:
    if request.rel_url.query.get("go") == "1":
        import html as _html
        safe = _html.escape(message)
        page = (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>登录失败</title></head><body>"
            f"<p style=\"font-family:sans-serif;padding:24px;color:#b91c1c\">{safe}</p>"
            "<p style=\"font-family:sans-serif;padding:0 24px\">"
            "<a href=\"/english/\">返回重新登录</a></p>"
            "</body></html>"
        )
        return web.Response(text=page, status=status, content_type="text/html", charset="utf-8")
    return _json({"ok": False, "error": message}, status)

async def options_handler(_: web.Request) -> web.Response:
    return web.Response(status=204, headers=CORS_HEADERS)


async def _do_phone_login(request: web.Request, phone: str, code: str) -> web.Response:
    if not PHONE_RE.match(phone) or not code:
        return _login_error(request, "参数不完整", 400)

    async with _codes_lock:
        entry = _codes.get(phone)
        if not entry or time.time() > entry["expire_at"]:
            return _login_error(request, "验证码已过期，请重新获取", 400)
        if entry["code"] != code:
            return _login_error(request, "验证码错误", 400)
        _codes.pop(phone, None)

    try:
        user_id = await asyncio.to_thread(auth_user.upsert_user_by_phone, phone)
        await asyncio.to_thread(
            auth_user.record_login,
            user_id,
            _client_ip(request),
            request.headers.get("User-Agent", ""),
            login_type="phone",
            phone=phone,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[auth] 手机登录写库失败: %s", e)
        return _login_error(request, "登录失败，请稍后重试", 500)


    token = create_token(user_id)
    logger.info("[auth] 手机登录成功 user_id=%s phone=%s", user_id, phone)
    user_obj = {
        "id": user_id,
        "phone": phone,
        "device_id": auth_user.device_id_for_user(user_id),
    }
    if request.rel_url.query.get("go") == "1":
        import json as _jsonlib
        html = (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>登录中</title></head><body>"
            "<p style=\"font-family:sans-serif;padding:24px\">登录成功，正在返回 SpeakPal…</p>"
            "<script>(function(){try{"
            f"localStorage.setItem('speakpal_token',{_jsonlib.dumps(token)});"
            f"localStorage.setItem('speakpal_user',{_jsonlib.dumps(user_obj, ensure_ascii=False)});"
            f"localStorage.setItem('speakpal_device_id',{_jsonlib.dumps(user_obj['device_id'])});"
            "}catch(e){}location.replace('/english/');})();</script>"
            "</body></html>"
        )
        return web.Response(text=html, content_type="text/html", charset="utf-8")
    return _json({"ok": True, "token": token, "user": user_obj})


async def send_code(request: web.Request) -> web.Response:
    """发短信；带 code/sms 则校验登录。支持 POST JSON / form 与 GET。"""
    phone = ""
    code = ""
    if request.method == "GET":
        q = request.rel_url.query
        phone = str(q.get("phone", "")).strip()
        code = str(q.get("code") or q.get("sms") or "").strip()
    else:
        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            try:
                post = await request.post()
                body = dict(post)
            except Exception:
                return _json({"ok": False, "error": "无效请求"}, 400)
        phone = str(body.get("phone", "")).strip()
        code = str(body.get("code") or body.get("sms") or "").strip()

    if code:
        return await _do_phone_login(request, phone, code)

    if not PHONE_RE.match(phone):
        return _json({"ok": False, "error": "手机号格式不正确"}, 400)

    now = time.time()
    async with _codes_lock:
        prev = _codes.get(phone)
        if prev and now - prev.get("last_sent", 0) < 60:
            wait = int(60 - (now - prev["last_sent"]))
            return _json({"ok": False, "error": f"请 {wait} 秒后再试"}, 429)
        new_code = sms.generate_code()
        _codes[phone] = {"code": new_code, "expire_at": now + 300, "last_sent": now}

    ok = await sms.send_sms_code(phone, new_code)
    if not ok:
        if config.AUTH_SMS_DEBUG:
            logger.warning(
                "[auth] SMS 失败但 AUTH_SMS_DEBUG=1，验证码=%s phone=%s", new_code, phone
            )
        else:
            async with _codes_lock:
                _codes.pop(phone, None)
            return _json({"ok": False, "error": "验证码发送失败，请稍后重试"}, 500)

    logger.info("[auth] 验证码已发送 phone=%s", phone)
    return _json({"ok": True, "message": "验证码已发送"})


async def verify_code(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json({"ok": False, "error": "无效请求"}, 400)
    phone = str(body.get("phone", "")).strip()
    code = str(body.get("code", "")).strip()
    return await _do_phone_login(request, phone, code)


async def wx_login(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json({"ok": False, "error": "无效请求"}, 400)
    # 兼容 huanDa 前端字段名 wx_code
    code = str(body.get("code") or body.get("wx_code") or "").strip()
    if not code:
        return _json({"ok": False, "error": "缺少微信登录 code"}, 400)
    if not config.WX_APPID or not config.WX_SECRET:
        return _json({"ok": False, "error": "微信登录未配置"}, 500)

    url = (
        "https://api.weixin.qq.com/sns/jscode2session"
        f"?appid={config.WX_APPID}&secret={config.WX_SECRET}"
        f"&js_code={code}&grant_type=authorization_code"
    )
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                data = await resp.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        logger.warning("[auth] jscode2session 失败: %s", e)
        return _json({"ok": False, "error": "微信登录服务异常"}, 500)

    openid = data.get("openid")
    if not openid:
        logger.warning("[auth] 微信登录失败: %s", data)
        return _json(
            {
                "ok": False,
                "error": data.get("errmsg") or "微信登录失败",
                "errcode": data.get("errcode"),
            },
            400,
        )

    try:
        user_id = await asyncio.to_thread(auth_user.upsert_user_by_openid, openid)
        await asyncio.to_thread(
            auth_user.record_login,
            user_id,
            _client_ip(request),
            request.headers.get("User-Agent", ""),
            login_type="wechat",
            openid=openid,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[auth] 微信登录写库失败: %s", e)
        return _json({"ok": False, "error": "登录失败，请稍后重试"}, 500)

    token = create_token(user_id)
    user = await asyncio.to_thread(auth_user.get_user, user_id)
    logger.info("[auth] 微信登录成功 user_id=%s", user_id)
    return _json(
        {
            "ok": True,
            "token": token,
            "user": {
                "id": user_id,
                "phone": (user or {}).get("phone"),
                "openid": openid,
                "device_id": auth_user.device_id_for_user(user_id),
            },
        }
    )


async def me(request: web.Request) -> web.Response:
    user = user_from_request(request)
    if not user:
        return _json({"ok": False, "error": "未登录"}, 401)
    return _json(
        {
            "ok": True,
            "user": {
                "id": user["id"],
                "phone": user.get("phone"),
                "openid": user.get("openid"),
                "device_id": auth_user.device_id_for_user(int(user["id"])),
            },
        }
    )


def setup_auth_routes(app: web.Application) -> None:
    try:
        auth_user.init_auth_tables()
    except Exception as e:  # noqa: BLE001
        logger.warning("[auth] 初始化用户表失败（稍后重试）: %s", e)

    # phone-login 为主路径（避免部分浏览器/广告拦截规则匹配 verify）
    # verify-code 保留兼容
    app.router.add_route("OPTIONS", "/api/auth/send-code", options_handler)
    app.router.add_route("OPTIONS", "/api/auth/phone-login", options_handler)
    app.router.add_route("OPTIONS", "/api/auth/verify-code", options_handler)
    app.router.add_route("OPTIONS", "/api/auth/wx-login", options_handler)
    app.router.add_route("OPTIONS", "/api/auth/me", options_handler)
    app.router.add_post("/api/auth/send-code", send_code)
    app.router.add_get("/api/auth/send-code", send_code)
    app.router.add_post("/api/auth/phone-login", verify_code)
    app.router.add_post("/api/auth/verify-code", verify_code)
    app.router.add_post("/api/auth/wx-login", wx_login)
    app.router.add_get("/api/auth/me", me)
    logger.info("[auth] 路由已注册 /api/auth/*")
