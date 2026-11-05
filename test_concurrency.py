"""验证：并发跑 6 个问题，限并发 3，不会 429。"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

from infra import Budget
from llm import client
from runner import run_pipeline

load_dotenv()

TOPICS = [
    "文档切分策略怎么选",
    "大模型应用的成本控制",
    "如何评估检索效果",
    "混合检索和 rerank 的区别",
    "向量库选型对比",
    "提示词注入怎么防",
]


def worker(topic: str) -> str:
    budget = Budget(max_calls=20, max_tokens=60000, max_seconds=300)
    try:
        run_pipeline(topic, budget, client())
        return f"OK   {topic}"
    except Exception as exc:                      # noqa: BLE001
        return f"FAIL {topic}  {exc}"


def main() -> None:
    t0 = time.time()
    # max_workers 要 >= llm 内部的 MAX_CONCURRENCY 才有意义
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(worker, t) for t in TOPICS]
        for f in as_completed(futures):
            print(f.result())
    print(f"\n总耗时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
