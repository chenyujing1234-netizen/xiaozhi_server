"""MQTT+UDP 端到端联调（模拟一台真实的小智设备）。

完整复刻设备行为：
  1. TCP 连接 MQTT 网关，发 CONNECT
  2. PUBLISH hello(transport=udp)，收 hello 回复，拿到 udp{server,port,key,nonce}
  3. 建 UDP，把 TTS 造的 16k 语音编码成 Opus，按设备格式加密成 UDP 包上行
  4. PUBLISH listen start/stop
  5. 收 MQTT 的 stt/tts 控制消息 + UDP 下行加密音频（解密->解码统计时长）
"""
import asyncio
import json
import struct
import sys

import dashscope
from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat

sys.path.insert(0, ".")
from config import config  # noqa: E402
from xiaozhi.opus_codec import OpusEncoder, OpusDecoder  # noqa: E402
from xiaozhi.crypto import aes_ctr_crypt  # noqa: E402

dashscope.api_key = config.DASHSCOPE_API_KEY
HOST = "127.0.0.1"
QUESTION = "你好，请用一句话介绍一下你自己。"


# ---------------- MQTT 报文工具 ----------------

def enc_rl(n):
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


def mqtt_connect(client_id="test", user="xiaozhi", pwd="xiaozhi"):
    vh = b"\x00\x04MQTT\x04\xc2" + struct.pack(">H", 60)
    def s(x):
        x = x.encode()
        return struct.pack(">H", len(x)) + x
    payload = s(client_id) + s(user) + s(pwd)
    body = vh + payload
    return b"\x10" + enc_rl(len(body)) + body


def mqtt_publish(topic, payload):
    tb = topic.encode()
    pb = payload.encode() if isinstance(payload, str) else payload
    body = struct.pack(">H", len(tb)) + tb + pb
    return b"\x30" + enc_rl(len(body)) + body


async def read_packet(reader):
    first = (await reader.readexactly(1))[0]
    mult = 1
    rl = 0
    while True:
        b = (await reader.readexactly(1))[0]
        rl += (b & 0x7F) * mult
        if not (b & 0x80):
            break
        mult *= 128
    body = await reader.readexactly(rl) if rl else b""
    return first, body


def parse_publish(body):
    tl = struct.unpack(">H", body[0:2])[0]
    topic = body[2:2 + tl].decode("utf-8", "ignore")
    payload = body[2 + tl:]
    return topic, payload


# ---------------- 设备模拟 ----------------

def make_uplink_opus_frames():
    synth = SpeechSynthesizer(model=config.TTS_MODEL, voice=config.TTS_VOICE,
                              format=AudioFormat.PCM_16000HZ_MONO_16BIT)
    pcm = synth.call(QUESTION)
    print(f"[client] 造出用户语音 PCM={len(pcm)} bytes (~{len(pcm)/2/16000:.1f}s)")
    enc = OpusEncoder(sample_rate=16000, channels=1, frame_ms=60)
    return list(enc.encode_pcm_stream(pcm))


class UdpProto(asyncio.DatagramProtocol):
    def __init__(self, on_pkt):
        self.on_pkt = on_pkt

    def datagram_received(self, data, addr):
        self.on_pkt(data)


