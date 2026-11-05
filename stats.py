"""成本报表：读 logs/calls.jsonl 出统计。"""

import sqlite3
import statistics
from collections import defaultdict

import cache
from ledger import read_calls


def fmt_money(v: float) -> str:
    return f"¥{v:.6f}"


def main() -> None:
    calls = read_calls()
    if not calls:
        print("还没有调用记录，先跑一次 runner.py")
        return

    total_cost = sum(c["cost"] for c in calls)
    cached = [c for c in calls if c["cached"]]
    real = [c for c in calls if not c["cached"]]

    print("=== 总计 ===")
    print(f"调用            {len(calls)} 次")
    print(f"  命中缓存      {len(cached)} 次（{len(cached)/len(calls)*100:.0f}%）")
    print(f"  真实调用      {len(real)} 次")
    print(f"总成本          {fmt_money(total_cost)}")
    if real:
        print(f"平均每次真实调用 {fmt_money(total_cost/len(real))}")

    print("\n=== 按步骤 ===")
    by_step: dict[str, list[dict]] = defaultdict(list)
    for c in calls:
        by_step[c["step"] or "(未标注)"].append(c)

    header = f"{'步骤':<12}{'次数':>6}{'成本':>14}{'占比':>8}{'平均耗时':>10}"
    print(header)
    print("-" * 52)
    for step, rows in sorted(by_step.items(), key=lambda kv: -sum(r["cost"] for r in kv[1])):
        cost = sum(r["cost"] for r in rows)
        share = cost / total_cost * 100 if total_cost else 0
        lat = [r["latency_ms"] for r in rows if not r["cached"]]
        avg_lat = f"{statistics.mean(lat):.0f}ms" if lat else "-"
        print(f"{step:<12}{len(rows):>6}{fmt_money(cost):>14}{share:>7.0f}%{avg_lat:>10}")

    print("\n=== 按模型 ===")
    by_model: dict[str, int] = defaultdict(int)
    for c in calls:
        by_model[c["model"]] += 1
    for m, n in sorted(by_model.items(), key=lambda kv: -kv[1]):
        print(f"  {m:<24}{n} 次")

    lat = [c["latency_ms"] for c in real]
    if lat:
        lat.sort()
        p50 = lat[len(lat) // 2]
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
        print("\n=== 延迟 ===")
        print(f"  P50 {p50}ms   P95 {p95}ms   最快 {lat[0]}ms   最慢 {lat[-1]}ms")

    # cache.db 可能被外部占用或损坏，不能让整份报表在最后一行崩掉
    try:
        size: str | int = cache.size()
    except sqlite3.DatabaseError:
        size = "不可用（cache.db 损坏或被占用）"
    print(f"\n缓存库现有 {size} 条")


if __name__ == "__main__":
    main()
