# ExCyTIn-Bench Workflow

这个目录提供一个与通用后台 workflow 分离的同步测评流程。它严格遵守
ExCyTIn-Bench 的外部动作协议：Workflow 生成一条 SQL，SQL 由
`ExcytinEnv.step()` 执行，Workflow 在下一轮收到 observation 后才进行
Reviewer 审查和 Planner 重规划。

```text
initial observation
  -> Agent shortlists active PM by question + goals + inputs -> load applicable PM (including semantic_refs) -> Planner G/F/T ITT list
  -> T node only -> Executor one read-only SQL
  -> ExcytinEnv.step(SQL)
  -> observation -> program writes SQL/result/status into T -> Reviewer (question + task outline + execution evidence)
  -> Verifier (context + question + ITT + reviewer)
  -> confirmed: submit answer / unconfirmed: Planner update -> next T
  -> ExcytinEnv.step(submit=True)
```

Verifier 在每轮 Reviewer 后同步调用，接收 `context + question + itt + reviewer`。ITT 包含每个 T 的实际 SQL 和执行结果；reviewer 仅含本轮 summary/suggestion。不传候选答案、独立 executions 列表或证据片段目录，不检索长期记忆。

Verifier 输出 `can_answer: "yes"` 或 `can_answer: "no"`，仅 yes 时附加 `answer`。能回答时直接生成最终答案，程序退出循环提交，不再调用最终答案整理器或反证审查。不能回答时，把 Reviewer 总结、建议以及 Verifier 的未确认状态交给 Planner 继续规划；Reviewer 的提交/停止建议不直接终止调查，Planner 的完成状态也不能替代 Verifier 确认。预算耗尽而仍未确认时，程序提交“无法确认答案：调查结束，尚无充分证据。”。Verifier 未确认时，Planner 的重规划必须包含可执行的 todo T；G/E 的推导状态不再触发提前提交。缺少可执行任务按现有有限校验重试要求补充，耗尽后记录规划失败，不递归空转或提交。格式校验失败按有限重试处理，耗尽后显式失败。核验记录保存在 `verifications` 和 `answer_verified` trace 中；答案是否有充分证据由模型判断。

PM 成本控制：v5 查询模板经离线归纳和 schema 校验，在线只调用一次选择代理，输入上限 14,000 UTF-8 字节、输出上限 192 token、关闭 thinking。最多一个模板，未命中转 SM；缓存正负结果。Planner 写 template_id，程序向当前 T 装载模板，Executor 填参，避免反复生成已有 SQL。

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
- Executor 仅收到当前 T 的 id/objective/status/semantic_refs/execution_code/execution_result，正常查询不检索记忆，可携带程序装载的 query_template；SQL error 后仅检索最多一条 EM 用于恢复，不构建 SM/PM，不传原始问题、全树或其他 T 的历史。Planner 负责将必要实体、范围和输出要求写入自包含的 T.objective。
- Planner 从 active 查询模板中选择可用操作（一次选择调用），T.template_id 引用真实模板；程序附加 inputs/sql_template/returns/semantic_refs。Executor 收到 query_template 时只返回 template_id 和 bindings，程序安全填参；无模板或 EM 修复时输出 SQL。
- Planner 仅收到 context/question 和适用的 Procedural Memory（包含 semantic_refs），PM 未命中时以一次小代理调用从精简 SM 目录选择最多两张表，程序加载真实字段名；输入上限 6,000 UTF-8 字节、输出上限 192 token、关闭 thinking、不重试，相同任务和 SM 库缓存结果。PM 部分仍为空。runtime_info 仅供运行时使用，不传给模型或 PM 选择助手。
- Reviewer 不检索 SM/PM/EM。仅接收 context/question、精简 ITT（id/objective/status/children）和本轮 executor_result；不传累计执行记录或顶层当前任务 ID。
- Reviewer 模型仅输出 `summary`（最多 80 字符）和 `suggestion`（最多 60 字符），程序严格校验字段与长度；超限按现有有限格式重试处理，不截断证据。`suggestion` 仅作为 Verifier 和 Planner 的参考；存储层兼容旧审核字段，Planner 输入只传两字段。
- 每轮 Reviewer 后调用 Verifier；仅 Verifier 确认可以作答时跳过 Planner 并提交其 answer。
- SQL 必须是单条、MySQL 可解析的只读语句。
- 每题默认最多 25 个 benchmark action；Workflow 会预留最后一个 action 用于提交最终答案，因此成功提交前最多执行 24 条 SQL。
- Executor 针对同一个 L3 的内部查询循环最多执行 2 条 SQL。SQL error 时用失败 SQL 和错误精确检索 EM；空结果按正常返回回写 T 并直接交给 Reviewer，不在 Executor 内重试。SQL 报错达到次数上限后交回 Reviewer/Planner。
- 仅 SQL error 允许一次 EM 检索和一次辅助修复，同时受每个 T 总计 2 次的硬上限约束；已移除 max_empty_repairs 参数。空结果不表示问题解决，Reviewer 可建议 Planner 新建调查任务。
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

Benchmark 与通用后台共用 ITT YAML 列表协议：支持多个 G，三级 G/F/T，todo/done/failed 状态，以及任务 semantic_refs/execution_code/execution_result。G.objective 从 question 提炼，一个独立问题对应一个 G；初始 F 由 LLM 联合 context/question 生成。T.semantic_refs 保存相关 PM 的表和字段，随任务传给 Executor，无相关知识时为 []。初始化与更新都输出完整列表，运行时选择首个可执行任务并管理版本与快照。

当前重规划协议：Planner 首轮保留 YAML 输出示例；后续轮次使用无示例的重规划提示词，user message 为 YAML（context、question、itt、review），ITT 保持嵌套列表结构。Executor 每次只调用模型一次，不进行格式重试或本地 SQL 校验重试；解析与只读校验仍保留，失败直接记录错误。只有实际数据库 SQL error 触发一次修复执行。
