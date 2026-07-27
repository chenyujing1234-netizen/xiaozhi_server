"""上行音频静音 / VAD 判定（默认通道与英语通道共用）。

职责：
- 按 RMS 阈值统计连续高/低能量帧
- 维护会话级静音计时（超时 → goodbye / idle）
- 可选：维护「一句说完」静音计时（默认通道结束 ASR 用）
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from config import config
from .audio_utils import is_speech, pcm_rms

logger = logging.getLogger("vad")


@dataclass
class VadFrameResult:
    rms: float
    is_speech: bool
    active_speech: bool
    quiet_sec: float
    frame_count: int
    consecutive_speech_frames: int
    low_rms_frames: int


class SilenceGate:
    """共享静音门控状态机。"""

    def __init__(
        self,
        *,
        channel: str,
        session_id: str,
        log_enabled: bool = False,
        track_utterance: bool = False,
        threshold: Optional[float] = None,
        frames_required: Optional[int] = None,
    ):
        self.channel = channel
        self.session_id = session_id
        self.log_enabled = log_enabled
        self.track_utterance = track_utterance
        self.threshold = (
            float(threshold)
            if threshold is not None
            else float(config.SPEECH_RMS_THRESHOLD)
        )
        self.frames_required = (
            int(frames_required)
            if frames_required is not None
            else int(config.SPEECH_FRAMES_REQUIRED)
        )
        self.quiet_since: Optional[float] = None
        self.consecutive_speech_frames = 0
        self.low_rms_frames = 0
        self.rms_log_counter = 0
        self.frame_count = 0
        self.utterance_had_speech = False
        self.utterance_quiet_since: Optional[float] = None
        self.reset(mark_quiet_now=False)

    def reset(self, *, mark_quiet_now: bool = True) -> None:
        """进入聆听 / 重新开始一轮时调用。"""
        self.quiet_since = time.time() if mark_quiet_now else None
        self.consecutive_speech_frames = 0
        self.low_rms_frames = 0
        self.rms_log_counter = 0
        self.frame_count = 0
        self.utterance_had_speech = False
        self.utterance_quiet_since = None

    def mark_after_playback(self) -> None:
        """导师/助手播完，恢复聆听时重新开始静音计时。"""
        self.quiet_since = time.time()
        self.consecutive_speech_frames = 0
        self.low_rms_frames = 0

    def mark_utterance_active(self) -> None:
        self.utterance_had_speech = True

    def clear_utterance(self) -> None:
        self.utterance_had_speech = False
        self.utterance_quiet_since = None

    @property
    def active_speech(self) -> bool:
        return self.consecutive_speech_frames >= self.frames_required

    @property
    def quiet_sec(self) -> float:
        if self.quiet_since is None:
            return 0.0
        return max(0.0, time.time() - self.quiet_since)

    @property
    def utterance_quiet_sec(self) -> float:
        if self.utterance_quiet_since is None:
            return 0.0
        return max(0.0, time.time() - self.utterance_quiet_since)

    def on_frame(self, pcm: bytes) -> VadFrameResult:
        """处理一帧 PCM，更新静音状态并按需打 RMS 日志。"""
        self.frame_count += 1
        rms = pcm_rms(pcm)
        speech = is_speech(pcm, self.threshold)

        if speech:
            self.consecutive_speech_frames += 1
            self.low_rms_frames = 0
            if self.consecutive_speech_frames >= self.frames_required:
                # 确认在说话：清掉会话静音与句末静音
                self.quiet_since = None
                if self.track_utterance:
                    self.utterance_quiet_since = None
        else:
            was_active = self.consecutive_speech_frames >= self.frames_required
            self.consecutive_speech_frames = 0
            self.low_rms_frames += 1
            now = time.time()
            # 任意静音帧：若尚未计时则开始（含「从未说过话」）
            if self.quiet_since is None:
                self.quiet_since = now
            # 从有效语音刚落入静音：开始「一句说完」计时
            if (
                self.track_utterance
                and was_active
                and self.utterance_had_speech
                and self.utterance_quiet_since is None
            ):
                self.utterance_quiet_since = now

        result = VadFrameResult(
            rms=rms,
            is_speech=speech,
            active_speech=self.active_speech,
            quiet_sec=self.quiet_sec,
            frame_count=self.frame_count,
            consecutive_speech_frames=self.consecutive_speech_frames,
            low_rms_frames=self.low_rms_frames,
        )
        self._maybe_log(result)
        return result

    def _maybe_log(self, result: VadFrameResult) -> None:
        if not self.log_enabled:
            return
        self.rms_log_counter += 1
        if self.rms_log_counter != 1 and self.rms_log_counter % 17 != 0:
            return
        logger.info(
            "[%s][%s] 上行音频 #%d RMS=%.0f 阈值=%.0f speech=%s "
            "连续高RMS帧=%d 连续低RMS帧=%d 静音计时=%.1fs/%.0fs",
            self.channel,
            self.session_id,
            result.frame_count,
            result.rms,
            self.threshold,
            result.is_speech,
            result.consecutive_speech_frames,
            result.low_rms_frames,
            result.quiet_sec,
            config.SILENCE_TIMEOUT_SEC,
        )

    def idle_timeout_due(self, timeout_sec: Optional[float] = None) -> bool:
        limit = (
            float(timeout_sec)
            if timeout_sec is not None
            else float(config.SILENCE_TIMEOUT_SEC)
        )
        return self.quiet_since is not None and self.quiet_sec >= limit

    def utterance_end_due(self, end_sec: Optional[float] = None) -> bool:
        if not self.track_utterance or not self.utterance_had_speech:
            return False
        if self.utterance_quiet_since is None:
            return False
        limit = (
            float(end_sec)
            if end_sec is not None
            else float(config.UTTERANCE_END_SILENCE_SEC)
        )
        return self.utterance_quiet_sec >= limit


class EmptyTurnTracker:
    """连续「有输入迹象但无有效文本」计数（默认 ASR / 英语 Omni 共用）。

    - 默认通道：VAD 判定说完但 ASR 无文本
    - 英语通道：音频已送 Omni，但本轮无用户转写
    达到 limit 次 → 应下发 goodbye / idle。
    """

    def __init__(
        self,
        *,
        channel: str,
        session_id: str,
        limit: Optional[int] = None,
    ):
        self.channel = channel
        self.session_id = session_id
        self.limit = (
            int(limit) if limit is not None else int(config.EMPTY_UTTERANCE_LIMIT)
        )
        self.streak = 0

    def reset(self) -> None:
        self.streak = 0

    def note_text(self) -> None:
        """本轮拿到有效文本，清零计数。"""
        if self.streak:
            logger.info(
                "[%s][%s] 本轮有有效文本，清空空转计数（原 %d）",
                self.channel,
                self.session_id,
                self.streak,
            )
        self.streak = 0

    def note_empty(self, *, reason: str = "") -> bool:
        """记一次空转。返回 True 表示已达上限，应 idle。"""
        self.streak += 1
        logger.info(
            "[%s][%s] 空转 %d/%d%s",
            self.channel,
            self.session_id,
            self.streak,
            self.limit,
            f"（{reason}）" if reason else "",
        )
        return self.streak >= self.limit


class WatchdogTask:
    """静音 watchdog 任务启停（各会话共用）。"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, coro) -> None:
        self.stop()
        self._task = asyncio.create_task(coro)

    def stop(self) -> None:
        """停止 watchdog。若在 watchdog 自身回调里调用，只摘掉引用、不 cancel 自己，
        避免 CancelledError 打断 goodbye 下发（英语空转 idle 曾踩坑）。"""
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        try:
            current = asyncio.current_task()
        except Exception:  # noqa: BLE001
            current = None
        if current is task:
            return
        task.cancel()


