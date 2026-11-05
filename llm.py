"""统一的 LLM 调用入口：路由 + 缓存 + 记账 + 限并发。

所有步骤都走这里，成本才统计得全。

相对教程加固：
1. 独立 load_dotenv() —— 教程依赖 runner.py 顶层隐式加载，单独导入本模块会拿不到 key。
2. resp.usage 可能为 None（部分兼容接口不返回），做了保护，不把成功调用判成崩溃。
"""

import os
import threading
import time

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError

import cache
import pricing
from ledger import CallRecord, log_call, now_iso

load_dotenv()

MODEL_BIG = os.getenv("LLM_MODEL", "deepseek-chat")
# 默认仍是主模型；只有 .env 显式配了 LLM_MODEL_SMALL 才会真的切到便宜模型
MODEL_SMALL = os.getenv("LLM_MODEL_SMALL", "deepseek-chat")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")

MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "3"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "60"))

_client: OpenAI | None = None
_client_lock = threading.Lock()
_semaphore = threading.Semaphore(MAX_CONCURRENCY)


def client() -> OpenAI:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                key = os.getenv("DEEPSEEK_API_KEY")
                if not key:
                    raise RuntimeError("没有配置 DEEPSEEK_API_KEY")
                _client = OpenAI(api_key=key, base_url=BASE_URL,
                                 timeout=REQUEST_TIMEOUT)
    return _client


class Reply:
    def __init__(self, text: str, model: str, prompt_tokens: int,
                 completion_tokens: int, cost: float, cached: bool,
                 latency_ms: int):
        self.text = text
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cost = cost
        self.cached = cached
        self.latency_ms = latency_ms

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __repr__(self) -> str:
        tag = "缓存" if self.cached else "API"
        return f"<Reply {tag} {self.model} {self.total_tokens}tok ¥{self.cost:.6f}>"


def pick_model(tier: str) -> str:
    """tier: 'small' 用便宜模型，'big' 用主模型。"""
    return MODEL_SMALL if tier == "small" else MODEL_BIG


def chat(
    messages: list[dict],
    *,
    step: str = "",
    tier: str = "big",
    temperature: float = 0.0,
    max_tokens: int = 800,
    use_cache: bool = True,
    budget=None,
) -> Reply:
    model = pick_model(tier)
    key = cache.make_key(model, messages, temperature, max_tokens)

    # ① 查缓存：只缓存确定性调用
    can_cache = use_cache and temperature == 0
    if can_cache:
        hit = cache.get(key)
        if hit:
            if budget is not None:
                budget.spend(0)                    # 命中不计 token，但计一次"调用"
            log_call(CallRecord(
                ts=now_iso(), step=step, model=model,
                prompt_tokens=0, completion_tokens=0,
                cost=0.0, cached=True, latency_ms=0,
            ))
            return Reply(hit["text"], model, 0, 0, 0.0, True, 0)

    # ② 调 API（限并发 + 429 退避）
    last_exc = None
    for attempt in range(1, 4):
        with _semaphore:
            t0 = time.perf_counter()
            try:
                resp = client().chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except RateLimitError as exc:
                last_exc = exc
                delay = 2 ** attempt
                print(f"    [429] 限流，{delay}s 后重试")
                time.sleep(delay)
                continue
            latency_ms = int((time.perf_counter() - t0) * 1000)

        text = (resp.choices[0].message.content or "").strip()
        usage = resp.usage
        pt = usage.prompt_tokens if usage else 0
        ct = usage.completion_tokens if usage else 0
        cost = pricing.cost_of(model, pt, ct)

        if budget is not None:
            budget.spend(pt + ct)

        log_call(CallRecord(
            ts=now_iso(), step=step, model=model,
            prompt_tokens=pt, completion_tokens=ct,
            cost=cost, cached=False, latency_ms=latency_ms,
        ))

        if can_cache:
            cache.put(key, {"text": text})

        return Reply(text, model, pt, ct, cost, False, latency_ms)

    raise RuntimeError(f"限流重试 3 次仍失败：{last_exc}")
