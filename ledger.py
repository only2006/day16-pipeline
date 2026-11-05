"""调用台账：每次 LLM 调用追加一行 JSONL。

为什么用 JSONL 而不是数据库：
    追加写、崩溃不丢、每行独立好排查、文本工具直接能看。

相对教程加固：并发跑流水线时多线程会同时写同一个文件，
用模块级锁保证每行完整写入，不会出现半行 JSON。
"""

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "calls.jsonl"
_write_lock = threading.Lock()


@dataclass
class CallRecord:
    ts: str
    step: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost: float
    cached: bool
    latency_ms: int


def log_call(rec: CallRecord) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(rec), ensure_ascii=False) + "\n"
    with _write_lock:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line)


def read_calls() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    out: list[dict] = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue                      # 坏行跳过，不阻断统计
    return out


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
