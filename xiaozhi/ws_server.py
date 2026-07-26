"""WebSocket 语音通道服务端。

设备（ESP32）连接后：
  1. 发 hello -> 我们回 hello（带 session_id 和下行音频参数）
  2. 发 listen start + 二进制 Opus 上行音频
  3. 我们识别(ASR) -> 大模型(LLM) -> 合成(TTS) -> 下行 Opus 音频 + tts/stt 控制消息
"""
import asyncio
import logging
from urllib.parse import parse_qs, urlparse

import websockets

from config import config
from .session import Session
from .english_session import EnglishSession
from .transports import WebSocketTransport

logger = logging.getLogger("ws")


def _get_header(headers, name: str, default: str = "") -> str:
    try:
        return headers.get(name, default) or default
    except Exception:  # noqa: BLE001
        return default


async def _handler(websocket):
    # 兼容不同 websockets 版本获取请求头/路径
    try:
        request = websocket.request
        path = request.path
        headers = request.headers
    except AttributeError:
        path = getattr(websocket, "path", "")
        headers = getattr(websocket, "request_headers", {})

    parsed = urlparse(path)
    path_only = parsed.path
    query = parse_qs(parsed.query)
    # 浏览器 WebSocket 无法自定义 Header，允许 query 或默认 web 客户端 ID
    device_id = _get_header(headers, "Device-Id", "") or query.get("device_id", ["web-browser"])[0]
    client_id = _get_header(headers, "Client-Id", "") or query.get("client_id", ["web-test"])[0]
    proto_ver = _get_header(headers, "Protocol-Version", "?")
    peer = getattr(websocket, "remote_address", ("?", 0))

    english = path_only.rstrip("/") == config.ENGLISH_WS_PATH.rstrip("/")
    session_cls = EnglishSession if english else Session
    tag = "english" if english else "default"

    logger.info(
        "设备已连接[%s] path=%s device_id=%s client_id=%s proto=%s from=%s",
        tag, path_only, device_id, client_id, proto_ver, peer,
    )

    loop = asyncio.get_running_loop()
    transport = WebSocketTransport(websocket)
    session = session_cls(transport, loop, device_id=device_id, client_id=client_id)

    try:
        async for message in websocket:
            if isinstance(message, (bytes, bytearray)):
                session.handle_binary(bytes(message))
            else:
                await session.handle_text(message)
    except websockets.ConnectionClosed:
        logger.info("设备断开连接 device_id=%s", device_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("连接处理异常: %s", e)
    finally:
        await session.cleanup()
        logger.info("会话已清理 session_id=%s", session.session_id)


async def start_ws_server():
    logger.info("WebSocket 服务监听 ws://%s:%d%s", config.WS_HOST, config.WS_PORT, config.WS_PATH)
    logger.info("设备应连接（默认）: %s", config.ws_url_for_device)
    if config.ENGLISH_ENABLED:
        logger.info("设备应连接（英语）: %s", config.english_ws_url_for_device)
    server = await websockets.serve(
        _handler,
        config.WS_HOST,
        config.WS_PORT,
        max_size=None,        # 音频帧不限制大小
        ping_interval=30,
        ping_timeout=60,
    )
    return server
