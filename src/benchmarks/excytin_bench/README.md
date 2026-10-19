# ExCyTIn-Bench Workflow

这个目录提供一个与通用后台 workflow 分离的同步测评流程。它严格遵守
ExCyTIn-Bench 的外部动作协议：Workflow 生成一条 SQL，SQL 由
`ExcytinEnv.step()` 执行，Workflow 在下一轮收到 observation 后才进行
Reviewer 审查，然后自动推进 PM 或进入核验与重规划。

```text
initial observation
  -> Planner creates typed G/E only (no T)
  -> memory_query -> direct PM -> typed PM composition -> EM -> SM v2
  -> Planner selects procedure / episode / table -> allocate T nodes and input E edges
  -> {id, objective, selected memory context} -> Executor one read-only SQL -> program writes execution_code
  -> ExcytinEnv.step(SQL)
  -> observation -> Reviewer (current task + current execution only) -> program writes structured execution_result
  -> pending PM step: bind observed inputs -> next Executor
  -> single step / PM completion or interruption: Verifier (context + question + nested ITT)
  -> confirmed: submit answer / unconfirmed: Planner update -> next T
  -> ExcytinEnv.step(submit=True)
```

EM/SM 单步结束，以及 PM 完成、暂停、中断或预算耗尽时，Verifier 在 Reviewer 后同步调用；PM 正常衔接下一步时跳过 Planner 和 Verifier。Verifier 接收 `context + question + itt`。ITT 为通过 children 连接的 G/E/T 三层嵌套树，包含每个 T 的实际 SQL 和结构化证据（summary/facts/limitations/source），不重复传最新总结。不传独立 executions 列表，不检索长期记忆。

Verifier 输出 `can_answer: "yes"` 或 `can_answer: "no"`，仅 yes 时附加 `answer`。能回答时直接生成最终答案，程序退出循环提交，不再调用最终答案整理器或反证审查。不能回答时，把包含结构化证据的 ITT 及 Verifier 的未确认状态交给 Planner 继续规划；Reviewer 的提交/停止建议不直接终止调查，Planner 的完成状态也不能替代 Verifier 确认。预算耗尽而仍未确认时，程序提交“无法确认答案：调查结束，尚无充分证据。”。Verifier 未确认时，Planner 的重规划必须包含可执行的 todo T；G/E 的推导状态不再触发提前提交。缺少可执行任务按现有有限校验重试要求补充，耗尽后记录规划失败，不递归空转或提交。格式校验失败按有限重试处理，耗尽后显式失败。核验记录保存在 `verifications` 和 `answer_verified` trace 中；答案是否有充分证据由模型判断。

SQL workflow 默认启用 PM 多步流程、EM 单步片段、SM v2 表字段字典。99 条 PM / 320 条 EM 是训练基底；运行时应用 57 条 PM 屏蔽清单和独立失败案例调整层，有效数量见[记忆审计](../../memory/longterm_memory/procedural_memory/flows/failure_review/audit.json)。Planner 请求依次检索：本层没有候选时自动下探；有候选但不适用时可用 `memory_query.layer` 进入 composed、episodic 或 semantic；保持 known/wanted 的重复请求也会继续下一层，不要求意图逐字相同。流程使用 `add_task.procedure`，单步使用 `add_task.episode`，选表使用 `add_task.table`。选定 PM 首步后自动预建全部 T，后续 SQL 在执行前才绑定真实结果。EM 发现新线索后，下一轮规划必须重新从 PM 检索。检索及选择分别记入三类日志，目录指纹记入 `memory_catalog_ids`。`procedural_memory_enabled=False` 保留 SM-only 对照；`semantic_memory_enabled=False` 保留无记忆基线。详见 [三层协议](../../memory/longterm_memory/procedural_memory/flows/README.md)。

本地 runner 在开始规划前读取当前数据库 information_schema 快照，仅用于后台过滤映射，完整 schema 不进入提示词。这次初始化读取不计入模型调查 SQL 次数。SecGym adapter 未提供数据库快照时采用目录并集并标记 catalog_union，实际可用性仍以执行反馈为准。

核心接口：

```python
workflow.start(question)
action = workflow.propose_next_action()
observation, _, _, info = env.step(*action.as_secgym_action())
workflow.accept_observation(
    observation,
    query_success=info.get("query_success"),
)
```

官方 runner 可使用 `ExcytinBenchAgent` 的兼容接口：

```python
from src.benchmarks.excytin_bench import ExcytinBenchAgent

agent = ExcytinBenchAgent(max_steps=25)
observation, info = env.reset(question_index)
agent.reset(**info)
for _ in range(env.max_steps):
    action, submit = agent.act(observation)
    observation, reward, done, info = env.step(action, submit=submit)
    if submit or done:
        break
```

注意：

