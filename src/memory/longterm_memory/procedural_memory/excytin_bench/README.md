# Procedural Memory v5：可复用查询模板

PM 保存「已知输入 → 参数化 SQL → 输出字段」，不保存案件答案或调查故事。训练构建与在线使用分离：离线归纳、校验、回放，在线一次轻量选择；不再运行前提审核与独立复核两轮代理。

## 模板结构

每条包含 `id / name / status / purpose / inputs / sql_template / returns / semantic_refs`。
`id` 是规范化 SQL 的稳定摘要；`inputs` 说明参数对应字段、类型、比较操作及含义；`returns` 解释输出；`semantic_refs` 从 SQL AST 自动生成，不由模型猜测。

示例（实际邮件模板还包含其他邮件属性）：

```yaml
purpose: 根据邮件标识查询发件地址
inputs:
  p1: {field: NetworkMessageId, type: string, operator: EQ, description: 邮件标识}
sql_template: SELECT SenderFromAddress FROM EmailEvents WHERE NetworkMessageId = :p1 LIMIT 100
returns:
  SenderFromAddress: 邮件记录中的发件地址
```

## 构建依据与边界

- 仅使用与 train 题目对齐的 237 条训练轨迹、1,605 次查询尝试。
- 只从成功且有行返回的查询提取操作；排除无过滤查询、结构查询、失败查询。
- 将 WHERE 中的具体值替换为带类型的参数；IN 的长度归一为列表参数。保留 AND/OR、比较操作与排序，不能把不同条件关系混合。
- 相同过滤结构可以合并实际观测过的输出字段，显式投影优先，最多 12 个字段；SELECT * 使用通用表字段清单缩减输出。统一 LIMIT 100，不表示完整穷尽。
- 至少两个训练 episode 支持才能入库；至少两个 incident 支持才 active。同事件内重复模式作为 candidate。
- 当前生成 46 条模板，34 条 active、12 条 candidate。没有将 test 题目、值或答案写入模板。
- 当前覆盖单表过滤 SELECT。JOIN、子查询、聚合和计算投影暂不自动归纳，避免错误泛化。PM 没有覆盖的操作由 SM 和正常 SQL 路径处理。
- 来源支持、过滤原因和限制在 `build_audit.json`；不进入在线提示词。
- AST 验证参数、返回字段、只读 SELECT、真实表字段和行数上限。模板内不含案件常量。

重建及图导入：

```bash
uv run python -m src.memory.longterm_memory.procedural_memory.excytin_bench.build_knowledge
uv run python -m src.memory.longterm_memory.procedural_memory.excytin_bench.import_neo4j --dry-run
uv run python -m src.memory.longterm_memory.procedural_memory.excytin_bench.import_neo4j
```

v5 图目录是 `excytin-query-templates-v5`，结构为 `Environment → ProcedureCatalog → QueryTemplate`。导入只替换此 v5 目录，保留旧目录及 SM/EM。已有进程需重新加载 repository。

## 在线路径

1. 本地词项排序最多 32 个 active 模板，只发送名称、参数字段/类型/操作、返回字段。模型输入最多 14,000 UTF-8 字节，无完整 ITT、原始观察或全库 SQL。
2. 小代理一次返回最多一个模板 ID，无匹配返回空；输出上限 192 token，关闭 thinking，不进行格式重试。代码围栏、带 id 的目录回显在程序内归一化；模型生成的 SQL/参数不被信任。
3. Planner 看到选中模板，T 只写 `template_id`，并在 objective 中说明具体已知参数。没有参数则先安排获取参数的任务。
4. 程序按 ID 从已提供模板或旧任务快照装载 canonical 模板，附到该 T 的 metadata。ITT 对模型仍只展示 ID，不要求复制模板 SQL。
5. Executor 只接收当前 T。有模板时输出 `template_id + bindings`，程序用 SQL AST 安全替换字面量，不进行字符串拼接，不让模型重写字段。无模板则正常生成 SQL。
6. SQL 报错后仍只检索一次 EM、重试一次；修复允许输出新的 SQL。空结果正常进入 Reviewer。
7. PM 未命中时保留轻量 SM 兜底。PM 正/负结果与 SM 结果缓存。答案关系由 Verifier 核验，查询成功并不代表答案正确。

## 验证记录

`outputs/pm-v5-rebuild/sql_validation.json` 保存 46 条模板各两个训练样例的 MySQL 回放：92 次 EXPLAIN 和 92 次实际 SELECT 均成功且有返回。它证明这些样例上的可执行性，不证明所有未来参数或最终答案正确。

旧 v4 的 snapshot、构建逻辑及测试副本保留在 `outputs/pm-v5-rebuild/` 作为迁移记录；`legacy_retrieval.py` 仅供历史诊断报告解释旧三阶段输出，生产路径不调用它。