async def silence_watchdog_loop(
    gate: SilenceGate,
    *,
    is_busy: Callable[[], bool],
    should_run: Callable[[], bool],
    on_idle_timeout: Callable[[], Awaitable[None]],
    on_utterance_end: Optional[Callable[[], Awaitable[None]]] = None,
    break_on_utterance_end: bool = True,
    on_tick: Optional[Callable[[], Awaitable[bool]]] = None,
    poll_sec: float = 0.3,
) -> None:
    """通用静音轮询：句末静音回调（可选）+ 会话静音超时。

    - should_run: 是否仍处于聆听等可继续状态
    - is_busy: 正在播报/回应时跳过本轮检查（不重置计时）
    - break_on_utterance_end: 默认通道 finalize 后结束；英语通道可设 False 继续等 Omni
    - on_tick: 每轮额外检查，返回 True 则结束 watchdog（如 Omni 转写等待超时）
    """
    try:
        while should_run():
            await asyncio.sleep(poll_sec)
            if not should_run() or is_busy():
                continue
            if on_utterance_end is not None and gate.utterance_end_due():
                await on_utterance_end()
                if break_on_utterance_end:
                    break
            if on_tick is not None and await on_tick():
                break
            if gate.idle_timeout_due():
                await on_idle_timeout()
                break
    except asyncio.CancelledError:
        pass
