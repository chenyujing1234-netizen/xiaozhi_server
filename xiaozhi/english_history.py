"""英语口语对话历史：按 device_id 存 MySQL，重连时注入上下文。"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

import pymysql
from pymysql.cursors import DictCursor

from config import config

logger = logging.getLogger("english.history")


@dataclass
class ChatMessage:
    role: str  # user | assistant
    content: str
    session_id: str = ""
    created_at: float = 0.0


class HistoryStore:
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

    def _init_db(self):
        with self._lock:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS english_chat_messages (
                            id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                            device_id VARCHAR(128) NOT NULL,
                            role VARCHAR(16) NOT NULL,
                            content TEXT NOT NULL,
                            session_id VARCHAR(64) NOT NULL DEFAULT '',
                            created_at DOUBLE NOT NULL,
                            created_ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            INDEX idx_english_chat_device_created (device_id, created_at),
                            INDEX idx_english_chat_device_id (device_id, id)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                        """
                    )
                conn.commit()
                logger.info("[english.history] MySQL 表 english_chat_messages 就绪")
            finally:
                conn.close()

    def append(
        self,
        device_id: str,
        role: str,
        content: str,
        *,
        session_id: str = "",
    ) -> None:
        key = (device_id or "unknown").strip() or "unknown"
        text = (content or "").strip()
        if not text:
            return
        if role not in ("user", "assistant"):
            return
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO english_chat_messages
                            (device_id, role, content, session_id, created_at)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (key, role, text[:4000], session_id or "", now),
                    )
                conn.commit()
            finally:
                conn.close()

    def append_turn(
        self,
        device_id: str,
        user_text: str,
        assistant_text: str,
        *,
        session_id: str = "",
    ) -> None:
        """保存一轮完整对话（用户 + 导师）。"""
        if (user_text or "").strip():
            self.append(device_id, "user", user_text, session_id=session_id)
        if (assistant_text or "").strip():
            self.append(
                device_id, "assistant", assistant_text, session_id=session_id
            )

    def get_recent(
        self,
        device_id: str,
        *,
        max_messages: Optional[int] = None,
    ) -> list[ChatMessage]:
        """按时间正序返回最近 N 条消息。"""
        key = (device_id or "unknown").strip() or "unknown"
        limit = max_messages or config.ENGLISH_HISTORY_MAX_MESSAGES
        limit = max(1, min(int(limit), 200))
        with self._lock:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT role, content, session_id, created_at
                        FROM (
                            SELECT role, content, session_id, created_at, id
                            FROM english_chat_messages
                            WHERE device_id = %s
                            ORDER BY id DESC
                            LIMIT %s
                        ) t
                        ORDER BY id ASC
                        """,
                        (key, limit),
                    )
                    rows = cur.fetchall() or []
            finally:
                conn.close()
        return [
            ChatMessage(
                role=r["role"],
                content=r["content"] or "",
                session_id=r["session_id"] or "",
                created_at=float(r["created_at"] or 0.0),
            )
            for r in rows
            if (r.get("content") or "").strip()
        ]


_store: Optional[HistoryStore] = None
_store_lock = threading.Lock()


def get_history_store() -> HistoryStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = HistoryStore()
        return _store


def format_history_context(
    messages: list[ChatMessage],
    *,
    max_chars: Optional[int] = None,
) -> str:
    """把历史格式化为可注入 instructions 的上下文文本。"""
    if not messages:
        return ""
    budget = max_chars or config.ENGLISH_HISTORY_MAX_CHARS
    lines: list[str] = []
    for m in messages:
        label = "Student" if m.role == "user" else "Tutor"
        content = (m.content or "").strip().replace("\n", " ")
        if not content:
            continue
        lines.append(f"{label}: {content}")
    if not lines:
        return ""
    # 从尾部往前截，保留最近内容
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        add = len(line) + 1
        if kept and total + add > budget:
            break
        kept.append(line)
        total += add
    kept.reverse()
    return "\n".join(kept)
