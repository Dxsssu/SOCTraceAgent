# ExCyTIn-Bench Workflow

这个目录提供一个与通用后台 workflow 分离的同步测评流程。它严格遵守
ExCyTIn-Bench 的外部动作协议：Workflow 生成一条 SQL，SQL 由
`ExcytinEnv.step()` 执行，Workflow 在下一轮收到 observation 后才进行
Reviewer 审查和 Planner 重规划。

```text
initial observation
  -> Agent shortlists active PM by question + goals + inputs -> load applicable PM (including semantic_refs) -> Planner operations -> runtime G/E/T ITT
  -> {id, objective} -> Executor one read-only SQL -> program writes execution_code
  -> ExcytinEnv.step(SQL)
  -> observation -> Reviewer (current task + current execution only) -> program writes structured execution_result
  -> Verifier (context + question + nested ITT)
  -> confirmed: submit answer / unconfirmed: Planner update -> next T
  -> ExcytinEnv.step(submit=True)
```

Verifier 在每轮 Reviewer 后同步调用，接收 `context + question + itt`。ITT 为通过 children 连接的 G/E/T 三层嵌套树，包含每个 T 的实际 SQL 和结构化证据（summary/facts/limitations/source），不重复传最新总结。不传独立 executions 列表，不检索长期记忆。

Verifier 输出 `can_answer: "yes"` 或 `can_answer: "no"`，仅 yes 时附加 `answer`。能回答时直接生成最终答案，程序退出循环提交，不再调用最终答案整理器或反证审查。不能回答时，把包含结构化证据的 ITT 及 Verifier 的未确认状态交给 Planner 继续规划；Reviewer 的提交/停止建议不直接终止调查，Planner 的完成状态也不能替代 Verifier 确认。预算耗尽而仍未确认时，程序提交“无法确认答案：调查结束，尚无充分证据。”。Verifier 未确认时，Planner 的重规划必须包含可执行的 todo T；G/E 的推导状态不再触发提前提交。缺少可执行任务按现有有限校验重试要求补充，耗尽后记录规划失败，不递归空转或提交。格式校验失败按有限重试处理，耗尽后显式失败。核验记录保存在 `verifications` 和 `answer_verified` trace 中；答案是否有充分证据由模型判断。

PM 成本控制：v5 查询模板经离线归纳和 schema 校验，在线只调用一次选择代理，输入上限 14,000 UTF-8 字节、输出上限 192 token、关闭 thinking。最多一个模板，未命中转 SM；缓存正负结果。模板仍可供 Planner 参考，历史 template_id 元数据保留；当前 Executor 统一接收 id/objective 并生成 SQL，不再输出模板参数。

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
- Executor 仅收到当前 T 的 id/objective。Planner 必须在 objective 中写齐实体、参数和查询范围。SQL error 的失败 SQL 和错误临时附加在 objective 中，原任务不变；程序校验 SQL 并在执行前写入 execution_code。
- 历史 PM 模板引用和元数据保持可读取；当前 Executor 不接收 query_template，不再走 template_id/bindings 填参协议。
- Planner 仅收到 context/question 和适用的 Procedural Memory（包含 semantic_refs），PM 未命中时以一次小代理调用从精简 SM 目录选择最多两张表，程序加载真实字段名；输入上限 6,000 UTF-8 字节、输出上限 192 token、关闭 thinking、不重试，相同任务和 SM 库缓存结果。PM 部分仍为空。runtime_info 仅供运行时使用，不传给模型或 PM 选择助手。
- Reviewer 不检索 SM/PM/EM。仅接收当前 task（id/objective）和本次 executor_result，不传全树、全局背景/问题或累计执行记录。
- Reviewer 输出 summary（最多 240 字符）、facts（记录对象列表，保留关联键、主机、PID/GUID、时间和路径等原值）、limitations（缺失、歧义或省略范围）。程序补充 source，整体写入 T.execution_result；原始结果保留在 executions。错误/空结果的 facts 强制为空；截断强制补充限制。
- 每轮 Reviewer 后调用 Verifier；仅 Verifier 确认可以作答时跳过 Planner 并提交其 answer。
- SQL 必须是单条、MySQL 可解析的只读语句。
- 每题默认最多 25 个 benchmark action；Workflow 会预留最后一个 action 用于提交最终答案，因此成功提交前最多执行 24 条 SQL。
- Executor 针对同一个 L3 的内部查询循环最多执行 2 条 SQL。SQL error 时用失败 SQL 和错误精确检索 EM；空结果按正常返回回写 T 并直接交给 Reviewer，不在 Executor 内重试。SQL 报错达到次数上限后交回 Reviewer/Planner。
- 仅 SQL error 允许一次 EM 检索和一次辅助修复，同时受每个 T 总计 2 次的硬上限约束；已移除 max_empty_repairs 参数。空结果不表示问题解决，Planner 可根据结构化证据新增调查任务。
- 单次 LLM 请求默认 120 秒超时且不自动重试，可通过
  `PARATERA_REQUEST_TIMEOUT_SECONDS` 和 `PARATERA_MAX_RETRIES` 调整；超时会作为该题失败记录，而不是无限停在 `running`。

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

