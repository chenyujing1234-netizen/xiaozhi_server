"""SpeakPal 用户表（手机号 / 微信 openid），同步 PyMySQL。"""
from __future__ import annotations

import logging
import threading
from typing import Optional

import pymysql
from pymysql.cursors import DictCursor

from config import config

logger = logging.getLogger("auth.user")

_lock = threading.Lock()
_ready = False


def _connect():
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


def init_auth_tables() -> None:
    global _ready
    with _lock:
        if _ready:
            return
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id            INT AUTO_INCREMENT PRIMARY KEY,
                        phone         VARCHAR(20)  NULL UNIQUE,
                        openid        VARCHAR(64)  NULL UNIQUE,
                        created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_login_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                                  ON UPDATE CURRENT_TIMESTAMP,
                        login_count   INT          NOT NULL DEFAULT 0,
                        INDEX idx_phone (phone),
                        INDEX idx_openid (openid)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS login_logs (
                        id          INT AUTO_INCREMENT PRIMARY KEY,
                        user_id     INT          NOT NULL,
                        phone       VARCHAR(20)  NULL,
                        openid      VARCHAR(64)  NULL,
                        login_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        ip          VARCHAR(64)  DEFAULT NULL,
                        user_agent  VARCHAR(512) DEFAULT NULL,
                        login_type  VARCHAR(20)  NOT NULL DEFAULT 'phone',
                        INDEX idx_user_id (user_id),
                        INDEX idx_login_at (login_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
            conn.commit()
            _ready = True
            logger.info("[auth] users / login_logs 表就绪")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def upsert_user_by_phone(phone: str) -> int:
    init_auth_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (phone, login_count)
                VALUES (%s, 1)
                ON DUPLICATE KEY UPDATE
                    last_login_at = NOW(),
                    login_count   = login_count + 1
                """,
                (phone,),
            )
            cur.execute("SELECT id FROM users WHERE phone = %s", (phone,))
            row = cur.fetchone()
        conn.commit()
        return int(row["id"])
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_user_by_openid(openid: str) -> int:
    init_auth_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (openid, login_count)
                VALUES (%s, 1)
                ON DUPLICATE KEY UPDATE
                    last_login_at = NOW(),
                    login_count   = login_count + 1
                """,
                (openid,),
            )
            cur.execute("SELECT id FROM users WHERE openid = %s", (openid,))
            row = cur.fetchone()
        conn.commit()
        return int(row["id"])
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_login(
    user_id: int,
    ip: str,
    user_agent: str,
    *,
    login_type: str = "phone",
    phone: str = "",
    openid: str = "",
) -> None:
    init_auth_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO login_logs (user_id, phone, openid, ip, user_agent, login_type)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    phone or None,
                    openid or None,
                    ip,
                    (user_agent or "")[:512],
                    login_type,
                ),
            )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        logger.warning("[auth] 写登录日志失败: %s", e)
    finally:
        conn.close()


def get_user(user_id: int) -> Optional[dict]:
    init_auth_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, phone, openid FROM users WHERE id = %s",
                (user_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def device_id_for_user(user_id: int) -> str:
    """登录后用于英语画像/历史的稳定 device_id。"""
    return f"user-{int(user_id)}"
