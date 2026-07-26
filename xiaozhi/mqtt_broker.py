"""一个极简的 MQTT 网关（仅满足小智设备所需）。

为什么要自己写而不是用 Mosquitto？
  设备端**从不订阅任何主题**，它只 PUBLISH 自己的消息，然后期望服务器把回复
  直接 PUBLISH 回它这条连接上。esp-mqtt 客户端对收到的任何 PUBLISH 都会触发
  回调（不管有没有订阅）。所以官方用的是一个“自定义网关”，而不是标准 broker。
  这里就实现这样一个网关：每条 TCP 连接 = 一个设备会话，直接往该连接推 PUBLISH。

只实现了必要的 MQTT 3.1.1 报文：CONNECT/CONNACK、PUBLISH(QoS0)、
SUBSCRIBE/SUBACK、PINGREQ/PINGRESP、DISCONNECT。
"""
import asyncio
import logging
import struct

from config import config
from .session import Session
from .transports import MqttUdpTransport

logger = logging.getLogger("mqtt")

# 报文类型（高 4 位）
CONNECT = 1
CONNACK = 2
PUBLISH = 3
PUBACK = 4
SUBSCRIBE = 8
SUBACK = 9
PINGREQ = 12
PINGRESP = 13
DISCONNECT = 14


def _encode_remaining_length(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n > 0:
            b |= 0x80
        out.append(b)
        if n == 0:
            break
    return bytes(out)


async def _read_remaining_length(reader: asyncio.StreamReader) -> int:
    multiplier = 1
    value = 0
    while True:
        b = (await reader.readexactly(1))[0]
        value += (b & 0x7F) * multiplier
        if not (b & 0x80):
            break
        multiplier *= 128
        if multiplier > 128 ** 4:
            raise ValueError("malformed remaining length")
    return value


def build_publish(topic: str, payload) -> bytes:
    tb = topic.encode("utf-8")
    pb = payload.encode("utf-8") if isinstance(payload, str) else payload
    body = struct.pack(">H", len(tb)) + tb + pb  # QoS0：无 packet id
    return bytes([0x30]) + _encode_remaining_length(len(body)) + body


def _parse_connect_client_id(body: bytes) -> str:
    try:
        # 变长头：协议名(2+len) + level(1) + flags(1) + keepalive(2)
        pn_len = struct.unpack(">H", body[0:2])[0]
        idx = 2 + pn_len + 1 + 1 + 2
        cid_len = struct.unpack(">H", body[idx:idx + 2])[0]
        idx += 2
        return body[idx:idx + cid_len].decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return "unknown"


def _parse_publish(header_byte: int, body: bytes):
    qos = (header_byte >> 1) & 0x03
    topic_len = struct.unpack(">H", body[0:2])[0]
    idx = 2
    topic = body[idx:idx + topic_len].decode("utf-8", "ignore")
    idx += topic_len
    if qos > 0:
        idx += 2  # 跳过 packet id
    payload = body[idx:]
    return topic, payload, qos


def _build_suback(body: bytes) -> bytes:
    packet_id = body[0:2]
    # 统计订阅的主题数，每个回 0x00（最大 QoS0 授予）
    idx = 2
    count = 0
    while idx < len(body):
        tl = struct.unpack(">H", body[idx:idx + 2])[0]
        idx += 2 + tl + 1  # topic + 1 字节 qos
        count += 1
    payload = packet_id + bytes([0x00] * max(count, 1))
    return bytes([0x90]) + _encode_remaining_length(len(payload)) + payload


class MqttConnection:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self._wlock = asyncio.Lock()

    async def _send(self, data: bytes):
        async with self._wlock:
            self.writer.write(data)
            await self.writer.drain()

    async def publish(self, topic: str, payload):
        await self._send(build_publish(topic, payload))

    async def send_connack(self):
        await self._send(bytes([0x20, 0x02, 0x00, 0x00]))

    async def send_pingresp(self):
        await self._send(bytes([0xD0, 0x00]))

    async def send_suback(self, body: bytes):
        await self._send(_build_suback(body))


class MqttBroker:
    def __init__(self, loop, udp_server, session_class=Session, name: str = "default"):
        self.loop = loop
        self.udp_server = udp_server
        self.session_class = session_class
        self.name = name

    async def start(self, host: str, port: int):
        server = await asyncio.start_server(self._handle_client, host, port)
        logger.info("MQTT 网关[%s] 监听 %s:%d", self.name, host, port)
        return server

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        conn = MqttConnection(reader, writer)
        session = None
        try:
            while True:
                try:
                    first = (await reader.readexactly(1))[0]
                except asyncio.IncompleteReadError:
                    break
                rl = await _read_remaining_length(reader)
                body = await reader.readexactly(rl) if rl else b""
                ptype = first >> 4

                if ptype == CONNECT:
                    client_id = _parse_connect_client_id(body)
                    await conn.send_connack()
                    transport = MqttUdpTransport(conn, self.udp_server, self.loop)
                    session = self.session_class(
                        transport, self.loop, device_id=client_id, client_id=client_id
                    )
                    logger.info(
                        "设备 MQTT[%s] 已连接 client_id=%s from=%s session=%s",
                        self.name, client_id, peer, session.session_id,
                    )
                elif ptype == PUBLISH:
                    topic, payload, _qos = _parse_publish(first, body)
                    if session is not None:
                        await session.handle_text(payload.decode("utf-8", "ignore"))
                elif ptype == PINGREQ:
                    await conn.send_pingresp()
                elif ptype == SUBSCRIBE:
                    await conn.send_suback(body)
                elif ptype == DISCONNECT:
                    logger.info("设备发送 DISCONNECT")
                    break
                elif ptype == PUBACK:
                    pass
                else:
                    logger.debug("未处理的 MQTT 报文类型: %d", ptype)
        except asyncio.IncompleteReadError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.exception("MQTT 连接异常: %s", e)
        finally:
            if session is not None:
                await session.cleanup()
                logger.info("MQTT 会话已清理 session=%s", session.session_id)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