export SOCAGENT_LTM_BACKEND=neo4j

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


## PM 真实 Workflow 开关对照

```bash
.venv/bin/python -m src.benchmarks.excytin_bench.pm_workflow_check \
  --model DeepSeek-V4.1-Flash \
  --cases incident_39-q0088 incident_55-q0022 incident_5-q0001 \
  --max-steps 12 \
  --output results/pm_workflow_check/new_run
.venv/bin/python -m src.benchmarks.excytin_bench.pm_workflow_report \
  results/pm_workflow_check/new_run
```

使用生产 ExcytinBenchWorkflow 的 Planner/Executor/Reviewer、Neo4j 长期记忆和真实 MySQL。两组使用相同题目、模型和动作预算，baseline 仅关闭 PM 选择与加载，Reviewer 不使用长期记忆，Executor 仅在 SQL error 后使用 EM。默认三题各跑两次，自动读取本地数据库配置。已完成的题目/模式会跳过；重新独立评估请换输出目录。

`calls/` 保存单次模板选择及角色请求原文（历史 v4 报告仍包含前提核对），`cases/` 保存真实 SQL、结果和答案评估；顶层保留知识库快照及 manifest。`analysis.json` 区分检索、实际注入和 SQL 引用重合，并汇总耗时、tokens 和费用。SQL 引用重合不证明因果依赖；少量诊断题和单次运行也不构成准确率提升证据。答案使用字符串归一化匹配，不计算论文 reward。

Benchmark 与通用后台共用 Planner 增量操作协议，定义于 `src/schema/planner_operations.py`。输出仅含 `operations` 列表，支持 `add_goal`（仅首轮）、`add_entity`、`add_task`、`select_task`；程序分配 G/E/T ID、构建完整树并保留历史。临时 `ref` 通过 `$alias` 关联本批次新增节点，后续使用正式 ID。`add_task` 可提供 semantic_refs 或 template_id；SQL、结果与任务状态均由运行时维护。校验整批通过后才应用，SQL workflow 仍要求 Verifier 确认答案前存在可执行任务。成功操作记录于 `planner_operations_applied` trace。

重规划 user message 为 YAML（context、question、itt、review.can_answer）；ITT 使用具名字段及 children 嵌套展示 G/E/T 层级，保留拓扑、目标及所有 T 的状态、SQL/证据。Verifier 同样使用 YAML 和这一嵌套 ITT 视图。内部完整快照格式和 SQLite 版本检查保留，execution_result 支持结构化对象及历史字符串。

可用历史请求与快照做真实模型协议对照（不执行 SQL、不评估答案正确率）：

```bash
uv run python -m scripts.check_planner_operations \
  --case <历史逐题JSON> --requests <对应LLM请求JSONL> \
  --baseline 47f0d29 --live --output runtime/planner_operations_check
```

比较同一背景、问题和重规划前状态下，旧版完整树与新版操作协议的校验结果、输入/输出 token、耗时与重试；每项最多两次请求。默认关闭 `--live` 时只准备输入统计。输出为 `report.json` 和真实请求记录 `requests.jsonl`。小样本协议对照不代表端到端调查准确率提升。
