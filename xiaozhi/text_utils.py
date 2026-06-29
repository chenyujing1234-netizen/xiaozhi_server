"""文本处理小工具：把流式 LLM 输出按句切分，方便逐句送 TTS。"""

# 句子结束标点（中英文，遇到即切，不限字数）
_END_PUNCTS = "。！？!?；;\n."
# 软分句标点：累计字数达到阈值后，遇到这些标点也可先送 TTS
_SOFT_PUNCTS = "，,、：:"


class SentenceSplitter:
    """喂入增量文本，吐出完整句子。"""

    def __init__(self, min_chars: int = 10):
        self._buf = ""
        self._min_chars = max(1, min_chars)

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
        for i, ch in enumerate(self._buf):
            if ch in _END_PUNCTS:
                return i
        if len(self._buf) >= self._min_chars:
            for i, ch in enumerate(self._buf):
                if ch in _SOFT_PUNCTS:
                    return i
        return -1
