"""简单音频分析：用 PCM 能量判断是否为有效语音（轻量 VAD）。"""
import struct


def pcm_rms(pcm: bytes) -> float:
    """计算 16-bit 小端 PCM 的 RMS 能量。pcm 为空返回 0。"""
    if not pcm:
        return 0.0
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    ssum = sum(s * s for s in samples)
    return (ssum / n) ** 0.5


def is_speech(pcm: bytes, threshold: float) -> bool:
    """能量超过阈值视为有效语音。"""
    return pcm_rms(pcm) >= threshold
