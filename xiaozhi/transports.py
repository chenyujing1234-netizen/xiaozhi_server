"""传输层：把 Session 的“发 JSON / 发音频 / 生成 hello”落到具体协议上。

两种实现：
  - WebSocketTransport：JSON 走文本帧，音频走二进制帧（裸 Opus，协议版本1）
  - MqttUdpTransport：JSON 走 MQTT PUBLISH，音频走 UDP（AES-CTR 加密的 Opus 包）
"""
import json
import logging
import os
import struct

from config import config
from .crypto import aes_ctr_crypt

logger = logging.getLogger("transport")


def _audio_params() -> dict:
    return {
        "format": "opus",
        "sample_rate": config.DOWNLINK_SAMPLE_RATE,
        "channels": config.CHANNELS,
        "frame_duration": config.FRAME_DURATION_MS,
    }


class WebSocketTransport:
    def __init__(self, websocket):
        self.ws = websocket
        self.session = None

    async def send_json(self, obj: dict):
        await self.ws.send(json.dumps(obj, ensure_ascii=False))

    async def send_audio(self, opus_frame: bytes):
        await self.ws.send(opus_frame)

    def make_server_hello(self, session_id: str) -> dict:
        return {
            "type": "hello",
            "transport": "websocket",
            "session_id": session_id,
            "audio_params": _audio_params(),
        }

    def on_session_closed(self):
        pass


class MqttUdpTransport:
    """一条 MQTT 连接对应一个设备会话；音频走 UDP。"""

    def __init__(self, mqtt_conn, udp_server, loop):
        self.mqtt = mqtt_conn
        self.udp = udp_server
        self.loop = loop
        self.session = None

        # UDP 加密相关（在 make_server_hello 里分配）
        self.key: bytes = b""
        self.nonce_template: bytes = b""
        self.conn_id: int = 0
        self.device_addr = None  # 从上行 UDP 包学习
        self._tx_seq = 0
        self._tx_timestamp = 0
        self.downlink_topic = config.MQTT_PUBLISH_TOPIC

    # ---- 发送 ----

    async def send_json(self, obj: dict):
        payload = json.dumps(obj, ensure_ascii=False)
        await self.mqtt.publish(self.downlink_topic, payload)

    async def send_audio(self, opus_frame: bytes):
        if self.device_addr is None:
            logger.debug("尚未获知设备 UDP 地址，丢弃下行音频帧")
            return
        self._tx_seq += 1
        header = bytearray(16)
        header[0] = 0x01                      # type
        header[1] = 0x00                      # flags
        struct.pack_into(">H", header, 2, len(opus_frame) & 0xFFFF)  # payload_len
        header[4:8] = self.conn_id.to_bytes(4, "big")                 # ssrc（用于路由）
        struct.pack_into(">I", header, 8, self._tx_timestamp & 0xFFFFFFFF)
        struct.pack_into(">I", header, 12, self._tx_seq & 0xFFFFFFFF)
        cipher = aes_ctr_crypt(self.key, bytes(header), opus_frame)
        self.udp.sendto(bytes(header) + cipher, self.device_addr)
        self._tx_timestamp += config.FRAME_DURATION_MS

    # ---- hello：分配密钥/会话，并注册 UDP 路由 ----

    def make_server_hello(self, session_id: str) -> dict:
        self.key = os.urandom(16)
        self.conn_id = int.from_bytes(os.urandom(4), "big")
        # nonce 模板 16 字节：type=01, flags=00, len=0000, ssrc=conn_id, 其余0
        nonce = bytearray(16)
        nonce[0] = 0x01
        nonce[4:8] = self.conn_id.to_bytes(4, "big")
        self.nonce_template = bytes(nonce)
        self._tx_seq = 0
        self._tx_timestamp = 0

        self.udp.register(self.conn_id, self)

        return {
            "type": "hello",
            "transport": "udp",
            "session_id": session_id,
            "audio_params": _audio_params(),
            "udp": {
                "server": config.PUBLIC_HOST,
                "port": config.UDP_PORT,
                "key": self.key.hex().upper(),
                "nonce": self.nonce_template.hex().upper(),
            },
        }

    # ---- 接收上行 UDP（由 UdpServer 在事件循环线程里调用）----

    def on_incoming_udp(self, data: bytes, addr):
        self.device_addr = addr
        if len(data) < 16:
            return
        iv = data[:16]
        cipher = data[16:]
        try:
            opus = aes_ctr_crypt(self.key, iv, cipher)
        except Exception as e:  # noqa: BLE001
            logger.warning("解密上行音频失败: %s", e)
            return
        if self.session is not None:
            self.session.handle_binary(opus)

    def on_session_closed(self):
        if self.conn_id:
            self.udp.unregister(self.conn_id)
