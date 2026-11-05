"""统一的 LLM 调用入口：路由 + 缓存 + 记账 + 限并发。

所有步骤都走这里，成本才统计得全。

相对教程 / Day17 首版修复的护栏缺陷：
1. 429 退避的 time.sleep 原先在 `with _semaphore` 内，抱着许可睡 2/4/8s，
   其余线程全被堵住；现在 sleep 移到信号量外。
2. OpenAI SDK 默认 max_retries=2，一次 create 可能发 3 个真实请求而 Budget
   只记 1 次；现在统一设 max_retries=0，重试由本模块明确管理。
3. 只捕获 RateLimitError，超时/连接失败/5xx 会绕过分类；现在复用
   infra.classify() 统一判定可重试性。
4. 缓存键不含 base_url，换中转端点会命中旧端点旧答案；现在纳入键。
5. cache.get/put 在并发下会击穿（同 key 多线程同时 miss → 重复付费）；
   现在用 per-key 锁做单飞 + double-check。
6. 独立 load_dotenv()、resp.usage 为 None 保护（Day17 首版已有）。
"""

import os
import threading
import time
from contextlib import nullcontext

from dotenv import load_dotenv
from openai import OpenAI

import cache
import pricing
from infra import RetryableError, classify
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
                                 timeout=REQUEST_TIMEOUT, max_retries=0)
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


def _request(model: str, messages: list[dict], temperature: float,
             max_tokens: int, step: str, budget) -> Reply:
    """真正打 API：限并发 + 可重试异常退避 + 记账。不含缓存读写。"""
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            with _semaphore:
                t0 = time.perf_counter()
                resp = client().chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            latency_ms = int((time.perf_counter() - t0) * 1000)
        except Exception as exc:  # noqa: BLE001 - 统一交给 classify 判定
            err = classify(exc)
            if not isinstance(err, RetryableError):
                raise err from exc
            last_exc = err
            if attempt == 3:
                break
            delay = 2 ** attempt
            print(f"    [重试] {type(exc).__name__}，{delay}s 后重试")
            # 关键：退避在信号量外，不抱着许可睡觉
            time.sleep(delay)
            continue

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
        return Reply(text, model, pt, ct, cost, False, latency_ms)

    raise RuntimeError(f"重试 3 次仍失败：{last_exc}")


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
    key = cache.make_key(model, messages, temperature, max_tokens, BASE_URL)

    # 只缓存确定性调用；temperature > 0 时缓存等于抹掉随机性
    can_cache = use_cache and temperature == 0

    # 同 key 单飞：避免并发同时 miss -> 同一内容重复付费
    guard = cache.key_lock(key) if can_cache else nullcontext()
    with guard:
        if can_cache:
            hit = cache.get(key)
            if hit:
                if budget is not None:
                    budget.spend(0)                # 命中不计 token，但计一次"调用"
                log_call(CallRecord(
                    ts=now_iso(), step=step, model=model,
                    prompt_tokens=0, completion_tokens=0,
                    cost=0.0, cached=True, latency_ms=0,
                ))
                return Reply(hit["text"], model, 0, 0, 0.0, True, 0)

        reply = _request(model, messages, temperature, max_tokens, step, budget)
        if can_cache:
            cache.put(key, {"text": reply.text})
        return reply
