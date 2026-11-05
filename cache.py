"""结果缓存：同输入不重复付费。

键 = sha256(model + messages + temperature + max_tokens)
只缓存 temperature == 0 的调用（否则等于抹掉随机性）。

相对教程加固：sqlite3.connect 加 timeout=10，避免并发流水线同时
读写缓存时偶发 "database is locked"。
"""

import hashlib
import json
import sqlite3
import time
from pathlib import Path

DB_FILE = Path("cache.db")
DEFAULT_TTL = 3600          # 秒


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache ("
        " key TEXT PRIMARY KEY, value TEXT NOT NULL, ts REAL NOT NULL)"
    )
    conn.commit()
    return conn


def make_key(model: str, messages: list[dict], temperature: float,
             max_tokens: int) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get(key: str, ttl: int = DEFAULT_TTL) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value, ts FROM cache WHERE key = ?", (key,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return None

    value, ts = row
    if time.time() - ts > ttl:
        return None

    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def put(key: str, value: dict) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, ts) VALUES (?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def size() -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
    finally:
        conn.close()
