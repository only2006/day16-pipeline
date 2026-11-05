# Day 16–17 · 多步研究流水线 + 成本可观测

一个**确定性状态机**（不是自由 Agent）：给定主题，固定走四步，每步产物落盘，全程受「预算 / 重试 / 超时」三重护栏约束；Day 17 起所有 LLM 调用统一收口到 `llm.chat`，成本可见、可缓存、可路由。

```
主题 → ① plan 拆 3 个子问题 → ② search 本地检索 → ③ summarize 逐条压要点 → ④ report 出 Markdown 报告
```

> 能用状态机解决的，不要用自由 agent。**可预测 > 聪明**，尤其你要为成本负责的时候。

## 快速开始

```powershell
uv sync
# 在 .env 里填好密钥（.env 已被 .gitignore 排除，绝不入库）
uv run runner.py "RAG 的检索质量优化"     # 跑一次
uv run stress.py                          # Day16：10 次压测
uv run test_cost.py                       # Day17：同主题跑两次，看缓存省钱
uv run stats.py                           # Day17：成本报表
uv run test_concurrency.py                # Day17：6 并发限流验证
```

`.env` 格式（**值不要提交**）：

```
DEEPSEEK_API_KEY=your_key_here
LLM_MODEL=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com
```

可选环境变量：`LLM_MODEL_SMALL`（不配则 `tier="small"` 仍用主模型）、`MAX_CONCURRENCY`（默认 3）、`REQUEST_TIMEOUT`（默认 60 秒）。

单次运行会在 `runs/<时间戳>-<毫秒>/` 下产出 `input.txt` / `plan.json` / `search.json` / `summarize.json` / `report.json` / `report.md` / `timeline.json` / `budget.json`。`runs/`、`logs/`、`cache.db` 都是运行产物，已加入 `.gitignore`。

## 目录结构

```
infra.py      Budget / classify / run_with_timeout / with_retry  ← 横切关注点
corpus.py     极简检索（字符重合度打分，故意不依赖 day8-rag）
steps.py      四个步骤，每个都是「读 ctx → 返回结果」的纯函数
runner.py     顺序编排 + 产物落盘 + CLI
stress.py     Day16 的 10 次压测
pricing.py    价格表（元/百万 token，输入与输出分开）
ledger.py     每次调用追加一行 JSONL 台账
cache.py      sqlite 结果缓存（sha256 键 + TTL 3600s）
llm.py        ★ 统一入口：路由 / 缓存 / 记账 / 限并发
stats.py      按步骤 / 模型出成本报表
test_cost.py / test_concurrency.py  验证脚本
docs/         语料（Day 8–14 的 RAG 笔记 + 原有示例文档）
```

## 三重护栏

| 护栏 | 实现 | 防什么 |
|---|---|---|
| 预算硬上限 | `Budget`：调用次数 20 / token 60000 / 总耗时 180s，每次调用前 `check()` | 死循环、上下文塞爆、某步挂住 |
| 失败分类 + 指数退避 | `classify()` 把 SDK 异常翻译成 `RetryableError` / `FatalError`；`with_retry` 只重试前者，退避 1s→2s→4s | 把 401 重试三次白烧配额 |
| 超时两层 | `run_with_timeout`（线程池，45/150/110s）+ `llm.client()` 的 `REQUEST_TIMEOUT`（默认 60s） | 只设一层不生效 |

`search` 步骤标了 `optional=True`：检索失败只降级（跳过），不让整份报告生成失败。

## Day 17 · 成本可观测

**所有 LLM 调用必须走 `llm.chat`，否则统计不全。** 数据流：

```
路由 pick_model(tier) → 查缓存(命中即 0 元) → 限并发调 API(429 退避) → 写 ledger + 回填缓存
```

- **计费**：`成本 = 输入token × 输入单价 + 输出token × 输出单价`，单价在 `pricing.py`，单位元/百万 token，输出通常比输入贵 3–4 倍。
- **缓存**：键 = `sha256(model + messages + temperature + max_tokens)`，**只缓存 `temperature == 0`**（否则等于抹掉随机性），TTL 3600 秒；**命中也写一条 `cached=True` 的 ledger**，否则看不出省了多少。
- **路由**：`tier="small"` 给 plan 这类简单活，`tier="big"` 给 summarize / report。默认两者都是 `deepseek-chat`，要真正降本需在 `.env` 配 `LLM_MODEL_SMALL`。
- **限并发**：全局 `Semaphore(MAX_CONCURRENCY=3)`，外面开再多线程，同时打到 API 的也不超过 3 个。

被问「这个功能成本多少」时，标准答案是三段：**绝对值 → 成本结构（哪一步占大头）→ 优化效果（缓存命中率）**。跑 `uv run stats.py` 就能拿到这三组数。

## 相对教程修正的问题（可讲给面试官）

| # | 问题 | 修法 |
|---|---|---|
| 1 | 教程说 `infra.py` 已包含 2.2/2.3 的代码，实际给出的文件只有 `Budget`，照抄会 `ImportError` | 补齐 `RetryableError` / `FatalError` / `classify` / `run_with_timeout` / `with_retry` |
| 2 | `classify()` 定义了却**从没被调用**，SDK 原始异常不是 `RetryableError` → 重试逻辑形同虚设、异常直接冒 traceback | 在 `with_retry` 里统一翻译一次；`FatalError` / `BudgetExceeded` 直接上抛 |
| 3 | `run_with_timeout` 用 `with ThreadPoolExecutor(...)`，超时后 `__exit__` 的 `shutdown(wait=True)` 会阻塞主线程 → 超时白设 | 改显式 `shutdown(wait=False, cancel_futures=True)`，实测 0.2s 超时 0.20s 返回 |
| 4 | `summarize` 是 **3 次串行调用**，但 step 超时只给 60s，等于单次超时 → 会误判超时并把 3 次调用整个重做（成本翻倍） | step 给 150s；`report` 的 step 110s；Day17 后 API 层超时统一由 `REQUEST_TIMEOUT` 管 |
| 5 | `Step.run: callable` 用了内建函数当类型注解 | 改 `Callable[[dict], Any]` |
| 6 | `.gitignore` 漏了 `runs/`；`main.py` 还是 `uv init` 的 Hello 模板 | 补 `runs/`（Day17 再加 `logs/` / `cache.db*`）；`main.py` 改成转发到 `runner.main` |
| 7 | `docs/` 全是智能家居语料，压测主题全是 RAG → 检索结果不相关 | 补 4 篇 RAG 笔记进 `docs/`，实测命中 `chunking.md` / `retrieval-eval.md` / `retrieval_compare.md` |

