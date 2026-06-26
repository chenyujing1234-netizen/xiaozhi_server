"""文本处理小工具：把流式 LLM 输出按句切分，方便逐句送 TTS。"""

# 句子结束标点（中英文）
_END_PUNCTS = "。！？!?；;\n"
# 长句兜底切分标点（避免一句太长导致 TTS 首包延迟）
_SOFT_PUNCTS = "，,、："
_SOFT_SPLIT_THRESHOLD = 40


class SentenceSplitter:
    """喂入增量文本，吐出完整句子。"""

    def __init__(self):
        self._buf = ""

    def feed(self, delta: str):
        """喂入一段增量文本，返回本次可以立即合成的完整句子列表。"""
        self._buf += delta
        sentences = []
        while True:
            cut = self._find_cut()
            if cut == -1:
                break
            sentence = self._buf[: cut + 1].strip()
            self._buf = self._buf[cut + 1:]
            if sentence:
                sentences.append(sentence)
        return sentences

    def flush(self):
        """对话结束时，把剩余不完整的内容作为最后一句吐出。"""
        rest = self._buf.strip()
        self._buf = ""
        return rest

    def _find_cut(self) -> int:
        # 优先找硬结束标点
        for i, ch in enumerate(self._buf):
            if ch in _END_PUNCTS:
                return i
        # 句子过长时，用软标点兜底切一刀
        if len(self._buf) >= _SOFT_SPLIT_THRESHOLD:
            for i in range(_SOFT_SPLIT_THRESHOLD - 1, len(self._buf)):
                if self._buf[i] in _SOFT_PUNCTS:
                    return i
        return -1
