"""Day 16 · 三个基础设施：预算 / 重试 / 超时

相对教程原版修了两处真 bug：
1. ``classify()`` 原本定义了却没人调用。SDK 抛出的 APITimeoutError /
   RateLimitError / APIStatusError 并不是 RetryableError，而 with_retry 只
   捕获 RetryableError —— 结果是「重试」形同虚设，原始异常还会直接冒到
   main() 变成 traceback。现在由 with_retry 统一翻译一次。
2. ``run_with_timeout`` 原先用 ``with ThreadPoolExecutor(...)``。future
   超时后 __exit__ 会执行 shutdown(wait=True)，阻塞主线程一直等底层调用
   结束，超时等于没设。改成显式 shutdown(wait=False)。

超时后后台线程杀不掉（Python 限制），所以每个 LLM 调用自身也必须带
timeout=，两层配合才安全。
"""

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout


# ---------------------------------------------------------------- 异常：先定义
class BudgetExceeded(RuntimeError):
    """预算耗尽，必须立刻停止。"""


class RetryableError(RuntimeError):
    """临时性错误，等一会儿重试可能成功。"""


class FatalError(RuntimeError):
    """确定性错误，重试多少次都一样。"""


# ---------------------------------------------------------------- 预算
class Budget:
    """三重硬上限：调用次数、token、总耗时。任一超限立刻停。"""

    def __init__(
        self,
        max_calls: int = 20,
        max_tokens: int = 60000,
        max_seconds: float = 180,
    ) -> None:
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.max_seconds = max_seconds
        self.calls = 0
        self.tokens = 0
        self.start = time.time()

    def spend(self, tokens: int = 0) -> None:
        self.calls += 1
        self.tokens += tokens
        self.check()

    def check(self) -> None:
        if self.calls > self.max_calls:
            raise BudgetExceeded(f"调用次数超过上限 {self.max_calls}")
        if self.tokens > self.max_tokens:
            raise BudgetExceeded(f"token 超过上限 {self.max_tokens}")
        if time.time() - self.start > self.max_seconds:
            raise BudgetExceeded(f"总耗时超过 {self.max_seconds} 秒")

    def report(self) -> str:
        return (
            f"调用 {self.calls}/{self.max_calls} 次，"
            f"token {self.tokens}/{self.max_tokens}，"
            f"耗时 {time.time() - self.start:.1f}s"
        )


# ---------------------------------------------------------------- 异常分类
def classify(exc: Exception) -> Exception:
    """把 SDK 的异常翻译成可重试 / 不可重试。

    可重试：超时、连接失败、429 限流、5xx 服务端抖动。
    不可重试：401 密钥错、400 参数错、402 余额不足、其它确定性错误。
    """
    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        RateLimitError,
    )

    if isinstance(exc, (APITimeoutError, APIConnectionError, RateLimitError)):
        return RetryableError(str(exc))
    if isinstance(exc, APIStatusError):
        if exc.status_code in (500, 502, 503, 504):
            return RetryableError(str(exc))
        return FatalError(f"HTTP {exc.status_code}: {exc}")
    return FatalError(str(exc))


# ---------------------------------------------------------------- 超时
def run_with_timeout(fn, seconds: float, *args, **kwargs):
    """Windows 上 signal.alarm 不可用，用线程池实现超时。

    刻意不用 ``with ThreadPoolExecutor(...)``：超时后 __exit__ 的
    shutdown(wait=True) 会一直阻塞到线程跑完，超时就白设了。
    """
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=seconds)
        except FutTimeout as exc:
            future.cancel()
            raise RetryableError(f"步骤超时（{seconds}s）") from exc
    finally:
        # 不等待后台线程；真正结束它靠每次 LLM 调用自带的 timeout=
        pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------- 重试
def with_retry(fn, *, retries: int = 3, base_delay: float = 1.0, budget: Budget):
    """指数退避重试（1s → 2s → 4s）。只重试 RetryableError。

    这里是 classify() 唯一的调用点：SDK 原始异常先翻译再决定是否重试。
    FatalError / BudgetExceeded 直接上抛，不浪费配额和时间。
    """
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        budget.check()
        try:
            return fn(attempt)
        except (FatalError, BudgetExceeded):
            raise
        except Exception as exc:  # noqa: BLE001 - 统一在这里做异常分类
            err = exc if isinstance(exc, RetryableError) else classify(exc)
            if not isinstance(err, RetryableError):
                raise err from exc
            last = err
            if attempt == retries:
                break
            delay = base_delay * (2 ** (attempt - 1))
            print(f"    第 {attempt} 次失败（{err}），{delay:.1f}s 后重试")
            time.sleep(delay)
    raise FatalError(f"重试 {retries} 次仍失败：{last}")