Day17 另加固：`llm.py` 独立 `load_dotenv()`（教程依赖 runner 隐式加载）、`resp.usage` 为 None 不崩、`cache` 的 sqlite 连接加 `timeout=10` 防并发锁、`ledger` 写文件加线程锁防半行 JSON。

## 验收结果（2026-11-05 实测）

Day16 的 10 次压测：

```
成功 10/10
平均耗时 6.6s，最慢 8.8s
平均 token 2882，单次最多 3880
最多调用 5 次（上限 20，无异常飙升）
```

四步产物齐全，报告带 `[来源]` 引用且内容来自检索资料；单次示例 8.3s / 5 次调用 / 3447 token。

Day17 的 `uv run test_cost.py`（同主题跑两次）：

```
第 1 次冷启动：5 次调用 / 3418 token / 9.0s
第 2 次全缓存：5 次调用 /    0 token / 0.1s
命中缓存 5/10（50%），总成本 ¥0.012836，平均每次真实调用 ¥0.002567
按步骤成本：summarize 55% / report 42% / plan 3%
```

`uv run test_concurrency.py`：6 个主题并发 6/6 成功、无 429，冷跑 15.8s；并发写入的 70 行台账全部可解析。并发时多个线程会落在同一毫秒，所以 `run_dir` 除了毫秒还加了随机短码，否则产物会互相覆盖（实测复现过并已修复）。

## 成本口径

Day16 是**粗估**：`10 次共 28,815 token`，按统一 1.25–2.5 元/百万 token ≈ `0.036–0.072 元`。教程估的是「8 万 token / 0.1–0.2 元」，实测只有它的约 36%。

Day17 起改为**精确分项计费**（`pricing.py` 里输入 2.0 / 输出 8.0 元每百万 token），数字会比粗估偏高，因为输出 token 单价更贵——这正是「让成本可见」要暴露的东西。精确数字看 `logs/calls.jsonl`，汇总看 `uv run stats.py`。

> 单价是示例值，价格会调整，以 DeepSeek 官网为准；改 `pricing.py` 一张表即可，业务代码不用动。

## 已修复的护栏缺陷（第二轮复查）

| # | 问题 | 修法 |
|---|---|---|
| 1 | 429 退避的 `time.sleep` 在 `with _semaphore` 内，抱着许可睡 2/4/8s，其余线程全堵住 | sleep 移到信号量外 |
| 2 | OpenAI SDK 默认 `max_retries=2`，一次 `create` 可能发 3 个真实请求而 `Budget` 只记 1 次 | 三处 `OpenAI(...)` 统一 `max_retries=0`，重试由 `llm._request` 明确管理 |
| 3 | `Budget.check()` 只在事后判断次数，`max_calls=20` 会放到第 21 次调用 | 新增 `check(next_call=True)` 事前预检，`spend()` 与 `with_retry` 都用 |
| 4 | 缓存键不含 `base_url`，换中转端点会命中旧端点的旧答案 | `make_key` 纳入 `base_url` |
| 5 | `cache.get/put` 在并发下击穿，同 key 多线程同时 miss 会重复付费 | `cache.key_lock()` per-key 单飞 + double-check |
| 6 | 只捕获 `RateLimitError`，超时/连接失败/5xx 绕过异常分类 | 复用 `infra.classify()` 统一判定 |
| 7 | `.gitignore` 缺 `cache.db-wal/-shm` 与 `.env.*`；`stats.py` 遇损坏 `cache.db` 崩、JSONL 缺字段会 KeyError | 忽略规则改 `cache.db*` / `.env*`；`stats` 加损坏保护，`ledger.read_calls()` 逐行读 + 字段校验 |
| 8 | `cache._connect()` 建表失败时连接没关，Windows 上文件句柄被占用（连临时目录都删不掉） | 异常路径 `conn.close()` 后再抛出 |

> ⚠️ 缓存键加入 `base_url` 后，**旧 `cache.db` 里的缓存键全部失效**，第一次跑会重新冷启动（多花几分钱），属正常。`cache.py` 同时启用了 WAL，会产生 `cache.db-wal` / `-shm`，均已被忽略。

## 已知边界

- `corpus.py` 是字符重合度检索，故意朴素 —— 考的是编排，不是检索质量。
- 超时后后台线程杀不掉（Python 限制），靠 `REQUEST_TIMEOUT` 让它最终结束。
- `llm.chat` 内部对 429 重试 3 次，`with_retry` 外面还有一层重试；两套叠加时留意 `budget.calls`。
- 压测会真实调用 API 并产生费用，跑之前先跑一次 `runner.py` 看单次 token。
- `logs/calls.jsonl` 是**累计**台账、`cache.db` 是持久缓存。想复现教程里「10 次调用 / ¥0.0128」的干净数字，先清空这两个产物再跑 `test_cost.py`（都不入库，清了不影响代码）。
