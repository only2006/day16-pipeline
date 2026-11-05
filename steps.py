"""四个步骤：plan / search / summarize / report

每一步都是「读 ctx → 返回结果」的纯函数，可以单独测、单独重跑。
Day 17 起所有 LLM 调用统一走 llm.chat（路由 / 缓存 / 记账 / 限并发），
budget.spend 由 llm.chat 内部统一执行，这里不再重复计费。
"""

import json
import os
from dataclasses import dataclass
from typing import Any, Callable

from openai import OpenAI

import corpus
import llm
from infra import Budget, FatalError

# 保留兼容：实际用哪个模型由 llm.pick_model(tier) 决定
MODEL = os.getenv("LLM_MODEL", "deepseek-chat")


@dataclass
class Step:
    name: str
    run: Callable[[dict], Any]
    retries: int = 3
    timeout: float = 60.0
    optional: bool = False


PLAN_PROMPT = """把研究主题拆成 3 个可以独立检索的子问题。

要求：
1. 每个问题都要具体，能直接拿去搜索
2. 只输出 JSON 数组，例如 ["问题1","问题2","问题3"]
3. 不要解释"""

SUMMARY_PROMPT = """根据资料回答问题，输出 3 条以内的要点。
每条要点后面用 [来源文件名] 标注出处。
资料里没有的不要编。"""

REPORT_PROMPT = """把下面的研究要点整合成一份 Markdown 报告。

结构：
## 概要（3 句话）
## 分点结论（每点带 [来源]）
## 参考来源（列表）

要求：只用手上的要点，不要新增信息。"""


def _parse_plan(raw: str) -> list[str]:
    """模型偶尔会裹 ```json 代码块或加一句解释，这里做容错 + 硬截断。"""
    text = raw.strip()
    for fence in ("```json", "```JSON", "```"):
        if text.startswith(fence):
            text = text[len(fence):]
            break
    text = text.strip().removesuffix("```").strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            raise FatalError(f"plan 返回不是合法 JSON：{text[:120]}")
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise FatalError(f"plan 返回不是合法 JSON：{text[:120]}") from exc

    if not isinstance(data, list) or not data:
        raise FatalError(f"plan 返回格式不对：{text[:120]}")
    # 代码层硬截断：不管 prompt 怎么写，模型都可能给 5 个
    return [str(q).strip() for q in data[:3]]


def make_plan(client: OpenAI, budget: Budget) -> Step:
    def run(ctx: dict) -> list[str]:
        reply = llm.chat(
            [{"role": "user", "content": f"{PLAN_PROMPT}\n\n主题：{ctx['topic']}"}],
            step="plan", tier="small", max_tokens=300, budget=budget,
        )
        return _parse_plan(reply.text)

    # 1 次调用；llm 层超时由 REQUEST_TIMEOUT 统一控制，线程池这层给 45s
    return Step("plan", run, retries=2, timeout=45)


def make_search(budget: Budget) -> Step:
    """本地检索，不花 token，所以不调 budget.spend()。"""

    def run(ctx: dict) -> dict:
        results = {}
        for q in ctx["plan"]:
            hits = corpus.search(q, k=3)
            results[q] = hits
            if not hits:
                print(f"    [WARN] 子问题没检索到内容：{q}")
        return results

    # optional=True：检索失败不该让整个报告生成失败（降级比失败好）
    return Step("search", run, retries=2, timeout=30, optional=True)


def make_summarize(client: OpenAI, budget: Budget) -> Step:
    def run(ctx: dict) -> dict:
        out = {}
        for q, hits in ctx["search"].items():
            if not hits:
                out[q] = "（未检索到相关资料）"
                continue
            material = "\n\n".join(
                f"[{h['source']}]\n{h['text'][:600]}" for h in hits
            )
            reply = llm.chat(
                [{"role": "user",
                  "content": f"{SUMMARY_PROMPT}\n\n资料：\n{material}\n\n问题：{q}"}],
                step="summarize", tier="big", max_tokens=400, budget=budget,
            )
            out[q] = reply.text
        return out

    # 这里是 3 次串行调用！step 超时必须 >= 3 × 单次超时，
    # 否则会误判超时并把 3 次调用整个重做（调用次数/成本翻倍）。
    return Step("summarize", run, retries=2, timeout=150)


def make_report(client: OpenAI, budget: Budget) -> Step:
    def run(ctx: dict) -> str:
        body = "\n\n".join(f"### {q}\n{a}" for q, a in ctx["summarize"].items())
        reply = llm.chat(
            [{"role": "user",
              "content": f"{REPORT_PROMPT}\n\n主题：{ctx['topic']}\n\n{body}"}],
            step="report", tier="big", max_tokens=1200, budget=budget,
        )
        return reply.text

    return Step("report", run, retries=2, timeout=110)
