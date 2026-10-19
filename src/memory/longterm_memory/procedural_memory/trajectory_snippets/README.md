# 从完整训练轨迹重建的 Procedural Memory

本目录替代上一版“先定义 40 个意图、再扫描训练 SQL”的构建方法。输入为训练集中的 **237 条完整轨迹**；不使用测试集，不预设片段总数或意图目录。当前 SQL workflow 默认加载本目录，由 `procedural_memory_enabled` 控制；旧程序卡片系统仍关闭。

## 本次构建结果

237 条轨迹的 3,921 条原始消息、1,605 次 SQL 已完整覆盖，并逐条留下协作审查记录。结构去重及验证后保留 **419 条候选片段：320 条单步、99 条多步，最长 6 步**；共享 320 个 SQL 模板，保留 609 个调查意图变体。

MySQL 只读全支持回放：**471/471 次查询、125/125 条多步训练支持通过**，后者包含 276 个查询步骤。另有 13 条多步候选因上游结果可能截断而未纳入目录；1 条嵌套查询暂不编译、52 条未确认值流关系均单独留档。376 条参考调查路线、429 条恢复/失败经验不计入上述 419 条片段，也不作为自动执行计划。

详见 [构建统计](excytin/summary.json)、[覆盖台账](excytin/reading_ledger.json)、[全支持回放](excytin/all_support_validation.json) 和 [四个实际示例](examples.md)。针对 SQL 绑定、结果解析、多步依赖及检索的 18 项单元测试通过。

## 阅读与证据

每条轨迹的原始 `messages`（原系统说明、用户题目、思考、SQL、数据库观察和最终提交）完整发送给 Fancybag，模型逐次解释调查作用、转向原因和结论局限。重复系统说明也没有省略。`read_message_indices`、逐 SQL 说明及源文件哈希由程序校验；它们证明输入与覆盖完整，不能单独证明模型理解正确。

协作审查进一步逐条检查结构化阅读记录，对可疑判断回查原 SQL、观察或 Schema，并用独立 notes 记录修正。原模型输出保留不改。原数据已经截断的观察无法恢复；仅使用其中完整可解析的行，不能凭最终回答补齐证据。

原始输入、调用记录、逐条阅读、审查 notes 和离线回放参数保存在 `runtime/procedural_trajectory_review/`，不进入 Agent 检索上下文。

## 三种产物

| 产物 | 内容 | 用途 |
| --- | --- | --- |
| `catalog.json` | 成功 SQL 的原子片段；经字段和值传递核验的多步 DAG | Planner 查找候选动作、Executor 准备参数化 SQL |
| `branch_recipes.json` | 空结果后换表、结构确认、缩小/扩大条件等调查路线 | 仅供 Planner 参考，不能机械重放 |
| `recovery_rules.json` | 失败条件、实际调整及恢复经验 | 独立的 Episodic Memory 候选，不混作成功 SQL 模板 |

失败后直接提交、没有再执行查询的记录明确保留 `after=null`、`after_status="not_attempted"`、`unrecovered=true`，不能算作恢复成功。

阅读台账逐次覆盖所有 SQL。Schema 探查、空结果、错误和暂不支持的 SQL 均有明确去向；零 SQL 轨迹也保留，不凭题目答案编造模板。成功查询的片段只表示可复用查询动作，不表示该轨迹最终答案正确。

## 片段结构

每条片段包含 `trigger_entity`、`intent_variants`、`required_inputs`、`steps`、`bindings`、`caution`、训练支持和验证等级。

- `required_inputs` 列出**所有步骤**仍需要外部提供的参数，并注明步骤、表、字段及实体概念。后一步由前一步提供的值不列为外部输入。
- `steps` 引用独立 SQL 模板并提供 Planner 动作建议和输出字段。模板保留原 SQL 的过滤条件、投影、连接、排序与聚合。`SELECT *` 按真实数据库 Schema 展开，不能让模型猜字段或裁掉关键输出。
- `bindings` 明确前一步输出列、后一步过滤列和转换方式。目前支持等值/IN 值传递、单值集合、可验证的 JSON 路径及精确观察到的时间边界。时间窗口另一端仍需外部给定，不能擅自推算。表结构中的字段名不能当成事件实体数据流。
- `intent_variants` 保留共享 SQL 结构的不同调查意图与各自的动作建议、注意事项。只合并 SQL 结构相同且依赖关系相同的片段；不因为两段文字相似就合并字段角色不同的过程。

例如“URL → 邮件 ID → 发件人”表示第二步的邮件 ID 必须来自第一步真实结果。`InternetMessageId`、`NetworkMessageId`、设备各类 ID 不因名字相似而互换。出现同值也只验证了已观察到的值传递，仍需核对当前任务的字段语义。

模型在一条轨迹中拆成 A→B→C 和 C→D 的片段，会按已验证的边重建完整四步 DAG，同时保留较短片段。分叉查询也保留，例如同一邮件记录分别向发件人查询与 URL 查询提供不同标识。中间失败或空结果不提供新实体；经复核补充的非相邻关联有独立 notes 和相同的字段、原文及值验证。

## 检索与执行边界

`TrajectorySnippetStore.retrieve(entity, intent, kind=None)` 提供保守的实体概念与意图别名精确匹配；这是可审计的基础接口，尚不是自然语言向量检索器。意图无匹配时返回 `route="semantic"`。候选命中时返回对应意图的建议，而不是强行采用同模板第一个训练场景的描述。

目录状态为 `candidate`。通过训练库回放不足以证明新事件适用，不代表适用于任意当前事件。workflow 适配层显式使用 `allow_candidates=True`，在当前真实 Schema 下检索候选，由 Planner 确认适用性并选步骤；此接入没有把训练验证等级改成新事件调查正确性保证。

