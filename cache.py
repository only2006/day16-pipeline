"""结果缓存：同输入不重复付费。

键 = sha256(model + base_url + messages + temperature + max_tokens)
只缓存 temperature == 0 的调用（否则等于抹掉随机性）。

加固：
- sqlite3.connect 加 timeout=10 + WAL，避免并发读写 "database is locked"
- key_lock(key) 提供 per-key 锁，配合 llm.chat 做单飞，防并发缓存击穿
- base_url 纳入缓存键，换端点/账号不会命中旧端点的旧答案
"""

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

DB_FILE = Path("cache.db")
DEFAULT_TTL = 3600          # 秒

_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()


def key_lock(key: str) -> threading.Lock:
    """返回该 key 专属的锁；同 key 的「查缓存→调 API→回填」会串行。"""
    with _key_locks_guard:
        return _key_locks.setdefault(key, threading.Lock())


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    try:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass                   # 已是 WAL 或临时锁，不影响后续读写
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cache ("
            " key TEXT PRIMARY KEY, value TEXT NOT NULL, ts REAL NOT NULL)"
        )
        conn.commit()
    except BaseException:
        # 建表失败（如 cache.db 损坏）时若不关连接，Windows 上文件句柄会一直
        # 被占用，连临时目录都删不掉
        conn.close()
        raise
    return conn


def make_key(model: str, messages: list[dict], temperature: float,
             max_tokens: int, base_url: str = "") -> str:
    payload = {
        "model": model,
        "base_url": base_url,
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
