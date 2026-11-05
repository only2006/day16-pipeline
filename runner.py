"""编排：顺序执行四个步骤，每步产物落盘。"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from openai import OpenAI

from infra import Budget, BudgetExceeded, FatalError, run_with_timeout, with_retry
from steps import make_plan, make_report, make_search, make_summarize

load_dotenv()

BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")


def run_pipeline(topic: str, budget: Budget, client: OpenAI) -> dict:
    steps = [
        make_plan(client, budget),
        make_search(budget),
        make_summarize(client, budget),
        make_report(client, budget),
    ]

    ctx = {"topic": topic}
    # 毫秒 + 随机短码：并发时多个线程可能落在同一毫秒，
    # 只靠时间戳会让 run_dir 撞车、产物互相覆盖（实测 6 并发时复现过）。
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3] + "-" + uuid4().hex[:6]
    run_dir = Path("runs") / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.txt").write_text(topic, encoding="utf-8")

    timeline = []
    for step in steps:
        print(f"[{step.name}] 开始")
        t0 = time.time()
        try:
            out = with_retry(
                lambda attempt, s=step: run_with_timeout(s.run, s.timeout, ctx),
                retries=step.retries,
                budget=budget,
            )
        except (FatalError, BudgetExceeded) as exc:
            print(f"[{step.name}] 失败：{exc}")
            if step.optional:
                ctx[step.name] = {}
                continue
            raise
        except Exception as exc:  # noqa: BLE001 - 未预期异常也要留个明确说法
            print(f"[{step.name}] 未预期异常：{type(exc).__name__}: {exc}")
            if step.optional:
                ctx[step.name] = {}
                continue
            raise

        ctx[step.name] = out
        cost = time.time() - t0
        timeline.append({"step": step.name, "seconds": round(cost, 2)})
        print(f"[{step.name}] 完成，{cost:.1f}s，{budget.report()}")

        (run_dir / f"{step.name}.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    report = ctx.get("report", "")
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    (run_dir / "timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "budget.json").write_text(
        json.dumps(
            {
                "topic": topic,
                "calls": budget.calls,
                "tokens": budget.tokens,
                "max_calls": budget.max_calls,
                "max_tokens": budget.max_tokens,
                "max_seconds": budget.max_seconds,
                "elapsed_seconds": round(time.time() - budget.start, 2),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\n产物目录：{run_dir}")
    print(f"预算：{budget.report()}")
    return {
        "report": report,
        "run_dir": str(run_dir),
        "timeline": timeline,
        "calls": budget.calls,
        "tokens": budget.tokens,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="四步研究流水线")
    parser.add_argument("topic", help="研究主题")
    parser.add_argument("--max-calls", type=int, default=20)
    parser.add_argument("--max-tokens", type=int, default=60000)
    parser.add_argument("--max-seconds", type=float, default=180)
    args = parser.parse_args()

    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        print("没有配置 DEEPSEEK_API_KEY")
        return 1

    client = OpenAI(api_key=key, base_url=BASE_URL, timeout=60)
    budget = Budget(args.max_calls, args.max_tokens, args.max_seconds)

    try:
        run_pipeline(args.topic, budget, client)
    except BudgetExceeded as exc:
        print(f"\n[中止] 预算耗尽：{exc}")
        return 2
    except FatalError as exc:
        print(f"\n[中止] 不可重试错误：{exc}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