- 初始字典只读取 `context` 和 `question`，不会读取答案、solution 或 Ground Truth。
- Executor 收到当前 T 的 id/objective 和选中的 procedural_context、episodic_context 或 semantic_context。Planner 必须在 objective 中写齐实体、参数和查询范围。SQL error 后新增独立 error 字段，包含重新生成说明、失败 SQL 和报错结果，objective 与 semantic_context 保持原样；程序校验 SQL 并在执行前写入 execution_code。
- PM/EM 路径由运行时验证输入和真实上游结果，生成 bound_sql；Executor 返回该语句，AST 校验禁止改写。PM 通过 active_runs 继续后续步骤，所有步骤先建立 T，依赖步骤的参数与 SQL 在上游真实返回后才绑定。
- Planner 首轮接收 context/question、概念词汇及检索协议，随后接收自己检索到的候选摘要；后续还接收 ITT 与未确认状态。runtime_info 仅供运行记录使用，不传给模型。默认 G/E-first 规划阶段最多 12 次模型交互（包括检索、实体更新和任务选择），必须保留最后一次用于选 T；SM-only 对照仍为最多 5 次检索请求；每次调查最多 32 次实际 SM 检索，不占 SQL 动作预算。
- Reviewer 不检索 SM/PM/EM。仅接收当前 task（id/objective）和本次 executor_result，不传全树、全局背景/问题或累计执行记录。
- Reviewer 输出 summary（最多 240 字符）、facts（记录对象列表，保留关联键、主机、PID/GUID、时间和路径等原值）、limitations（缺失、歧义或省略范围）。程序补充 source，整体写入 T.execution_result；原始结果保留在 executions。错误/空结果的 facts 强制为空；截断强制补充限制。
- 每轮 Reviewer 后调用 Verifier；仅 Verifier 确认可以作答时跳过 Planner 并提交其 answer。
- SQL 必须是单条、MySQL 可解析的只读语句。
- 每题默认最多 25 个 benchmark action；Workflow 会预留最后一个 action 用于提交最终答案，因此成功提交前最多执行 24 条 SQL。
- Executor 针对同一个 L3 的内部查询循环最多执行 2 条 SQL。Semantic 路径 SQL error 时用失败 SQL 和实际错误重试，PM/EM 模板错误直接交回规划；空结果按正常返回回写 T 并直接交给 Reviewer，不在 Executor 内重试。SQL 报错达到次数上限后交回 Reviewer/Planner。
- 仅 Semantic 路径 SQL error 允许一次基于实际错误的修复，同时受每个 T 总计 2 次的硬上限约束；已移除 max_empty_repairs 参数。空结果不表示问题解决，Planner 可根据结构化证据新增调查任务。
- 单次 LLM 请求默认 120 秒超时，使用当前 provider 的 `*_REQUEST_TIMEOUT_SECONDS` 配置。所有角色遇到超时、连接失败、HTTP 408/409/429 或 5xx 时，随机等待 5–10 秒后重试，最多重试两次（总计三次请求），请求内容保持不变。401/403 等非临时错误直接抛出；重试耗尽后由 runner 记录本题失败。
- 接口重试由 workflow 统一处理，默认模型调用关闭 SDK 内部重试，避免与 `*_MAX_RETRIES` 叠加。自定义 llm 回调也应关闭其内部重试。trace 记录 `llm_api_retry_scheduled` 和 `llm_api_retries_exhausted`。
- Executor 输出格式或本地 SQL 语法校验失败时，默认附上校验错误再请求一次（`executor_format_attempts=2`）；写操作、多语句等禁止操作直接拒绝。接口重试与格式修正各自计数，只有最终通过校验的 SQL 才计为查询动作。

## 并行与速率限制

默认 `--workers 3`，并行运行 3 道题，每题内部仍依序调用角色和数据库。
结果文件由主线程汇总，单题使用独立 Workflow 与数据库连接。
`SOCAGENT_BENCHMARK_WORKERS` 可修改 CLI 默认值。

统一 `src.agent.llm` 入口共享进程内限流：`PARATERA_RPM_LIMIT=2500`、
`PARATERA_TPM_LIMIT=5000000`，`PARATERA_RATE_UTILIZATION=0.8`，有效预算
为 2000 RPM / 400 万 TPM。请求前按 UTF-8 字节数保守估算输入，并预留
`PARATERA_OUTPUT_TOKEN_RESERVE=16384` 输出 tokens，完成后按 API usage 修正。
输出预留不是输出长度上限，估算与服务商计数规则也可能不同，不能保证绝不触发 429。
失败请求仍保留估算额度。不要同时启动多个评测进程来绕过共享额度；直接使用
OpenAI SDK 的临时比较脚本也不经过此限流器。多进程或其他程序共用账号时需额外协调。

## 随机抽取 10 道测试题

先加载本地 Neo4j 和 MySQL Docker 的密码，再运行固定随机种子的 10 题评测：

```bash
set -a
source docker/neo4j/.env
source docker/excytin-mysql/.env
set +a


PYTHONPATH=. uv run python -m src.benchmarks.excytin_bench.runner \
  --sample-size 10 \
  --seed 42 \
  --max-steps 25 \
  --llm-eval
```

每次运行保存在 `runtime/excytin_bench/runs/<run-id>/`：

