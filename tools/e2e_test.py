"""端到端联调脚本（模拟一台 ESP32 设备）。

流程：
  1. 连接 WS，完成 hello 握手
  2. 用 TTS 合成一句"用户提问"的 16k 语音，编码成 Opus 帧当作设备上行
  3. 发送 listen start + 上行音频，再发 listen stop
  4. 接收服务端返回的 stt / tts 控制消息 + 下行 Opus 音频，解码统计时长

这个脚本只用于在没有真机时验证整条链路是否打通。
"""
import asyncio
import json
import sys

import websockets
import dashscope
from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat

sys.path.insert(0, ".")
from config import config  # noqa: E402
from xiaozhi.opus_codec import OpusEncoder, OpusDecoder  # noqa: E402

dashscope.api_key = config.DASHSCOPE_API_KEY
WS = "ws://127.0.0.1:8000/xiaozhi/v1/"
QUESTION = "你好，请用一句话介绍一下你自己。"


def make_uplink_opus_frames():
    """用 TTS 造一段 16k 语音，编码成设备上行 Opus 帧（16k/60ms）。"""
    synth = SpeechSynthesizer(model=config.TTS_MODEL, voice=config.TTS_VOICE,
                              format=AudioFormat.PCM_16000HZ_MONO_16BIT)
    pcm = synth.call(QUESTION)
    print(f"[client] 造出用户语音 PCM={len(pcm)} bytes (~{len(pcm)/2/16000:.1f}s)")
    enc = OpusEncoder(sample_rate=16000, channels=1, frame_ms=60)
    return list(enc.encode_pcm_stream(pcm))


async def main():
    frames = make_uplink_opus_frames()
    dec24 = OpusDecoder(sample_rate=24000, channels=1, frame_ms=60)

    async with websockets.connect(WS, additional_headers={
            "Device-Id": "test:device", "Client-Id": "e2e", "Protocol-Version": "1"}) as ws:
        await ws.send(json.dumps({"type": "hello", "version": 1, "transport": "websocket",
            "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}))
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        sid = hello.get("session_id")
        print("[client] 收到服务端 hello, session_id =", sid)

        await ws.send(json.dumps({"type": "listen", "state": "start", "mode": "auto", "session_id": sid}))

        # 边发上行音频（接近实时）
        async def send_audio():
            for f in frames:
                await ws.send(f)
                await asyncio.sleep(0.06)
            # 主动告诉服务端说完了（让 ASR 尽快出最终结果）
            await ws.send(json.dumps({"type": "listen", "state": "stop", "session_id": sid}))
            print("[client] 上行音频发送完毕，已发 listen stop")

        send_task = asyncio.create_task(send_audio())

        # 收服务端返回
        downlink_pcm = 0
        got_stt = got_tts_start = got_tts_stop = False
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=30)
                if isinstance(msg, (bytes, bytearray)):
                    downlink_pcm += len(dec24.decode(bytes(msg)))
                else:
                    obj = json.loads(msg)
                    t = obj.get("type")
                    if t == "stt":
                        got_stt = True
                        print("[client] STT(识别到的用户话) =", obj.get("text"))
                    elif t == "tts":
                        st = obj.get("state")
                        if st == "start":
                            got_tts_start = True
                            print("[client] TTS start")
                        elif st == "sentence_start":
                            print("[client] TTS 句子 =", obj.get("text"))
                        elif st == "stop":
                            got_tts_stop = True
                            print("[client] TTS stop")
                            break
        except asyncio.TimeoutError:
            print("[client] 等待超时")
        await send_task

        secs = downlink_pcm / 2 / 24000
        print("=" * 50)
        print(f"结果: stt={got_stt} tts_start={got_tts_start} tts_stop={got_tts_stop}")
        print(f"下行音频解码时长 ≈ {secs:.1f}s ({downlink_pcm} bytes PCM)")
        ok = got_stt and got_tts_start and got_tts_stop and downlink_pcm > 0
        print("端到端链路:", "✅ 成功" if ok else "❌ 有问题")


if __name__ == "__main__":
    asyncio.run(main())
