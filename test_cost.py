"""验证：跑两次同一个主题，第二次应该几乎免费。"""

from dotenv import load_dotenv

import stats as stats_mod
from infra import Budget
from llm import client
from runner import run_pipeline

load_dotenv()


def one_round(topic: str, label: str) -> None:
    print(f"\n{'=' * 50}")
    print(f"{label}：{topic}")
    budget = Budget(max_calls=20, max_tokens=60000, max_seconds=180)
    run_pipeline(topic, budget, client())
    print(f"本轮预算：{budget.report()}")


def main() -> None:
    topic = "RAG 的检索质量优化"
    one_round(topic, "第 1 次（冷启动）")
    one_round(topic, "第 2 次（应大量命中缓存）")
    print(f"\n{'=' * 50}")
    stats_mod.main()


if __name__ == "__main__":
    main()
