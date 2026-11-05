"""10 次压测：验证不死循环、不超预算，并把真实成本算出来写进 runs/。"""

import json
import os
import statistics
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from infra import Budget
from runner import run_pipeline

load_dotenv()

BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")

# 成本估算单价：元 / 百万 token。价格会调整，以 DeepSeek 官网为准。
# 沿用 Day16 教程口径：8 万 token ≈ 0.1–0.2 元 → 1.25–2.5 元/百万 token。
# 未区分输入/输出，只用于量级估算，不是账单。
PRICE_LOW_PER_MTOK = 1.25
PRICE_HIGH_PER_MTOK = 2.50

TOPICS = [
    "RAG 的检索质量优化",
    "文档切分策略怎么选",
    "大模型应用的成本控制",
    "如何评估检索效果",
    "混合检索和 rerank 的区别",
    "向量库选型对比",
    "提示词注入怎么防",
    "Agent 和流水线怎么选",
    "流式输出怎么实现",
    "如何做引用溯源",
]


def main() -> int:
    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        print("没有配置 DEEPSEEK_API_KEY，请在 .env 里填好再跑")
        return 1

    client = OpenAI(api_key=key, base_url=BASE_URL, timeout=60)
    results = []

    for i, topic in enumerate(TOPICS, start=1):
        budget = Budget(max_calls=20, max_tokens=60000, max_seconds=180)
        t0 = time.time()
        try:
            r = run_pipeline(topic, budget, client)
            results.append({
                "ok": True,
                "topic": topic,
                "seconds": round(time.time() - t0, 2),
                "calls": budget.calls,
                "tokens": budget.tokens,
                "steps": len(r["timeline"]),
            })
        except Exception as exc:  # noqa: BLE001 - 压测要记录失败而不是中断
            results.append({
                "ok": False,
                "topic": topic,
                "seconds": round(time.time() - t0, 2),
                "calls": budget.calls,
                "tokens": budget.tokens,
                "error": f"{type(exc).__name__}: {exc}",
            })
        flag = "OK  " if results[-1]["ok"] else "FAIL"
        print(f"[{i:2d}/{len(TOPICS)}] {flag} {topic} "
              f"({results[-1]['seconds']:.1f}s, {results[-1]['tokens']} token)")

    ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    total_tokens = sum(r["tokens"] for r in results)
    avg_tokens = statistics.mean(r["tokens"] for r in ok) if ok else 0
    low = total_tokens / 1_000_000 * PRICE_LOW_PER_MTOK
    high = total_tokens / 1_000_000 * PRICE_HIGH_PER_MTOK

    print(f"\n成功 {len(ok)}/{len(TOPICS)}")
    if ok:
        print(f"平均耗时 {statistics.mean(r['seconds'] for r in ok):.1f}s，"
              f"最慢 {max(r['seconds'] for r in ok):.1f}s")
        print(f"平均 token {avg_tokens:.0f}，单次最多 {max(r['tokens'] for r in ok)}")
        print(f"最多调用 {max(r['calls'] for r in ok)} 次"
              f"（上限 20，无异常飙升）")
    print(f"\n成本估算：{len(TOPICS)} 次运行共 {total_tokens:,} token "
          f"（平均 {avg_tokens:.0f}/次）")
    print(f"按 {PRICE_LOW_PER_MTOK}–{PRICE_HIGH_PER_MTOK} 元/百万 token 估算，"
          f"约 {low:.3f}–{high:.3f} 元")
    if failed:
        print(f"\n失败明细：")
        for r in failed:
            print(f"  - {r['topic']}: {r['error']}")

    summary = {
        "total": len(TOPICS),
        "succeeded": len(ok),
        "failed": len(failed),
        "total_tokens": total_tokens,
        "avg_tokens_per_run": round(avg_tokens, 1),
        "avg_seconds_per_run": round(statistics.mean(r["seconds"] for r in ok), 2) if ok else None,
        "max_calls_in_one_run": max((r["calls"] for r in ok), default=0),
        "estimated_cost_cny": {
            "low": round(low, 4),
            "high": round(high, 4),
            "price_per_mtok_cny": [PRICE_LOW_PER_MTOK, PRICE_HIGH_PER_MTOK],
            "note": "估算值，未区分输入/输出 token，价格以 DeepSeek 官网为准",
        },
        "runs": results,
    }
    Path("runs").mkdir(exist_ok=True)
    out = Path("runs") / "stress-summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写入 {out}")
    return 0 if len(ok) >= 9 else 2   # 教程验收线：≥ 9/10


if __name__ == "__main__":
    raise SystemExit(main())
