# ExCyTIn-Bench Workflow

这个目录提供一个与通用后台 workflow 分离的同步测评流程。它严格遵守
ExCyTIn-Bench 的外部动作协议：Workflow 生成一条 SQL，SQL 由
`ExcytinEnv.step()` 执行，Workflow 在下一轮收到 observation 后才进行
Reviewer 审查和 Planner 重规划。

```text
initial observation
  -> PM retrieval -> SM retrieval -> Planner full TTT
  -> L3 -> SM + EM retrieval -> Executor one read-only SQL
  -> ExcytinEnv.step(SQL)
  -> observation -> Reviewer (SM only)
  -> Review + PM + SM -> Planner updated full TTT
  -> next L3 / final answer -> ExcytinEnv.step(submit=True)
```

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
- Executor 首次查询只收到 Semantic Memory；仅当同一 L3 的 SQL 返回数据库 error
  或空结果时，才按失败现象检索并注入训练集脱敏 Episodic Memory。
- Planner 只收到 Procedural Memory 与 Semantic Memory。
- Reviewer 只收到 Semantic Memory。
- Reviewer 会先对照原始问题与真实 SQL 证据；答案实体或关系已明确且无冲突时返回
  `ready_to_submit`，Workflow 跳过 Planner 重规划并直接提交。
- SQL 必须是单条、MySQL 可解析的只读语句。
- 每题默认最多 25 个 benchmark action；Workflow 会预留最后一个 action 用于提交最终答案，因此成功提交前最多执行 24 条 SQL。
- Executor 针对同一个 L3 的内部查询循环最多执行 3 条 SQL。首次查询不使用 EM；出现 SQL error 或空结果后才使用 EM 修复。无论失败类型如何交替，达到 3 次都会交回 Reviewer/Planner。
- SQL error 和空结果各自最多允许两次 EM 辅助修复，但同时受上述每个 L3 总计 3 次的硬上限约束。
- 单次 LLM 请求默认 120 秒超时且不自动重试，可通过
  `DEEPSEEK_REQUEST_TIMEOUT_SECONDS` 和 `DEEPSEEK_MAX_RETRIES` 调整；超时会作为该题失败记录，而不是无限停在 `running`。

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
