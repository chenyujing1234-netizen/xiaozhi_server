"""语音合成（TTS），基于 DashScope CosyVoice。

按“一句话”为单位合成，返回 24kHz 单声道的 PCM16 字节，
上层再用 Opus 编码后分帧发给设备。

这是同步阻塞调用，调用方应放到线程里跑。
"""
import logging

from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat

logger = logging.getLogger("tts")


def synthesize(text: str, model: str, voice: str) -> bytes:
    """把一段文本合成为 PCM16(24k) 字节。失败返回空字节。"""
    text = (text or "").strip()
    if not text:
        return b""
    try:
        logger.info("[pipeline] TTS 请求开始 model=%s voice=%s text=%s", model, voice, text)
        synthesizer = SpeechSynthesizer(
            model=model,
            voice=voice,
            format=AudioFormat.PCM_24000HZ_MONO_16BIT,
        )
        audio = synthesizer.call(text)
        if audio is None:
            logger.warning("[pipeline] TTS 返回空音频: %s", text)
            return b""
        logger.info("[pipeline] TTS 合成完成 pcm=%d bytes: %s", len(audio), text)
        return audio
    except Exception as e:  # noqa: BLE001
        logger.error("[pipeline] TTS 合成失败: %s (text=%s)", e, text)
        return b""
