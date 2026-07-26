"""Opus 编解码封装。

设备端（ESP32）协议约定：
  - 上行：每个 WebSocket 二进制帧 = 一个 Opus 包 = 60ms 的 16kHz 单声道音频
  - 下行：同样是一个个 Opus 包，我们用 24kHz 单声道

这里把 PCM <-> Opus 的转换、以及按 60ms 分帧的逻辑都封装好。
"""
import logging

import opuslib

logger = logging.getLogger("opus")


class OpusDecoder:
    """把设备上行的 Opus 包解成 16k PCM（int16 小端字节）。"""

    def __init__(self, sample_rate: int = 16000, channels: int = 1, frame_ms: int = 60):
        self.sample_rate = sample_rate
        self.channels = channels
        # 单帧（每声道）样本数，60ms * 16000 = 960
        self.frame_size = int(sample_rate * frame_ms / 1000)
        self._dec = opuslib.Decoder(sample_rate, channels)

    def decode(self, opus_bytes: bytes) -> bytes:
        """返回 PCM16 字节；解码失败返回空字节。"""
        if not opus_bytes:
            return b""
        try:
            return self._dec.decode(opus_bytes, self.frame_size)
        except Exception as e:  # noqa: BLE001
            logger.warning("Opus 解码失败: %s", e)
            return b""


class OpusEncoder:
    """把 TTS 产生的 24k PCM 编码成一个个 60ms 的 Opus 包。"""

    def __init__(self, sample_rate: int = 24000, channels: int = 1, frame_ms: int = 60):
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_size = int(sample_rate * frame_ms / 1000)  # 24000*60/1000 = 1440
        self.bytes_per_frame = self.frame_size * channels * 2  # int16
        self._enc = opuslib.Encoder(sample_rate, channels, opuslib.APPLICATION_VOIP)
        self._carry = b""

    def reset(self):
        """新一轮下行音频开始前清空帧间残留。"""
        self._carry = b""

    def encode_pcm_stream(self, pcm_bytes: bytes, *, flush: bool = False):
        """把 PCM16 切成 60ms 一帧编码为 Opus。

        流式输入时（如 Omni audio.delta）会把不足一帧的尾部缓存在 _carry，
        等下一段 PCM 补齐后再编码，避免在分片边界补零造成杂音。
        flush=True 时在末尾补齐最后一帧（一轮回复结束时调用）。
        """
        data = self._carry + (pcm_bytes or b"")
        self._carry = b""
        if not data:
            return

        step = self.bytes_per_frame
        offset = 0
        total = len(data)
        while offset + step <= total:
            chunk = data[offset:offset + step]
            offset += step
            try:
                yield self._enc.encode(chunk, self.frame_size)
            except Exception as e:  # noqa: BLE001
                logger.warning("Opus 编码失败: %s", e)

        remainder = data[offset:]
        if remainder:
            if flush:
                chunk = remainder + b"\x00" * (step - len(remainder))
                try:
                    yield self._enc.encode(chunk, self.frame_size)
                except Exception as e:  # noqa: BLE001
                    logger.warning("Opus 编码失败: %s", e)
            else:
                self._carry = remainder