`prepare_step` 接收当前真实 Schema、外部输入、上游真实结果与显式候选选择，返回 SQL 和独立参数数组，不执行数据库操作。它拒绝：

- 缺失、失败或截断的上游结果；
- 多候选时未经显式选择就取第一行；
- 选择不在上游结果中的值、覆盖派生参数；
- 同字段多个参数而绑定目标不明确、Schema 不匹配；
- 非只读 SQL、参数类型或列表大小不符。

数据库值不会通过字符串拼接进入 SQL。原始业务字面量仅存离线回放记录；模板中的 `LIKE` 参数仍须由调用方明确给出模式，不能默默补通配符改变调查范围。模板最多返回 100 行，保留原 SQL 更小的 LIMIT；达到上限视为可能截断，不能据此做“只有一个/不存在”等完整性结论。

## 验证与限制

构建时先校验真实表字段、SQL 只读性及原观察中的字段和值传递，再在 MySQL 只读事务中执行 `EXPLAIN` 和查询。多步回放从本次前一步返回的行中绑定后一步；历史目标值只用作显式选择，并验证它确实存在于这些行中。此验证不代表 Planner 能自主选对所有候选。

全支持回放会另行覆盖每条成功查询的训练绑定，以及保留多步片段的各条训练支持。空结果、错误和缺少字段语义/转换证据的链不进入可执行目录，但保留完整审计及参考路线。尚不支持的复杂子查询、变换等记录具体原因，不自动重写为另一条 SQL。

`unsupported_queries.json` 单独保留暂不能编译的成功查询及参数化结构；`unresolved_relations.json` 记录未通过值流校验的关系；`rejected_replays.json` 保存链执行拒绝原因。这些记录都不计入验证通过的目录。全量覆盖统计见 `reading_ledger.json`，最终数量与回放结果见 `excytin/summary.json`。

训练轨迹中的问题也随片段保留：全局关键词命中可能缺少主机关联；邮件包含 URL 不证明用户点击；文件创建不证明执行；账号对象 ID 不自动等于另一数据源的用户 ID；公司名称不证明程序可信；`IS NOT NULL` 不排除空字符串；`OR` 与 `AND` 的组合可能让时间范围仅约束一部分条件。

## 复现

```bash
# 单次请求含一条完整轨迹，可按互不重叠的范围并行；成功记录自动跳过。
FANCYBAG_MODEL=deepseek-v4.1-flash uv run python scripts/review_procedural_trajectories.py --start-from 1 --stop-after 237

# 默认每次填充到下一个 10 条里程碑；--all 处理所有待验证候选，无总数上限。
uv run python scripts/build_reviewed_procedural_memory.py --all
uv run python scripts/build_reviewed_procedural_memory.py --recheck
uv run python scripts/audit_reviewed_procedural_memory.py
uv run python scripts/validate_reviewed_procedural_memory.py
uv run python scripts/export_reviewed_procedural_memory.py
uv run python -m pytest -q tests/test_trajectory_snippets.py
```

需要已配置的 Fancybag 接口以及本地 ExCyTIn MySQL 数据。所有构建只用训练集；API 故障、输出格式重试和未完成轨迹均保留记录。完整性以阅读台账 `missing_indices=[]`、237 条轨迹和 1,605 次 SQL 覆盖为准。

## 当前 workflow 接入

`session.py` 连接已验证片段与 `ExcytinBenchWorkflow`：

1. Planner 使用现有 `semantic_query` 描述已知概念、目标概念与意图；系统先按实体概念/角色、意图关键词和输出概念召回最多四个 PM 候选，并验证目标 Schema。不会给 Planner 注入完整目录。
2. Planner 选择 `add_task.procedure: {id, step, inputs}`，片段建议与 SQL 模板、参数、注意事项一起传给 Executor；省去该任务的 Semantic 选表。候选不适用时重复同一 query 转 Semantic，未命中直接自动回退。直接选择普通 table 任务也视为拒绝模板，程序补做最近请求的 Semantic 检索后校验该表。
3. 首步 T 的 ID 作为 run，后续步骤填写 `run` 并消费同一次调查的原始返回值；不使用 Reviewer 推测、历史训练值或无依据的实体替换。多候选需明确 selections，失败/空/截断拒绝继续绑定。
4. 为兼容只接受 SQL 字符串的 Benchmark，按类型校验后的参数由 SQL AST 字面量编码，禁止字符串替换。Executor 仍只返回 sql，运行时比较 AST，拒绝改写片段条件、表或参数。模板执行失败交回 Planner 选路。

起点匹配与目标概念召回仅提供候选，适用性仍由 Planner 判断，不保证语义检索无误。`active_runs` 提供未完成多步计划；相互依赖的 T 按观察结果逐个建立。

真实接口评测：`uv run python scripts/run_procedural_workflow_fancybag.py --max-steps 10`；原始调用、SQL、PM/SM 选择及答案保存在 `runtime/procedural_workflow_fancybag/`。

`excytin/summary.json` 是离线构建阶段的历史快照，其中 `workflow_enabled=false` 不描述当前运行时开关；当前启用状态以 `get_logging().planner_pm_enabled` 为准。

Planner 同时收到 Semantic 概念词表和 PM 的起点词表，后者包含 `Title`、`AlertName`、`Alert_ID` 等仅由片段目录提供的入口。不会把完整片段或所有 SQL 注入提示词。普通 table 选择若位于相同 Semantic 检索的后页，适配层会在总检索预算内自动翻页并缓存；未收录的表仍无法作为 Semantic 任务通过，不把排名缺页和字典缺失混为一谈。
