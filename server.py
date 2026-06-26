"""小智 ESP32 服务端入口。

同时启动两个服务：
  - OTA HTTP 配置接口（把设备引导到本服务端的 WebSocket）
  - WebSocket 语音通道（ASR -> LLM -> TTS 全链路）

运行：
  python server.py
"""
import asyncio
import logging

import dashscope

from config import config
from xiaozhi.ota_server import start_ota_server
from xiaozhi.ws_server import start_ws_server
from xiaozhi.udp_server import UdpServer
from xiaozhi.mqtt_broker import MqttBroker


def setup_logging():
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # 降低第三方库噪音
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
    log.info("LLM 模型: %s", config.LLM_MODEL)
    log.info("ASR 模型: %s", config.ASR_MODEL)
    log.info("TTS 模型: %s (音色=%s)", config.TTS_MODEL, config.TTS_VOICE)
    log.info("设备传输方式(TRANSPORT): %s", config.TRANSPORT)
    log.info("对外地址(PUBLIC_HOST): %s", config.PUBLIC_HOST)
    log.info("=" * 60)

    loop = asyncio.get_running_loop()

    # OTA 配置接口（把设备引导到正确的语音通道）
    ota_runner = await start_ota_server()

    # WebSocket 通道（TRANSPORT=websocket 时使用）
    ws_server = await start_ws_server()

    # MQTT + UDP 通道（设备默认，TRANSPORT=mqtt 时使用）
    udp_server = UdpServer(loop)
    await udp_server.start(config.UDP_HOST, config.UDP_PORT)
    mqtt_broker = MqttBroker(loop, udp_server)
    mqtt_tcp = await mqtt_broker.start(config.MQTT_HOST, config.MQTT_PORT)

    log.info("服务已就绪。按 Ctrl+C 退出。")
    try:
        await asyncio.Future()  # 永久运行
    except asyncio.CancelledError:
        pass
    finally:
        ws_server.close()
        await ws_server.wait_closed()
        mqtt_tcp.close()
        await mqtt_tcp.wait_closed()
        await ota_runner.cleanup()
        log.info("服务已停止")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已退出")
