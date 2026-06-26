"""OTA 配置下发接口（HTTP）。

设备开机会向 OTA URL 发请求（携带系统信息 JSON），服务端返回一段 JSON 配置。
我们在这里：
  - 按 config.TRANSPORT 下发 mqtt（默认，设备出厂方式）或 websocket 配置
  - 回一个不高于当前固件的版本号，避免触发误升级
  - 顺便对齐一下设备时钟

要让设备连到本服务端，需要把设备的 OTA URL 指向这里（见 README）。
"""
import json
import logging
import time

from aiohttp import web

from config import config

logger = logging.getLogger("ota")


def _build_response(body_text: str, device_id: str) -> dict:
    # 尝试从设备上报的系统信息里取当前固件版本，回相同版本以避免升级
    current_version = "0.0.0"
    try:
        body = json.loads(body_text) if body_text else {}
        app = body.get("application") or {}
        if isinstance(app, dict) and app.get("version"):
            current_version = str(app["version"])
    except Exception:  # noqa: BLE001
        pass

    now_ms = int(time.time() * 1000)
    resp = {
        "server_time": {
            "timestamp": now_ms,
            "timezone_offset": 480,  # UTC+8，单位：分钟
        },
        # 与当前版本一致 + 空 url => 设备判断为“已是最新”，不会升级
        "firmware": {
            "version": current_version,
            "url": "",
        },
    }

    if config.TRANSPORT == "websocket":
        # 只给 websocket，不给 mqtt => 设备走 WebSocket
        resp["websocket"] = {
            "url": config.ws_url_for_device,
            "version": 1,             # 二进制协议版本1：裸 Opus 帧
            "token": "xiaozhi-test",
        }
    else:
        # 默认：给 mqtt（设备出厂默认走 MQTT+UDP）。endpoint 用 :1883 => 明文 TCP（非 TLS）
        client_id = _make_client_id(device_id)
        resp["mqtt"] = {
            "endpoint": f"{config.PUBLIC_HOST}:{config.MQTT_PORT}",
            "client_id": client_id,
            "username": "xiaozhi",
            "password": "xiaozhi",
            "publish_topic": config.MQTT_PUBLISH_TOPIC,
            "keepalive": 240,
        }
    return resp


def _make_client_id(device_id: str) -> str:
    safe = "".join(c for c in (device_id or "") if c.isalnum())
    if not safe:
        safe = "device"
    return f"xz-{safe}"


async def _handle_ota(request: web.Request) -> web.Response:
    body_text = await request.text()
    device_id = request.headers.get("Device-Id", "unknown")
    client_id = request.headers.get("Client-Id", "unknown")
    logger.info("OTA 请求 device_id=%s client_id=%s body=%s", device_id, client_id, body_text[:300])

    resp = _build_response(body_text, device_id)
    if "websocket" in resp:
        logger.info("OTA 下发 websocket.url=%s", resp["websocket"]["url"])
    else:
        logger.info("OTA 下发 mqtt.endpoint=%s publish_topic=%s",
                    resp["mqtt"]["endpoint"], resp["mqtt"]["publish_topic"])
    return web.json_response(resp)


async def _handle_activate(request: web.Request) -> web.Response:
    # 设备已激活的情况下一般不会走到这里；直接返回成功
    logger.info("收到 activate 请求，直接返回 200")
    return web.json_response({"status": "ok"})


def build_app() -> web.Application:
    app = web.Application()
    # 设备的 OTA URL 形如 http://host:port/xiaozhi/ota/
    app.router.add_route("*", "/xiaozhi/ota/", _handle_ota)
    app.router.add_route("*", "/xiaozhi/ota", _handle_ota)
    app.router.add_route("*", "/xiaozhi/ota/activate", _handle_activate)
    # 兜底：根路径也返回配置，方便不同 OTA URL 写法
    app.router.add_route("*", "/", _handle_ota)
    return app


async def start_ota_server():
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.OTA_HOST, config.OTA_PORT)
    await site.start()
    logger.info("OTA 服务监听 http://%s:%d/xiaozhi/ota/", config.OTA_HOST, config.OTA_PORT)
    logger.info("请把设备 OTA URL 设置为: http://%s:%d/xiaozhi/ota/", config.PUBLIC_HOST, config.OTA_PORT)
    return runner
