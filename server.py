"""小智 ESP32 服务端入口。

同时启动：
  - OTA HTTP 配置接口
  - 默认小智通道：MQTT 1883 + UDP 8001（ASR → LLM → TTS）
  - 英语口语通道：MQTT 1884 + UDP 8003（Qwen-Omni Realtime S2S）
  - WebSocket 语音通道（TRANSPORT=websocket 时使用）

运行：
  python server.py
"""
import asyncio
import logging

import dashscope

from config import config
from xiaozhi.ota_server import start_ota_server
from xiaozhi.ws_server import start_ws_server
from xiaozhi.ssl_util import load_server_ssl_context
from xiaozhi.udp_server import UdpServer
from xiaozhi.mqtt_broker import MqttBroker
from xiaozhi.english_session import EnglishSession


def setup_logging():
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def main():
    setup_logging()
    log = logging.getLogger("main")

    if not config.DASHSCOPE_API_KEY:
        log.error("未配置 DASHSCOPE_API_KEY，无法调用 ASR/LLM/TTS")
        return
    dashscope.api_key = config.DASHSCOPE_API_KEY

    log.info("=" * 60)
    log.info("小智服务端启动中……")
    log.info("【默认通道】LLM=%s ASR=%s TTS=%s", config.LLM_MODEL, config.ASR_MODEL, config.TTS_MODEL)
    log.info("【默认通道】MQTT %s:%d  UDP %s:%d", config.PUBLIC_HOST, config.MQTT_PORT, config.PUBLIC_HOST, config.UDP_PORT)
    if config.ENGLISH_ENABLED:
        log.info("【英语通道】Omni=%s voice=%s", config.ENGLISH_OMNI_MODEL, config.ENGLISH_OMNI_VOICE)
        log.info(
            "【英语通道】MQTT %s:%d  UDP %s:%d",
            config.PUBLIC_HOST, config.ENGLISH_MQTT_PORT,
            config.PUBLIC_HOST, config.ENGLISH_UDP_PORT,
        )
    log.info("设备传输方式(TRANSPORT): %s", config.TRANSPORT)
    log.info("对外地址(PUBLIC_HOST): %s", config.PUBLIC_HOST)
    log.info("=" * 60)

    loop = asyncio.get_running_loop()

    ssl_context = None
    if config.HTTPS_ENABLED:
        ssl_context = load_server_ssl_context(
            config.SSL_CERT_FILE, config.SSL_KEY_FILE, config.PUBLIC_HOST,
        )

    ota_runner = await start_ota_server(ssl_context=ssl_context)
    ws_server = await start_ws_server()
    wss_server = None
    if ssl_context is not None:
        wss_server = await start_ws_server(port=config.WSS_PORT, ssl_context=ssl_context)

    # 默认小智：MQTT + UDP
    udp_server = UdpServer(loop)
    await udp_server.start(config.UDP_HOST, config.UDP_PORT)
    mqtt_broker = MqttBroker(loop, udp_server, name="default")
    mqtt_tcp = await mqtt_broker.start(config.MQTT_HOST, config.MQTT_PORT)

    # 英语口语练习：独立 MQTT + UDP（互不影响）
    english_mqtt_tcp = None
    if config.ENGLISH_ENABLED:
        english_udp = UdpServer(loop)
        await english_udp.start(config.UDP_HOST, config.ENGLISH_UDP_PORT)
        english_broker = MqttBroker(
            loop, english_udp, session_class=EnglishSession, name="english"
        )
        english_mqtt_tcp = await english_broker.start(config.MQTT_HOST, config.ENGLISH_MQTT_PORT)

    log.info("服务已就绪。按 Ctrl+C 退出。")
    if config.ENGLISH_ENABLED:
        log.info(
            "英语练习 OTA: http://%s:%d/xiaozhi/ota/english/",
            config.PUBLIC_HOST, config.OTA_PORT,
        )
        if config.HTTPS_ENABLED:
            log.info("SpeakPal 用户入口: %s", config.english_web_url)
            log.info("SpeakPal WebSocket: %s", config.english_wss_url_for_web)
    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        pass
    finally:
        ws_server.close()
        await ws_server.wait_closed()
        if wss_server is not None:
            wss_server.close()
            await wss_server.wait_closed()
        mqtt_tcp.close()
        await mqtt_tcp.wait_closed()
        if english_mqtt_tcp is not None:
            english_mqtt_tcp.close()
            await english_mqtt_tcp.wait_closed()
        await ota_runner.cleanup()
        log.info("服务已停止")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已退出")
