"""英语 cheap 轮：文本 LLM + TTS（不经 Omni Realtime）。"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Callable, Awaitable, List, Dict

from config import config
from xiaozhi import runtime_config as rc
from .english_profile import build_instructions
from .providers import llm as llm_provider
from .providers import tts as tts_provider
from .text_utils import SentenceSplitter

if TYPE_CHECKING:
    from .english_session import EnglishSession

logger = logging.getLogger("english.text")

PROACTIVE_GREETING_LLM_USER = (
    "[Session start: the kid has not spoken yet. "
    "Speak first as XiaoYu (小语). One or two short spoken sentences: "
    "friendly greeting + ONE very easy question they can answer in Chinese "
    "or with one English word. Offer A-or-B choices when helpful. "
    "Topics: games, food, pets, something funny today. "
    "Do NOT mention English class, practice, studying, or being a tutor. "
    "Example vibe: \"Hey! Quick one — pizza or noodles for lunch?\"]"
)


def build_chat_messages(
    session: "EnglishSession",
    user_text: str,
    *,
    enable_search: bool = False,
) -> List[Dict[str, str]]:
    instructions = build_instructions(
        session._profile,
        history_context=session._history_context,
    )
    if enable_search:
        instructions += (
            "\n\nWEB SEARCH: This question needs up-to-date facts (news, weather, "
            "date, etc.). Use the search tool results. Answer accurately, then teach "
            "useful English phrases related to the topic if appropriate."
        )
    if session._pending_image_b64:
        instructions += (
            "\n\nNote: A photo was attached but this turn uses text-only mode. "
            "If they ask about the image, ask them to describe it or switch to pronunciation practice."
        )
    messages: List[Dict[str, str]] = [{"role": "system", "content": instructions}]
    messages[0]["content"] += (
        "\n\nVOICE LATENCY: Start with one short spoken line (under ~20 words). "
        "When the student might be shy or stuck, end with an easy question (yes/no or A/B). "
        "Then add detail only if needed. "
        "Do not open with meta phrases like (slowly) or stage directions in parentheses."
    )
    messages.append({"role": "user", "content": user_text})
    return messages


async def run_text_turn(
    session: "EnglishSession",
    user_text: str,
    *,
    send_json: Callable[..., Awaitable[None]],
    play_pcm_frames: Callable[[bytes], Awaitable[None]],
    is_cancelled: Callable[[], bool],
    enable_search: bool = False,
) -> str:
    """流式 LLM + 分句 TTS，经 play_pcm_frames 下发（Opus 由 session 编码）。"""
    t0 = time.time()
    model = rc.get_str("ENGLISH_TEXT_LLM_MODEL") or config.ENGLISH_TEXT_LLM_MODEL or config.LLM_MODEL
    voice = rc.get_str("ENGLISH_TEXT_TTS_VOICE") or config.ENGLISH_TEXT_TTS_VOICE or config.TTS_VOICE
    tts_model = config.ENGLISH_TEXT_TTS_MODEL or config.TTS_MODEL

    use_search = bool(
        enable_search
        and rc.get_bool("ENGLISH_TEXT_ENABLE_SEARCH")
    )
    search_options = None
    if use_search:
        strategy = (
            rc.get_str("ENGLISH_TEXT_SEARCH_STRATEGY")
            or config.ENGLISH_TEXT_SEARCH_STRATEGY
            or "turbo"
        )
        search_options = {"search_strategy": strategy}

    messages = build_chat_messages(session, user_text, enable_search=use_search)
    logger.info(
        "[english][%s] TEXT 轮 LLM model=%s search=%s user=%r",
        session.session_id, model, use_search, user_text[:120],
    )

    await send_json({"type": "tts", "state": "start"})
    session._tts_start_at = time.time()
    if not session._web_pcm_mode:
        await asyncio.sleep(config.ENGLISH_TEXT_TTS_START_LEAD_SEC)

    split_min = max(1, config.ENGLISH_TEXT_TTS_SPLIT_MIN_CHARS)
    splitter = SentenceSplitter(min_chars=split_min)
    full_reply = ""
    llm_queue: asyncio.Queue = asyncio.Queue()
    tts_text_queue: asyncio.Queue = asyncio.Queue()
    llm_error: list = [None]
    llm_first_token_logged = False

    async def _llm_reader():
        nonlocal full_reply, llm_first_token_logged
        session.loop.run_in_executor(
            None,
            _run_llm,
            session.loop,
            messages,
            model,
            llm_queue,
            llm_error,
            use_search,
            search_options,
        )
        try:
            while True:
                delta = await llm_queue.get()
                if delta is None:
                    break
                if is_cancelled():
                    break
                if delta and not llm_first_token_logged:
                    llm_first_token_logged = True
                    logger.info(
                        "[english][%s] TEXT LLM 首 token %.2fs",
                        session.session_id,
                        time.time() - t0,
                    )
                full_reply += delta
                if full_reply.strip():
                    await send_json({
                        "type": "tts",
                        "state": "delta",
                        "text": full_reply,
                    })
                for sentence in splitter.feed(delta):
                    if is_cancelled():
                        break
                    await tts_text_queue.put(sentence)
            if not is_cancelled() and llm_error[0]:
                raise llm_provider.LlmError(llm_error[0])
            if not is_cancelled():
                last = splitter.flush()
                if last:
                    await tts_text_queue.put(last)
        finally:
            await tts_text_queue.put(None)

    async def _tts_player():
        nonlocal full_reply
        synth_task = None
        synth_sentence = ""
        play_count = 0
        display_buf = ""
        while True:
            sentence = await tts_text_queue.get()
            if sentence is None:
                if synth_task and not is_cancelled():
                    pcm = await synth_task
                    play_count = await _play_sentence(
                        session, synth_sentence, pcm, send_json, play_pcm_frames,
                        is_cancelled, t0, play_count,
                        _display_text(display_buf, synth_sentence, full_reply),
                    )
                break
            if is_cancelled():
                continue
            if synth_task is not None:
                pcm = await synth_task
                play_count = await _play_sentence(
                    session, synth_sentence, pcm, send_json, play_pcm_frames,
                    is_cancelled, t0, play_count,
                    _display_text(display_buf, synth_sentence, full_reply),
                )
                display_buf = (
                    f"{display_buf} {synth_sentence}".strip()
                    if display_buf
                    else synth_sentence
                )
                next_synth = asyncio.create_task(
                    _synthesize(session, sentence, tts_model, voice)
                )
                synth_task = next_synth
                synth_sentence = sentence
            else:
                synth_task = asyncio.create_task(
                    _synthesize(session, sentence, tts_model, voice)
                )
                synth_sentence = sentence
                logger.info(
                    "[english][%s] TEXT 首句送 TTS 合成 %.2fs: %s",
                    session.session_id,
                    time.time() - t0,
                    sentence[:80],
                )

    await asyncio.gather(_llm_reader(), _tts_player())
    reply = full_reply.strip()
    logger.info(
        "[english][%s] TEXT 轮完成 %.1fs reply_len=%d",
        session.session_id, time.time() - t0, len(reply),
    )
    return reply


async def run_proactive_greeting_turn(
    session: "EnglishSession",
    *,
    send_json: Callable[..., Awaitable[None]],
    play_pcm_frames: Callable[[bytes], Awaitable[None]],
    is_cancelled: Callable[[], bool],
) -> str:
    """新 Web 会话无历史时，小语主动开口（不落库用户侧假消息）。"""
    return await run_text_turn(
        session,
        PROACTIVE_GREETING_LLM_USER,
        send_json=send_json,
        play_pcm_frames=play_pcm_frames,
        is_cancelled=is_cancelled,
        enable_search=False,
    )


def _run_llm(
    loop,
    messages,
    model,
    queue: asyncio.Queue,
    llm_error: list,
    enable_search: bool = False,
    search_options: dict | None = None,
):
    try:
        for delta in llm_provider.stream_chat(
            messages,
            model=model,
            enable_search=enable_search,
            search_options=search_options,
        ):
            loop.call_soon_threadsafe(queue.put_nowait, delta)
    except llm_provider.LlmError as e:
        llm_error[0] = str(e)
    except Exception as e:  # noqa: BLE001
        llm_error[0] = str(e)
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, None)


def _display_text(display_buf: str, sentence: str, full_reply: str) -> str:
    """页面字幕用累积全文，避免每句覆盖只留最后一句。"""
    played = (
        f"{display_buf} {sentence}".strip()
        if display_buf
        else (sentence or "").strip()
    )
    streamed = (full_reply or "").strip()
    if streamed and len(streamed) >= len(played):
        return streamed
    return played


async def _synthesize(session: "EnglishSession", sentence: str, model: str, voice: str) -> bytes:
    return await session.loop.run_in_executor(
        None, tts_provider.synthesize, sentence, model, voice
    )


async def _play_sentence(
    session: "EnglishSession",
    sentence: str,
    pcm: bytes,
    send_json,
    play_pcm_frames,
    is_cancelled,
    t0: float,
    play_count: int,
    display_text: str = "",
) -> int:
    sentence = (sentence or "").strip()
    if not sentence or is_cancelled() or not pcm:
        return play_count
    ui_text = (display_text or sentence).strip()
    await send_json({"type": "tts", "state": "sentence_start", "text": ui_text})
    await send_json({
        "type": "tts",
        "state": "delta",
        "text": ui_text,
    })
    play_count += 1
    label = "TEXT 首包入下行" if play_count == 1 else "TEXT TTS 播放"
    logger.info(
        "[english][%s] %s %.2fs: %s",
        session.session_id, label, time.time() - t0, sentence[:80],
    )
    frame_bytes = (
        int(config.DOWNLINK_SAMPLE_RATE * config.FRAME_DURATION_MS / 1000)
        * 2
        * config.CHANNELS
    )
    offset = 0
    while offset < len(pcm):
        if is_cancelled():
            break
        chunk = pcm[offset : offset + frame_bytes]
        offset += frame_bytes
        if not chunk:
            break
        await play_pcm_frames(chunk)
    return play_count
