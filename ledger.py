"""调用台账：每次 LLM 调用追加一行 JSONL。

为什么用 JSONL 而不是数据库：
    追加写、崩溃不丢、每行独立好排查、文本工具直接能看。

加固：
- 并发写用模块级锁，保证每行完整，不会出现半行 JSON
- read_calls() 逐行读（台账可无限增长），并校验必需字段，
  坏行 / 旧格式行直接跳过，避免 stats.py 读到这里时 KeyError
"""

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "calls.jsonl"
_write_lock = threading.Lock()

REQUIRED_FIELDS = (
    "ts", "step", "model", "prompt_tokens", "completion_tokens",
    "cost", "cached", "latency_ms",
)


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
    with LOG_FILE.open("r", encoding="utf-8") as f:
        for line in f:                    # 逐行，不把整个台账读进内存
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or not all(k in obj for k in REQUIRED_FIELDS):
                continue              # 半行 / 缺字段的旧格式行，跳过
            out.append(obj)
    return out


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
