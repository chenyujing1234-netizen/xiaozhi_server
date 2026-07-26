"""UDP 音频服务端。

设备上行的加密 Opus 包都发到这里。包结构（前 16 字节明文头）：
  type(1) flags(1) payload_len(2) ssrc(4) timestamp(4) sequence(4) | 加密载荷

我们用明文头里的 ssrc（4 字节，等于 hello 时分配的 conn_id）把包路由到对应会话。
解密、喂给会话、以及下行发送，都由对应的 MqttUdpTransport 负责。
"""
import asyncio
import logging

logger = logging.getLogger("udp")


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: "UdpServer"):
        self._server = server

    def connection_made(self, transport):
        self._server._datagram_transport = transport

    def datagram_received(self, data: bytes, addr):
        self._server.on_datagram(data, addr)

    def error_received(self, exc):
        logger.debug("UDP error: %s", exc)


class UdpServer:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.port = 0
        self._routes = {}  # conn_id(int) -> MqttUdpTransport
        self._datagram_transport = None

    async def start(self, host: str, port: int):
        self.port = port
        await self.loop.create_datagram_endpoint(
            lambda: _UdpProtocol(self),
            local_addr=(host, port),
        )
        logger.info("UDP 音频服务监听 %s:%d", host, port)

    def register(self, conn_id: int, transport):
        self._routes[conn_id] = transport
        logger.debug("注册 UDP 路由 conn_id=%d", conn_id)

    def unregister(self, conn_id: int):
        self._routes.pop(conn_id, None)

    def on_datagram(self, data: bytes, addr):
        if len(data) < 16:
            return
        conn_id = int.from_bytes(data[4:8], "big")
        transport = self._routes.get(conn_id)
        if transport is None:
            logger.debug("收到未知 conn_id=%d 的 UDP 包，来自 %s", conn_id, addr)
            return
        transport.on_incoming_udp(data, addr)

    def sendto(self, data: bytes, addr):
        if self._datagram_transport is not None and addr is not None:
            self._datagram_transport.sendto(data, addr)
