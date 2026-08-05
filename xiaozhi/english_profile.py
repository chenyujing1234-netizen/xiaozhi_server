"""英语口语用户画像：以自然语言段落为主。

按 device_id 存 MySQL；开会话时注入 Omni instructions。
显式偏好可立即重写画像；平时每 N 轮对话后用 LLM 沉淀更新。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, fields
from http import HTTPStatus
from typing import Any, Optional

import pymysql
from dashscope import Generation
from pymysql.cursors import DictCursor

from config import config

logger = logging.getLogger("english.profile")

DEFAULT_PROFILE_TEXT = (
    "这位使用者大约十一岁，中文母语，英语开口少、可能不爱说话或习惯说中文。"
    "把互动定位成「轻松聊天伙伴小语」，不是老师、不是口语课、不是作业辅导。"
    "你要主动带节奏：几乎每轮结尾抛一个超好答的问题（是/否、二选一、一个词即可）。"
    "孩子用中文答完全可以；先接话、再夹一点简单英文，不要逼完整英文句。"
    "若对方嗯/不知道/冷场：立刻给两三个有趣选项（游戏/吃的/宠物/周末干了啥）。"
    "可以偶尔用游戏口吻「考考你」出超短选择题，但别说测验、别说学习。"
    "语气平等、略酷、像大一点的朋友；可聊游戏、动漫、运动、宠物、搞笑；"
    "不要幼儿化，不要叫「小朋友」「同学」。"
    "前至少五轮以接话、好奇、开玩笑为主，几乎不纠错、不布置任务。"
    "之后也只在明显影响理解，或对方主动要求纠音/纠错时，才轻轻纠正一点。"
    "默认回复短、口语化，适合语音来回。"
)

# 用户“在提偏好/要求”的信号：命中后立刻用 LLM 重写画像段落
_PREFERENCE_SIGNAL_PATTERNS = [
    r"听不懂",
    r"太难了",
    r"用中文",
    r"说中文",
    r"中文解释",
    r"只要英语",
    r"别说中文",
    r"不要中文",
    r"全英语",
    r"纯英语",
    r"english only",
    r"no chinese",
    r"短(一点|一些)?(回答|回复|说)?",
    r"简短",
    r"说短点",
    r"别说太长",
    r"详细",
    r"讲完整",
    r"多说一点",
    r"讲长一点",
    r"讲(一个|个)?故事",
    r"tell (me )?(a )?story",
    r"keep it short",
    r"be brief",
    r"more detail",
    r"in detail",
    # 纠错强度 / 方式
    r"严格(一点|一些)?(纠正|纠错)?",
    r"狠(一点|一些)?纠正",
    r"多纠正",
    r"认真纠正",
    r"每个错都要",
    r"温和(一点|一些)?(纠正|纠错)?",
    r"轻(一点|一些)?纠正",
    r"少纠正",
    r"别老纠正",
    r"不要总纠正",
    r"别纠正了",
    r"先别纠正",
    r"不要纠正",
    r"纠正我的发音",
    r"纠正语法",
    r"帮我纠错",
    r"correct my",
    r"be (more )?strict",
    r"don't correct",
    r"less correction",
    r"我希望",
    r"请你以后",
    r"以后请",
    r"能不能",
    r"可不可以",
]


@dataclass
class EnglishProfile:
    profile_text: str = DEFAULT_PROFILE_TEXT
    turn_count: int = 0
    last_refine_turn: int = 0
    updated_at: float = 0.0
    # 兼容旧字段（不再驱动提示词，仅迁移/读写保留）
    listening_level: str = "beginner"
    reply_policy: str = "en_then_zh"
    correction_level: str = "medium"
    reply_length: str = "short"
    notes: str = ""
    confidence: float = 0.4
    explicit_locked: bool = False

    def normalized(self) -> "EnglishProfile":
        text = (self.profile_text or "").strip()
        if not text:
            text = DEFAULT_PROFILE_TEXT
        self.profile_text = text[:1200]
        self.turn_count = max(0, int(self.turn_count or 0))
        self.last_refine_turn = max(0, int(self.last_refine_turn or 0))
        self.notes = (self.notes or "")[:300]
        self.confidence = max(0.0, min(1.0, float(self.confidence or 0.0)))
        return self


def _synthesize_from_legacy(row: dict) -> str:
    """旧枚举画像 → 一段初始自然语言（仅在 profile_text 为空时用）。"""
    level = row.get("listening_level") or "beginner"
    policy = row.get("reply_policy") or "en_then_zh"
    length = row.get("reply_length") or "short"
    notes = (row.get("notes") or "").strip()
    level_map = {
        "beginner": "英语听力偏弱，词汇请尽量简单",
        "intermediate": "英语听力中等，可用日常表达",
        "advanced": "英语听力较好，可用较自然的英语",
    }
    policy_map = {
        "en_only": "请只用英语回复，不要使用中文",
        "en_then_zh": "请先说简短英文，再用中文解释要点",
        "zh_first": "请先用中文说明，再给一句简短英文示例",
    }
    length_map = {
        "short": "默认回复要很短，适合口语来回",
        "medium": "回复长度适中即可",
        "long": "允许较长、较完整的回答（如讲故事）",
    }
    corr = row.get("correction_level") or "medium"
    corr_map = {
        "light": "纠错要少而轻，只纠正严重影响理解的问题",
        "medium": "纠错要温和，用中文说明后给出正确英文并再读一遍，每轮一到两个要点",
        "heavy": "纠错可以更严格细致，但仍保持鼓励语气，用中文说明后给出正确英文并再读一遍",
    }
    parts = [
        "这位学习者是中文母语者。",
        level_map.get(level, level_map["beginner"]) + "。",
        policy_map.get(policy, policy_map["en_then_zh"]) + "。",
        length_map.get(length, length_map["short"]) + "。",
        corr_map.get(corr, corr_map["medium"]) + "。",
    ]
    if notes:
        parts.append(f"额外偏好：{notes}")
    return "".join(parts)


def _profile_from_row(row: dict) -> EnglishProfile:
    text = (row.get("profile_text") or "").strip()
    if not text:
        text = _synthesize_from_legacy(row)
    return EnglishProfile(
        profile_text=text,
        turn_count=int(row.get("turn_count") or 0),
        last_refine_turn=int(row.get("last_refine_turn") or 0),
        updated_at=float(row.get("updated_at") or 0.0),
        listening_level=row.get("listening_level") or "beginner",
        reply_policy=row.get("reply_policy") or "en_then_zh",
        correction_level=row.get("correction_level") or "medium",
        reply_length=row.get("reply_length") or "short",
        notes=row.get("notes") or "",
        confidence=float(row.get("confidence") or 0.0),
        explicit_locked=bool(row.get("explicit_locked")),
    ).normalized()


class ProfileStore:
    """MySQL 持久化，按 device_id 读写。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self):
        return pymysql.connect(
            host=config.MYSQL_HOST,
            port=config.MYSQL_PORT,
            user=config.MYSQL_USER,
            password=config.MYSQL_PASSWORD,
            database=config.MYSQL_DATABASE,
            charset=config.MYSQL_CHARSET,
            cursorclass=DictCursor,
            autocommit=False,
            connect_timeout=5,
            read_timeout=10,
            write_timeout=10,
        )

    def _ensure_column(self, cur, name: str, ddl: str) -> None:
        cur.execute(f"SHOW COLUMNS FROM english_profiles LIKE %s", (name,))
        if cur.fetchone():
            return
        cur.execute(ddl)
        logger.info("[english.profile] 已增加列 %s", name)

    def _init_db(self):
        with self._lock:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS english_profiles (
                            device_id VARCHAR(128) NOT NULL PRIMARY KEY,
                            profile_text TEXT NOT NULL,
                            listening_level VARCHAR(32) NOT NULL DEFAULT 'beginner',
                            reply_policy VARCHAR(32) NOT NULL DEFAULT 'en_then_zh',
                            correction_level VARCHAR(32) NOT NULL DEFAULT 'medium',
                            reply_length VARCHAR(32) NOT NULL DEFAULT 'short',
                            notes VARCHAR(512) NOT NULL DEFAULT '',
                            confidence DOUBLE NOT NULL DEFAULT 0.4,
                            explicit_locked TINYINT(1) NOT NULL DEFAULT 0,
                            updated_at DOUBLE NOT NULL DEFAULT 0,
                            turn_count INT NOT NULL DEFAULT 0,
                            last_refine_turn INT NOT NULL DEFAULT 0,
                            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            INDEX idx_english_profiles_updated_at (updated_at)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                        """
                    )
                    self._ensure_column(
                        cur,
                        "profile_text",
                        "ALTER TABLE english_profiles "
                        "ADD COLUMN profile_text TEXT NULL AFTER device_id",
                    )
                    self._ensure_column(
                        cur,
                        "last_refine_turn",
                        "ALTER TABLE english_profiles "
                        "ADD COLUMN last_refine_turn INT NOT NULL DEFAULT 0 "
                        "AFTER turn_count",
                    )
                    self._ensure_column(
                        cur,
                        "reply_length",
                        "ALTER TABLE english_profiles "
                        "ADD COLUMN reply_length VARCHAR(32) NOT NULL DEFAULT 'short' "
                        "AFTER correction_level",
                    )
                    # 旧行：空 profile_text 用默认值填上，便于直接查看
                    cur.execute(
                        """
                        UPDATE english_profiles
                        SET profile_text = %s
                        WHERE profile_text IS NULL OR TRIM(profile_text) = ''
                        """,
                        (DEFAULT_PROFILE_TEXT,),
                    )
                conn.commit()
                logger.info(
                    "[english.profile] MySQL 就绪 %s:%s/%s（自然语言画像）",
                    config.MYSQL_HOST,
                    config.MYSQL_PORT,
                    config.MYSQL_DATABASE,
                )
            finally:
                conn.close()

    def get(self, device_id: str) -> EnglishProfile:
        key = (device_id or "unknown").strip() or "unknown"
        with self._lock:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT * FROM english_profiles WHERE device_id = %s",
                        (key,),
                    )
                    row = cur.fetchone()
            finally:
                conn.close()
        if row is None:
            return EnglishProfile().normalized()
        return _profile_from_row(row)

    def save(self, device_id: str, profile: EnglishProfile) -> EnglishProfile:
        key = (device_id or "unknown").strip() or "unknown"
        p = profile.normalized()
        p.updated_at = time.time()
        with self._lock:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO english_profiles (
                            device_id, profile_text, listening_level, reply_policy,
                            correction_level, reply_length, notes, confidence,
                            explicit_locked, updated_at, turn_count, last_refine_turn
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            profile_text=VALUES(profile_text),
                            listening_level=VALUES(listening_level),
                            reply_policy=VALUES(reply_policy),
                            correction_level=VALUES(correction_level),
                            reply_length=VALUES(reply_length),
                            notes=VALUES(notes),
                            confidence=VALUES(confidence),
                            explicit_locked=VALUES(explicit_locked),
                            updated_at=VALUES(updated_at),
                            turn_count=VALUES(turn_count),
                            last_refine_turn=VALUES(last_refine_turn)
                        """,
                        (
                            key,
                            p.profile_text,
                            p.listening_level,
                            p.reply_policy,
                            p.correction_level,
                            p.reply_length,
                            p.notes,
                            p.confidence,
                            1 if p.explicit_locked else 0,
                            p.updated_at,
                            p.turn_count,
                            p.last_refine_turn,
                        ),
                    )
                conn.commit()
            finally:
                conn.close()
        return p

    def list_all(self, limit: int = 100) -> list[tuple[str, EnglishProfile]]:
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT * FROM english_profiles
                        ORDER BY updated_at DESC
                        LIMIT %s
                        """,
                        (limit,),
                    )
                    rows = cur.fetchall() or []
            finally:
                conn.close()
        return [(row["device_id"], _profile_from_row(row)) for row in rows]

    def patch(self, device_id: str, **updates: Any) -> EnglishProfile:
        profile = self.get(device_id)
        allowed = {f.name for f in fields(EnglishProfile)}
        for k, v in updates.items():
            if k in allowed and v is not None:
                setattr(profile, k, v)
        return self.save(device_id, profile)


_store: Optional[ProfileStore] = None
_store_lock = threading.Lock()
_refine_lock = threading.Lock()
_last_refine_at: dict[str, float] = {}


def get_store() -> ProfileStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = ProfileStore()
        return _store


_CORRECTION_PROTOCOL = (
    "=== Correction protocol (relaxed mode — profile may override) ===\n"
    "Default: conversation first, correction later. Skip correction in the first 5 turns "
    "unless the student explicitly asks or meaning is completely blocked.\n"
    "When correcting (only after rapport or on request):\n"
    "Step A: react to what they said like a friend (not a grade).\n"
    "Step B: one short Chinese note OR simple English rephrase — pick whichever feels lighter.\n"
    "Step C: one clear model sentence, read once; invite repeat only if they seem willing.\n"
    "Fix at most ONE point per turn. Never stack grammar lectures. "
    "If there is no clear issue, keep chatting.\n"
    "=== Spelling requests (怎么拼 / how do you spell) ===\n"
    "When spelling a word letter by letter for the student, use commas or spaces "
    "between letters (e.g. \"W, I, N, D, O, W, S\" or \"w i n d o w s\"), "
    "then say the full word once clearly. "
    "Never use hyphens between letters: do not write or speak forms like w-i-n-d-o-w-s."
)

_RELAXED_OPENING = (
    "=== Opening a new conversation (no recent history) ===\n"
    "YOU speak first. Sound like a curious friend, NOT a teacher.\n"
    "Shape: 1 short greeting + 1 very easy question they can answer in Chinese "
    "or one English word. Give choices when helpful (A or B?).\n"
    "Good: \"Hey! I'm Xiaoyu. Quick one — pizza or noodles?\" "
    "\"What's something fun you did today? Games count!\"\n"
    "Bad: practice English, let's study, I'm your tutor, how was school (parent tone).\n"
    "Never mention correction, learning goals, or homework in the opening."
)

_PROACTIVE_DIALOGUE = (
    "=== Proactive dialogue — keep the ball moving ===\n"
    "Target: shy kids or kids who don't know what to say in English.\n"
    "1) End almost EVERY turn with ONE clear, easy question.\n"
    "2) Accept Chinese answers; mirror briefly in simple English, then ask again.\n"
    "3) Cold start / 不知道 / um / very short reply: immediately offer 2-3 fun options.\n"
    "4) Light daily chat OK: lunch, a game, a pet, something funny — not interrogation.\n"
    "5) Playful \"quick challenge\" OK as a game (\"Cat or dog? Guess my favorite!\") "
    "— do NOT call it quiz, test, or practice unless they asked.\n"
    "6) React first (nice / cool / haha), then maybe one useful English phrase, "
    "then the next question — never lecture.\n"
    "7) First ~8 turns: you lead topics; do not wait for them to invent conversation."
)

_TEACHING_STRATEGY = (
    "=== Teaching strategy (only when they ask or the moment fits naturally) ===\n"
    "When the student asks a question (English learning, vocabulary, grammar, usage, "
    "or general knowledge):\n"
    "1) Direct answer vs guided discovery — choose per turn:\n"
    "   - Give a clear direct answer when: they explicitly ask for the answer; the "
    "topic is new to them; they are clearly stuck after trying; or a quick factual "
    "reply is obviously more helpful.\n"
    "   - Prefer hints and guided thinking when: the answer is within their current "
    "level (see student profile and recent conversation); they already know related "
    "words or patterns; or a small nudge would help them remember or produce English "
    "themselves. Offer one short hint or guiding question first; wait for their try "
    "if the conversation flow allows. If they are close, praise and nudge; if still "
    "stuck after one hint, give the answer kindly.\n"
    "2) Follow the student profile for pace, language mix, and depth.\n"
    "3) Mini oral prompts — use as playful games when flow is good OR they ask; "
    "never as homework. At most ONE per few turns, then back to chat.\n"
    "4) Stay warm and peer-like. Never withhold answers to frustrate."
)

# 导师字幕里常见的「逐字母拼读」连字符（不影响 twenty-one 等整词连字符）
_SPELLING_HYPHEN_TOKEN = re.compile(r"\b(?:[A-Za-z]-)+[A-Za-z]\b")


def normalize_spelling_hyphens(text: str) -> str:
    """w-i-n-d-o-w-s → w i n d o w s（仅单字母用 - 串联的片段）。"""

    def _repl(match: re.Match) -> str:
        token = match.group(0)
        parts = token.split("-")
        if all(len(p) == 1 and p.isalpha() for p in parts):
            return " ".join(parts)
        return token

    return _SPELLING_HYPHEN_TOKEN.sub(_repl, text or "")


def build_instructions(
    profile: Optional[EnglishProfile] = None,
    *,
    history_context: str = "",
) -> str:
    """基础导师角色 + 纠错协议 + 自然语言画像 + 可选历史上下文。"""
    p = (profile or EnglishProfile()).normalized()
    parts = [
        config.ENGLISH_OMNI_INSTRUCTIONS.strip(),
        "",
        _RELAXED_OPENING,
        "",
        _PROACTIVE_DIALOGUE,
        "",
        _CORRECTION_PROTOCOL,
        "",
        _TEACHING_STRATEGY,
        "",
        "=== Student profile (follow closely; this is the adaptation source of truth) ===",
        p.profile_text,
        "",
        "Keep replies natural for voice. No markdown, bullets, or emoji.",
    ]
    hist = (history_context or "").strip()
    if hist:
        parts.extend(
            [
                "",
                "=== Recent conversation history (for continuity only) ===",
                "Use this to continue the tutoring naturally. "
                "Do not read the history aloud unless the student asks. "
                "Do not restart introductions if you already greeted before.",
                hist,
            ]
        )
    return "\n".join(parts)


def looks_like_preference_request(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    return any(
        re.search(pat, text, flags=re.IGNORECASE)
        for pat in _PREFERENCE_SIGNAL_PATTERNS
    )


def _parse_llm_json(raw: str) -> Optional[dict]:
    text = (raw or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _recent_dialogue_snippet(device_id: str, max_messages: int = 12) -> str:
    try:
        from .english_history import format_history_context, get_history_store

        msgs = get_history_store().get_recent(device_id, max_messages=max_messages)
        return format_history_context(msgs, max_chars=1800)
    except Exception as e:  # noqa: BLE001
        logger.debug("[english.profile] 读取历史失败: %s", e)
        return ""


def _llm_refine_profile_text(
    profile: EnglishProfile,
    *,
    dialogue: str,
    user_text: str,
    tutor_text: str,
    reason: str,
) -> Optional[str]:
    system = (
        "你在维护一位中文学习者的英语口语陪练「用户画像」。"
        "画像必须是一段连贯的中文说明（可夹少量英文术语），需覆盖："
        "1) 听力/水平；2) 回复语言（全英/中英/先中后英）；"
        "3) 回复长短；4) 纠错强度与方式（严格/温和/少纠；是否用中文纠错；"
        "是否要求正确英文再读一遍）；5) 兴趣话题与其他稳定偏好；"
        "6) 掌握程度与教学节奏（何时直接给答案、何时用提示引导其自己思考；"
        "是否适合出题、测验难度偏好）。"
        "若用户明确要求严格纠正、少纠正、别纠正、只纠发音/语法等，必须写进画像。"
        "不要写成选项枚举，不要用 bullet。控制在 80-240 个汉字。"
        "保留仍然成立的旧偏好，吸收新证据；证据不足时保持不变。"
        "只返回 JSON："
        '{"change": true/false, "profile_text": "...", "reason": "简短原因"}'
    )
    user = (
        f"更新原因: {reason}\n\n"
        f"当前画像:\n{profile.profile_text}\n\n"
        f"最近对话:\n{dialogue or '(无)'}\n\n"
        f"本轮用户: {user_text}\n"
        f"本轮导师: {tutor_text}\n"
    )
    try:
        resp = Generation.call(
            model=config.ENGLISH_PROFILE_LLM_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            result_format="message",
            temperature=0.3,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[english.profile] 画像沉淀 LLM 调用失败: %s", e)
        return None

    if resp.status_code != HTTPStatus.OK:
        logger.warning(
            "[english.profile] 画像沉淀 LLM 错误 code=%s msg=%s",
            getattr(resp, "code", "?"),
            getattr(resp, "message", "?"),
        )
        return None
    try:
        content = resp.output.choices[0].message.content
    except Exception:  # noqa: BLE001
        return None
    data = _parse_llm_json(content)
    if not data:
        return None
    if not data.get("change", True):
        return None
    text = (data.get("profile_text") or "").strip()
    if not text:
        return None
    return text[:1200]


def refine_profile(
    device_id: str,
    *,
    user_text: str = "",
    tutor_text: str = "",
    reason: str = "periodic",
    force: bool = False,
) -> Optional[EnglishProfile]:
    """用 LLM 重写自然语言画像段落。"""
    if not config.ENGLISH_PROFILE_AUTO_UPDATE and not force:
        return None

    key = (device_id or "unknown").strip() or "unknown"
    now = time.time()
    with _refine_lock:
        last = _last_refine_at.get(key, 0.0)
        if (
            not force
            and (now - last) < config.ENGLISH_PROFILE_UPDATE_COOLDOWN_SEC
        ):
            return None
        _last_refine_at[key] = now

    profile = get_store().get(device_id)
    dialogue = _recent_dialogue_snippet(device_id)
    if user_text or tutor_text:
        extra = []
        if user_text:
            extra.append(f"Student: {user_text}")
        if tutor_text:
            extra.append(f"Tutor: {tutor_text}")
        dialogue = (dialogue + "\n" + "\n".join(extra)).strip()

    new_text = _llm_refine_profile_text(
        profile,
        dialogue=dialogue,
        user_text=user_text,
        tutor_text=tutor_text,
        reason=reason,
    )
    if not new_text:
        return None
    if new_text.strip() == profile.profile_text.strip():
        profile.last_refine_turn = profile.turn_count
        return get_store().save(device_id, profile)

    profile.profile_text = new_text
    profile.last_refine_turn = profile.turn_count
    profile.confidence = min(1.0, max(profile.confidence, 0.55) + 0.05)
    saved = get_store().save(device_id, profile)
    logger.info(
        "[english.profile] 画像已沉淀 device=%s reason=%s text=%s",
        device_id,
        reason,
        saved.profile_text[:160],
    )
    return saved


def apply_explicit_preference(device_id: str, user_text: str) -> Optional[EnglishProfile]:
    """用户明确提偏好时：立刻用 LLM 把要求写进画像段落。"""
    if not looks_like_preference_request(user_text):
        return None
    profile = get_store().get(device_id)
    profile.turn_count = max(profile.turn_count, 0)
    get_store().save(device_id, profile)
    updated = refine_profile(
        device_id,
        user_text=user_text,
        tutor_text="",
        reason="explicit_user_request",
        force=True,
    )
    return updated


def maybe_update_from_turn(
    device_id: str,
    user_text: str,
    tutor_text: str,
    *,
    force: bool = False,
) -> Optional[EnglishProfile]:
    """每轮结束后调用：累加轮次；每隔 N 轮沉淀一次画像。"""
    user_text = (user_text or "").strip()
    tutor_text = (tutor_text or "").strip()
    if not user_text:
        return None

    profile = get_store().get(device_id)
    profile.turn_count += 1
    get_store().save(device_id, profile)

    # 显式偏好：本轮立刻沉淀
    if looks_like_preference_request(user_text) or force:
        updated = refine_profile(
            device_id,
            user_text=user_text,
            tutor_text=tutor_text,
            reason="explicit_or_forced",
            force=True,
        )
        return updated or get_store().get(device_id)

    if not config.ENGLISH_PROFILE_AUTO_UPDATE:
        return get_store().get(device_id)

    every = max(1, int(config.ENGLISH_PROFILE_REFINE_EVERY_TURNS))
    turns_since = profile.turn_count - profile.last_refine_turn
    if turns_since < every:
        return get_store().get(device_id)

    updated = refine_profile(
        device_id,
        user_text=user_text,
        tutor_text=tutor_text,
        reason=f"every_{every}_turns",
        force=False,
    )
    return updated or get_store().get(device_id)


# 兼容旧调用名（若别处仍引用）
def match_explicit_preference(user_text: str) -> Optional[dict]:
    if looks_like_preference_request(user_text):
        return {"profile_text_update": True}
    return None