async def main():
    frames = make_uplink_opus_frames()
    dec24 = OpusDecoder(sample_rate=24000, channels=1, frame_ms=60)
    loop = asyncio.get_running_loop()

    reader, writer = await asyncio.open_connection(HOST, config.MQTT_PORT)
    writer.write(mqtt_connect())
    await writer.drain()
    t, _ = await read_packet(reader)
    print(f"[client] CONNACK 收到 (type={t>>4})")

    state = {"udp": None, "sid": None}
    hello_evt = asyncio.Event()
    downlink_pcm = 0
    flags = {"stt": False, "tts_start": False, "tts_stop": False}
    done_evt = asyncio.Event()

    async def mqtt_reader():
        try:
            while True:
                first, body = await read_packet(reader)
                if (first >> 4) != 3:  # 只关心 PUBLISH
                    continue
                _topic, payload = parse_publish(body)
                obj = json.loads(payload.decode("utf-8", "ignore"))
                typ = obj.get("type")
                if typ == "hello":
                    state["sid"] = obj.get("session_id")
                    state["udp"] = obj.get("udp")
                    print(f"[client] 收到 hello, session={state['sid']}, udp={state['udp']['server']}:{state['udp']['port']}")
                    hello_evt.set()
                elif typ == "stt":
                    flags["stt"] = True
                    print("[client] STT =", obj.get("text"))
                elif typ == "tts":
                    st = obj.get("state")
                    if st == "start":
                        flags["tts_start"] = True
                        print("[client] TTS start")
                    elif st == "sentence_start":
                        print("[client] TTS 句子 =", obj.get("text"))
                    elif st == "stop":
                        flags["tts_stop"] = True
                        print("[client] TTS stop")
                        done_evt.set()
        except asyncio.IncompleteReadError:
            pass

    reader_task = asyncio.create_task(mqtt_reader())

    # 发 hello
    hello = {"type": "hello", "version": 3, "transport": "udp",
             "features": {"mcp": True},
             "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}
    writer.write(mqtt_publish(config.MQTT_PUBLISH_TOPIC, json.dumps(hello)))
    await writer.drain()
    await asyncio.wait_for(hello_evt.wait(), timeout=5)

    udp = state["udp"]
    key = bytes.fromhex(udp["key"])
    nonce_template = bytes.fromhex(udp["nonce"])

    def on_udp(data):
        nonlocal downlink_pcm
        iv = data[:16]
        opus = aes_ctr_crypt(key, iv, data[16:])
        downlink_pcm += len(dec24.decode(opus))

    udp_transport, _ = await loop.create_datagram_endpoint(
        lambda: UdpProto(on_udp), remote_addr=(udp["server"], udp["port"]))

    # listen start
    writer.write(mqtt_publish(config.MQTT_PUBLISH_TOPIC,
        json.dumps({"type": "listen", "state": "start", "mode": "auto", "session_id": state["sid"]})))
    await writer.drain()

    # 上行加密音频
    seq = 0
    for f in frames:
        seq += 1
        nonce = bytearray(nonce_template)
        struct.pack_into(">H", nonce, 2, len(f) & 0xFFFF)
        struct.pack_into(">I", nonce, 8, 0)        # timestamp（设备上行通常为0）
        struct.pack_into(">I", nonce, 12, seq)
        cipher = aes_ctr_crypt(key, bytes(nonce), f)
        udp_transport.sendto(bytes(nonce) + cipher)
        await asyncio.sleep(0.06)

    # listen stop
    writer.write(mqtt_publish(config.MQTT_PUBLISH_TOPIC,
        json.dumps({"type": "listen", "state": "stop", "session_id": state["sid"]})))
    await writer.drain()
    print("[client] 上行完毕，已发 listen stop")

    try:
        await asyncio.wait_for(done_evt.wait(), timeout=40)
    except asyncio.TimeoutError:
        print("[client] 等待 TTS stop 超时")

    await asyncio.sleep(0.5)
    reader_task.cancel()
    udp_transport.close()
    writer.close()

    secs = downlink_pcm / 2 / 24000
    print("=" * 50)
    print(f"结果: stt={flags['stt']} tts_start={flags['tts_start']} tts_stop={flags['tts_stop']}")
    print(f"下行音频(UDP解密+解码)时长 ≈ {secs:.1f}s ({downlink_pcm} bytes PCM)")
    ok = all(flags.values()) and downlink_pcm > 0
    print("MQTT+UDP 端到端链路:", "✅ 成功" if ok else "❌ 有问题")


if __name__ == "__main__":
    asyncio.run(main())
