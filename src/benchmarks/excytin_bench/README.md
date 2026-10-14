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
  -> Review + PM -> Planner full ITT list -> validated internal snapshot
  -> next T / candidate answer -> Verifier -> confirmed answer / Planner follow-up
  -> ExcytinEnv.step(submit=True)
```

Verifier 是 SQL workflow 内同步调用的独立 LLM 角色，不另起轮询进程。所有答案提交路径（Reviewer 建议、Planner 完成、阻塞及预算退出）均经过核验。它只接收问题、G 目标、候选答案和实际执行记录，不检索记忆；确认必须覆盖全部 G，并说明所求关系、引用成功非空结果中的真实 execution_id 与 excerpt_id；程序回填逐字原文。格式/证据校验失败不会放行。初审通过后再进行一次独立反证审查，不传递前一审查的推理；明确的括号缩写目标还要求引用中出现独立术语或对应全称，不能只出现在脚本名的子串里。该保守规则可能要求额外取证。输入按问题和候选选择实际结果的有重叠片段，并标明并非完整结果；片段不足时拒绝确认。

验证拒绝且仍可调查时，程序撤销 G/F 的提前完成状态，保留 T 历史并交给 Planner 新建验证任务；同一证据状态最多两次纠偏，防止没有新查询的重规划循环。预算耗尽或已无可行方向时，只提交“无法确认答案”和缺口，不把未验证候选作为事实。`verifications` 与 `answer_verified` trace 保存核验记录。该机制仍依赖模型判断关系充分性，不是形式化正确性证明。

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
- Reviewer 不检索 SM/PM/EM。仅接收 context/question、精简 ITT（id/objective/status/children）、执行记录及当前任务 ID；每条 SQL 和结果只发送一次，保留证据 ID、错误和截断信息。
- Reviewer 模型仅输出 `summary`（最多 80 字符）和 `suggestion`（最多 60 字符），程序严格校验字段与长度；超限按现有有限格式重试处理，不截断证据。`suggestion` 为“提交答案”或“停止调查”时由程序映射终止决策，其余为继续调查建议；存储层兼容旧审核字段，Planner 输入只传两字段。
- Reviewer 会先对照原始问题与真实 SQL 证据；答案实体或关系已明确且无冲突时返回
  `ready_to_submit`，Workflow 跳过 Planner 重规划并直接提交。
- SQL 必须是单条、MySQL 可解析的只读语句。
- 每题默认最多 25 个 benchmark action；Workflow 会预留最后一个 action 用于提交最终答案，因此成功提交前最多执行 24 条 SQL。
- Executor 针对同一个 L3 的内部查询循环最多执行 3 条 SQL。SQL error 时用失败 SQL 和错误精确检索 EM；空结果按正常返回回写 T 并直接交给 Reviewer，不在 Executor 内重试。SQL 报错达到次数上限后交回 Reviewer/Planner。
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