```text
manifest.json       # 随机种子和抽中的题目
results.jsonl       # 每题结束后追加，便于断点保留
summary.json        # 机器可读汇总指标
summary.md          # 人类可读结果表
cases/*.json        # 每道题逐步 action/observation、TTT、Review 和记忆命中
```

默认指标包括 Exact/Containment Match、SQL 成功率、Schema 错误率、空结果率、
紧邻修复率、平均动作数、平均查询数、PM/EM 命中率和用时。`--llm-eval` 会额外
计算模仿论文奖励形态的模糊答案与部分步骤分数；它只有在使用论文相同 evaluator
模型和配置时才能视为官方可比成绩。

### 外部原生 CLI 基线

新增 OpenCode、Qwen Code、Cline 的统一 MCP 测评入口：
`python -m src.benchmarks.external_agents.runner`。
源码安装、模型代理、独立会话、预算和运行示例见
[`external_agents/README.md`](../../../external_agents/README.md)。


## PM 对照实验暂停

LTM 重建期间，`pm_workflow_check` 与程序卡片 PM 在线实验显式拒绝运行。历史版本见 `dfb8646`；现有报告仍可用 `pm_workflow_report` 分析。这仅指旧程序卡片实验。PM 流程和 EM 单步调查片段已接入默认 SQL workflow，分别记录检索与任务选择日志；旧版 EM 失败恢复规则仍未接入。

Benchmark 与通用后台共用 Planner 增量操作协议，定义于 `src/schema/planner_operations.py`。输出仅含 `operations` 列表，支持 `add_goal`（仅首轮）、`add_entity`、`add_task`、`select_task`；程序分配 G/E/T ID、构建完整树并保留历史。临时 `ref` 通过 `$alias` 关联本批次新增节点，后续使用正式 ID。当前 SQL workflow 的 `add_task` 用 procedure/episode 选择已检索模板，或使用 table 选择已检索表候选，程序合并同表已返回的字段片段；不要求 semantic_refs，旧字段仅保留兼容；SQL、结果与任务状态均由运行时维护。校验整批通过后才应用，SQL workflow 仍要求 Verifier 确认答案前存在可执行任务。成功操作记录于 `planner_operations_applied` trace。

重规划 user message 为 YAML（context、question、itt、review.can_answer）；ITT 使用具名字段及 children 嵌套展示 G/E/T 层级，保留拓扑、目标及所有 T 的状态、SQL/证据。Verifier 同样使用 YAML 和这一嵌套 ITT 视图。内部完整快照格式和 SQLite 版本检查保留，execution_result 支持结构化对象及历史字符串。

可用历史请求与快照做真实模型协议对照（不执行 SQL、不评估答案正确率）：

```bash
uv run python -m scripts.check_planner_operations \
  --case <历史逐题JSON> --requests <对应LLM请求JSONL> \
  --baseline 47f0d29 --live --output runtime/planner_operations_check
```

比较同一背景、问题和重规划前状态下，旧版完整树与新版操作协议的校验结果、输入/输出 token、耗时与重试；每项最多两次请求。默认关闭 `--live` 时只准备输入统计。输出为 `report.json` 和真实请求记录 `requests.jsonl`。小样本协议对照不代表端到端调查准确率提升。


## Fancybag + 两类 LTM 全量并行与续跑

```bash
FANCYBAG_MODEL=deepseek-v4.1-flash \
.venv/bin/python scripts/run_procedural_workflow_fancybag.py \
  --all --workers 10 --max-steps 25 \
  --output runtime/procedural_workflow_fancybag/deepseek_all
```

从项目根目录执行，读取 `docker/excytin-mysql/.env`，需要对应事件 MySQL 容器运行。当前测试集共 589 题；默认启用 Procedural、Episodic 和 Semantic Memory。每线程处理一道题，客户端、数据库连接及 workflow 状态独立。没有启动全量运行时也可用 `--cases incident_134-q0036` 指定单题。

同一命令、同一输出目录再次执行即续跑：仅跳过已正常提交答案的 `completed` 题目，失败、运行中、缺失或损坏的结果重新运行。答案评分不正确仍算运行完成，不会反复重试到正确；本脚本保留原来的确定性评分，未启用 LLM 裁判。新代码或模型在旧目录续跑会提示配置差异；独立对照实验应使用新输出目录。

进度条显示总进度、已跳过、本次完成、失败、运行中与耗时。每题完成后立即原子保存 checkpoint，单题异常不终止其他题目。Ctrl+C 停止派发新题，等待正在进行的请求返回后落盘。一个输出目录同时只允许一个批量进程。

- `summary.json`：当前选择题目的最新汇总。
- `progress.json`：本轮进度计数。
- `<case_id>/summary.json`：题目最新状态，续跑依据。
- `<case_id>/attempts/<attempt_id>/`：每次尝试的完整调用、workflow 记录与摘要；失败重跑不覆盖历史。
- `manifests/`：每次启动的模型、参数和源码指纹。

`--max-steps 25` 与本项目 baseline 批量入口默认预算一致，是每题最多 25 个 workflow 动作（SQL 查询和提交答案），不是最多 10 次模型调用。程序退出码：0 表示本轮无运行失败，1 表示存在失败题，130 表示中断。
