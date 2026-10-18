# 基于证据推进的 Procedural Memory

当前状态：LTM 重建期间已移出 workflow。下文在线接入与启用方式属于 `dfb8646` 历史版本；当前仅保留卡片和离线工具，PM 对照实验暂停，`--modes baseline` 可运行无记忆基线。

本实现独立于旧 SQL 模板库。`definitions.yaml` 中每个模型可见过程只有 `name / inputs / steps / outputs / constraints` 五个字段；类型索引、表字段依赖和训练来源放在外围。

目前构建 70 条候选卡片，来源是 ExpeL `corrects.jsonl` 的成功轨迹。构建器以实际用户消息对齐训练问题，排除失败、空结果及系统提示词中的演示查询。每条卡片关联实际 SQL 和观察摘要哈希，并用 SM 字段字典检查依赖。新增输出字段、约束和参数泛化是设计，历史查询成功不等于这些变化已经得到回放验证。

## 检索机制

1. 从当前问题提取目标证据类型，从用户输入及成功任务的结构化 facts 提取可用输入类型。计划文本、失败观察和记忆建议不能解锁后续过程。
2. 从目标证据反向搜索过程依赖，最多三步；明确要求特定告警范围且尚无观察时，优先推荐 `scope_discovery` 核对告警关联，不能用同名实体替代范围。共享前置过程只呈现一次，优先使用精确标识，再比较路线长度和相关性。无直接路线时，可返回明确标注 `candidate_discovery` 的候选告警发现流程；该流程不宣称已经满足目标类型。
3. 每个过程单独标记输入类型已出现或还需前一步观察。UPN、UserId、AccountObjectId、SID、DeviceId、AadDeviceId 分别作为输入类型，不能因名字相似直接互换。类型匹配不证明具体实体符合目标，Planner 必须核对当前实体关系。
4. 最多提供三张卡片、8,000 UTF-8 字节。预算不足时不截断依赖或字段，返回无可用路线。
5. 检索不调用 LLM。不把案件常量、来源轨迹、答案或完整表字典放入提示词。

新卡片只接入 SQL workflow 的 Planner。Planner 继续输出 operations，并把当前步骤的 MySQL 方言、精确表字段及已知参数写入任务 objective。Executor 仍只接收 id/objective；Reviewer 和 Verifier 不直接读取 PM。

## 构建与启用

```bash
uv run python -m src.memory.longterm_memory.procedural_memory.excytin_bench.cards.build
```

可通过 `ExcytinBenchWorkflow(planner_pm_enabled=True)` 或进程环境 `SOCAGENT_PLANNER_PM_ENABLED=true` 启用。该开关独立于旧 `SOCAGENT_LTM_ENABLED`，不会自动启用 SM/EM。默认不开启；实验脚本显式选择 PM 或 baseline。

```bash
SOCAGENT_LLM_PROVIDER=fancybag uv run python -m scripts.evaluate_procedure_cards \
  --split train --cases incident_322-q0013 --run-id example-development
```

每次运行保留独立目录，记录卡片快照、实现哈希、角色完整输入输出、token、真实 MySQL 结果和评分。训练题仅用于开发；确认集固定在项目的 PM 分析目录，尚未进行最终确认。

## 当前限制

- 检索目前使用显式类型和词项规则，未知表达可能漏召回；未来扩展须经独立检索评测。
- 依赖经过离线字段核对；检索器支持传入当前 schema 过滤，但生产适配尚未自动提供实时 schema，不能宣称已实现在线可用性保证。
- 语义角色、多个实体和时间范围仍需要 Planner 判断，类型就绪不等于完整参数绑定。
- 单个事件支持的卡片在构建审计中可识别，不能当作跨事件验证成功。
- 输出协议错误、无进展循环、完整端到端效果及未见事件泛化仍需迭代验证；开发样例成功不是“效果优秀”的证据。

## 可选证据展示实验

`ExcytinBenchWorkflow(compact_evidence=True)` 或评估脚本 `--compact-evidence` 仅改变 Planner/Verifier 的输入展示。G/E/T 树保持嵌套；至少三条且字段顺序一致的 facts 可表示为 columns/rows。只有包含解码说明后 UTF-8 字节数减少超过 10% 才使用，异构记录及较小结果保持原样。原始 ITT、Reviewer 输出、来源和限制不变。默认关闭；字节缩减不等于实测 token 降低，须单独验证答案质量与成本。

空结果恢复卡 `empty_time_scope_diagnostic` 使用相邻查询对作为来源：前一查询为空，后一查询保留相同非时间条件并返回记录。当前支持 SigninLogs/IPAddress、EmailEvents/SenderFromAddress、DeviceProcessEvents/DeviceName 三类范围诊断。仅最新真实执行为空时触发；不把放宽范围后的记录直接视为题目答案，不解锁任何新实体证据。
