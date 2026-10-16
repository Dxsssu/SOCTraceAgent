# Episodic Memory v2：失败恢复

EM 只保存数据库报错的背景、报错情况和修复方法。旧版 episode/attempt/NEXT/REPAIRED_BY 记忆已移除，首次查询和空结果不加载 EM。

## 三字段条目

每条条目严格只有三个字符串字段，来源和统计不混入条目：

```json
{
  "background": "在 MySQL 的 EmailEvents 表中读取发件人字段，使用 Sender 时出现字段错误。",
  "error": "ProgrammingError: Unknown column 'Sender' in 'field list'",
  "repair": "按问题区分 SenderFromAddress、SenderMailFromAddress 和 SenderDisplayName；保留消息标识、收件人和时间范围，使用当前案件值构造只读查询并验证。"
}
```

`background` 用表、字段及查询位置区分同名错误；`error` 是独立的向量化输入；`repair` 提供适用条件、合法字段和操作指导。运行时由 Executor 结合实际 schema 与当前证据生成具体 SQL，不复制历史实体值。

## 来源与筛选

构建使用 237 条已对齐训练轨迹的 1,605 次 SQL 执行记录，筛出全部 81 次数据库报错。以消息中的首条真实任务对齐训练题，拒绝错位的顶层 key；训练答案只参与脱敏，不进入记忆。

目前生成 **21 条记忆，覆盖 70 次有依据的错误，11 次未收录**。没有把 358 次空结果作为报错经验，也没有把错误后的 DESCRIBE 成功当作修复完成。

`repair_rules.py` 保存经实际 SM 核对的场景化指导。构建器要求同一轨迹中后续出现使用对应修复表及字段的、无未解析字段的已执行业务查询；DESCRIBE/SHOW 和无关查询不作支持。相同背景和错误归并，来源保存在独立 `build_audit.json`。该审计逐一列出 81 次错误的去向、失败模板、支持查询、结果状态或排除原因。

修复指导包含人工对字段含义与必要约束的审核；后续执行只是支持证据，不能证明语义等价、因果效果或最终答案正确。执行后为空仍可证明数据库错误不再发生，但不能证明任务完成。未能可靠确定方法的错误不生成通用猜测条目。

构建输入不包含测试运行历史。此次清点 `results/` 和 `runtime/excytin_bench/` 的 2,662 份逐题结果均属测试题；应用 SQLite 中事件和执行记录均为 0，因此不进入训练来源。清点详情在 `runtime/episodic_memory/history_inventory.json`，该本地文件不作为重建依赖。

## 检索与运行时

- 加载快照/图数据时预构建 `(错误类型, 去除别名后的报错对象)` 倒排索引及 error 的本地 TF-IDF 向量。运行时先精确召回，不调用 LLM 或 embedding API。
- 解析真实失败 SQL 的 AST 和作用域，将 `s.Description` 中的 `s` 解析到具体表，以 background 中的失败表作硬过滤；忽略字符串中的伪 FROM/JOIN。无法解析的表别名或跨表同名错误存在歧义时返回空。
- 同一表和报错对象内，优先匹配报错位置（field list / where clause），再以 error 的余弦相似度排序；相同 repair 去重。泛化错误需满足相似度门槛 0.35，未知对象不会借用其他字段的修复。
- Executor 默认只取 1 条；首次查询只接收 T；空结果按正常返回直接交给 Reviewer，不触发重试或检索。只有实际 SQL error 才通过独立 EM 接口检索，并在当前 T 的输入旁附加 `error_recovery: {failed_sql, error, episodic_memory}`。这部分不会写入 ITT 或 PM。
- 按 background 核对适用条件，用 repair 和当前 T 的实体/范围生成修复 SQL，执行验证后程序回写 execution_code/execution_result。无命中时根据真实错误探索结构，禁止猜测替代字段。每个 T 最多 3 次 SQL，SQL error 最多 2 次修复，耗尽后交回 Reviewer/Planner。
- `executor_error_recovery` 日志记录匹配数量、条目和来源执行 ID。Planner/Reviewer 不接收 EM；普通本地 Splunk Executor 不接入此 MySQL 恢复链路。
- 当前库覆盖的已知错误召回可以回放验证，不代表未知错误必然可修复，也不代表修复后的查询语义或答案一定正确。

## 构建与导入

```bash
.venv/bin/python -m src.memory.longterm_memory.episodic_memory.excytin_bench.build_knowledge
.venv/bin/python -m src.memory.longterm_memory.episodic_memory.excytin_bench.import_neo4j --dry-run
.venv/bin/python -m src.memory.longterm_memory.episodic_memory.excytin_bench.import_neo4j
```

导入时校验本地 SM L1/L2 的 schema 哈希，无需先导入 SM 图。新图为：

```text
Environment → EpisodeCatalog → FailureRecovery(background, error, repair)
EpisodeCatalog.semantic_schema_hash 记录本地 L1/L2 的 schema 版本
```

节点另带图存储所需的 ID、归属与顺序，Agent 条目仍只含三字段。一次事务删除旧 `excytin-episodes-v1` 与当前 v2 catalog 所属节点并写入新图，失败会回滚，既有 SM/PM 节点不删除。JSON 与 Neo4j 后端使用相同的本地向量检索。

PM 的离线挖掘现直接使用 `training_history.py` 解析训练语料，不读取旧 EM 的 episodes。该模块是内部训练解析器，不发布旧记忆文件。EM 重建后重新构建、导入 PM，更新依赖 catalog 和快照哈希；65 条 PM 内容保持不变。

```bash
.venv/bin/python -m src.memory.longterm_memory.procedural_memory.excytin_bench.build_knowledge
.venv/bin/python -m src.memory.longterm_memory.procedural_memory.excytin_bench.import_neo4j
```

## 验证

```bash
SOCAGENT_LTM_BACKEND=snapshot uv run --with pytest python -m pytest \
  tests/test_excytin_episodic_memory.py tests/test_workflow_memory_policy.py \
  tests/test_excytin_benchmark_workflow.py tests/test_excytin_procedural_memory.py -q
```

测试覆盖三字段结构、训练来源与重建一致性、所有错误去向、相同错误的表消歧、无匹配返回空、首次/空结果不注入以及错误修复触发。真实案件的修复质量仍需实际数据库与模型评估。
