# Day 16 · 多步任务编排：四步研究流水线

一个**确定性状态机**（不是自由 Agent）：给定主题，固定走四步，每步产物落盘，全程受「预算 / 重试 / 超时」三重护栏约束。

```
主题 → ① plan 拆 3 个子问题 → ② search 本地检索 → ③ summarize 逐条压要点 → ④ report 出 Markdown 报告
```

> 能用状态机解决的，不要用自由 agent。**可预测 > 聪明**，尤其你要为成本负责的时候。

## 快速开始

```powershell
uv sync
# 在 .env 里填好密钥（.env 已被 .gitignore 排除，绝不入库）
uv run runner.py "RAG 的检索质量优化"     # 跑一次
uv run stress.py                          # 跑 10 次压测 + 算成本
```

`.env` 格式（**值不要提交**）：

```
DEEPSEEK_API_KEY=your_key_here
LLM_MODEL=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com
```

单次运行会在 `runs/<时间戳>-<毫秒>/` 下产出：

| 文件 | 内容 |
|---|---|
| `input.txt` | 本次主题 |
| `plan.json` | 3 个子问题 |
| `search.json` | 每个子问题的检索命中（带来源与分数） |
| `summarize.json` | 每个子问题的要点（带 `[来源]`） |
| `report.json` / `report.md` | 最终 Markdown 报告 |
| `timeline.json` | 各步耗时 |
| `budget.json` | 本次调用次数 / token / 耗时与上限 |

`runs/` 已加入 `.gitignore`，是运行产物，不进仓库。

## 目录结构

```
infra.py      Budget / classify / run_with_timeout / with_retry  ← 横切关注点
corpus.py     极简检索（字符重合度打分，故意不依赖 day8-rag）
steps.py      四个步骤，每个都是「读 ctx → 返回结果」的纯函数
runner.py     顺序编排 + 产物落盘 + CLI
stress.py     10 次压测 + 成本统计
docs/         语料（Day 8–14 的 RAG 笔记 + 原有示例文档）
```

## 三重护栏

| 护栏 | 实现 | 防什么 |
|---|---|---|
| 预算硬上限 | `Budget`：调用次数 20 / token 60000 / 总耗时 180s，每次调用前 `check()` | 死循环、上下文塞爆、某步挂住 |
| 失败分类 + 指数退避 | `classify()` 把 SDK 异常翻译成 `RetryableError` / `FatalError`；`with_retry` 只重试前者，退避 1s→2s→4s | 把 401 重试三次白烧配额 |
| 超时两层 | `run_with_timeout`（线程池）+ 每个 LLM 调用的 `timeout=` | 只设一层不生效 |

`search` 步骤标了 `optional=True`：检索失败只降级（跳过），不让整份报告生成失败。

## 相对教程修正的问题（可讲给面试官）

| # | 问题 | 修法 |
|---|---|---|
| 1 | 教程说 `infra.py` 已包含 2.2/2.3 的代码，实际给出的文件只有 `Budget`，照抄会 `ImportError` | 补齐 `RetryableError` / `FatalError` / `classify` / `run_with_timeout` / `with_retry` |
| 2 | `classify()` 定义了却**从没被调用**，SDK 原始异常不是 `RetryableError` → 重试逻辑形同虚设、异常直接冒 traceback | 在 `with_retry` 里统一翻译一次；`FatalError` / `BudgetExceeded` 直接上抛 |
| 3 | `run_with_timeout` 用 `with ThreadPoolExecutor(...)`，超时后 `__exit__` 的 `shutdown(wait=True)` 会阻塞主线程 → 超时白设 | 改显式 `shutdown(wait=False, cancel_futures=True)`，实测 0.2s 超时 0.20s 返回 |
| 4 | `summarize` 是 **3 次串行调用**，但 step 超时只给 60s，等于单次超时 → 会误判超时并把 3 次调用整个重做（成本翻倍） | 单次 45s、step 150s；`report` 的 step 超时也调到 110s（大于 client 的 90s） |
| 5 | `Step.run: callable` 用了内建函数当类型注解 | 改 `Callable[[dict], Any]` |
| 6 | `.gitignore` 漏了 `runs/`；`main.py` 还是 `uv init` 的 Hello 模板 | 补 `runs/`；`main.py` 改成转发到 `runner.main` |
| 7 | `docs/` 全是智能家居语料，压测主题全是 RAG → 检索结果不相关 | 补 4 篇 RAG 笔记进 `docs/`，实测命中 `chunking.md` / `retrieval-eval.md` / `retrieval_compare.md` |

另外补了：`plan` 输出容错（去 ```json 围栏、从文本里捞数组、强制转字符串、硬截断 3 条）、`usage` 为 None 时不炸、run_dir 带毫秒防覆盖、压测结果落盘 `runs/stress-summary.json`。

## 验收结果（2026-11-05 实测）

```
成功 10/10
平均耗时 6.6s，最慢 8.8s
平均 token 2882，单次最多 3880
最多调用 5 次（上限 20，无异常飙升）
```

四步产物齐全，报告带 `[来源]` 引用且内容来自检索资料；单次示例 8.3s / 5 次调用 / 3447 token。

## 成本（实测，不是估算抄书）

```
10 次运行共 28,815 token（平均 2,882 / 次）
按 1.25–2.5 元 / 百万 token 估算 ≈ 0.036–0.072 元
```

教程给的口径是「10 次 × 8000 token ≈ 8 万 token，约 0.1–0.2 元」。本项目实测只有它的约 **36%**：每段检索材料截 600 字、各步设了 `max_tokens` 上限、每次固定 5 次调用，所以 token 数是可预测且更省的。

> 单价只是量级估算（沿用教程口径、未区分输入/输出 token，价格会调整，以 DeepSeek 官网为准）；**精确的 token 数**才是硬数据，已存在 `runs/stress-summary.json`。

## 已知边界

- `corpus.py` 是字符重合度检索，故意朴素 —— 今天考的是编排，不是检索质量。
- 超时后后台线程杀不掉（Python 限制），靠每个 LLM 调用自身的 `timeout=` 让它最终结束。
- 压测会真实调用 API 并产生费用，跑之前先跑一次 `runner.py` 看单次 token。
