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

    def encode_pcm_stream(self, pcm_bytes: bytes):
        """把任意长度的 PCM16 字节，切成 60ms 一帧，逐帧编码并 yield Opus 包。

        最后不足一帧的部分会补零后编码（保证尾音不丢）。
        """
        if not pcm_bytes:
            return
        step = self.bytes_per_frame
        total = len(pcm_bytes)
        offset = 0
        while offset < total:
            chunk = pcm_bytes[offset:offset + step]
            offset += step
            if len(chunk) < step:
                chunk = chunk + b"\x00" * (step - len(chunk))
            try:
                yield self._enc.encode(chunk, self.frame_size)
            except Exception as e:  # noqa: BLE001
                logger.warning("Opus 编码失败: %s", e)
                continue
