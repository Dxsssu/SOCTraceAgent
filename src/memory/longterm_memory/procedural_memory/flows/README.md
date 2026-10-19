# Procedural Memory：多步调查流程

训练基底目录包含 **99 条多步流程、223 个流程步骤**。它们来自已审计的 237 条训练轨迹联合目录，保留原来的步骤依赖、参数、注意事项和训练证据，没有重新生成 SQL。

每个步骤的 `episodic_id` 引用 [Episodic 单步目录](../../episodic_memory/investigation_snippets/README.md)中的片段，`template_id` 对应其中的 SQL 模板。PM 自身不再保存 SQL 模板字典。运行时交叉校验两个目录的指纹和每一条引用；缺失引用时直接报错。

## 当前检索与执行

```text
context + question / 当前 ITT
  → Planner 发起 memory_query（统一记忆检索入口）
  → PM：实体概念与角色 + 调查意图 + 目标输出 + 当前 Schema
      命中适用流程 → 按流程依赖逐步建立 T
      未命中或不适用 ↓
  → EM：同样检索条件，召回单步调查片段
      命中适用片段 → 建立一个 T，执行后规划下一步
      未命中或不适用 ↓
  → SM：召回候选表及相关字段 → Planner 选表建立 T
  → Executor 输出当前 T 的只读 SQL
  → 数据库 → Reviewer → Verifier
      证据充分：回答；否则：Planner 更新 ITT，继续流程或重新选路
```

检索和选择都由 Planner 驱动；Executor 不检索记忆。本层没有候选时，运行时自动查询下一层；有候选但 Planner 判断不适用时，重复完全相同的 `memory_query` 表示拒绝当前层。不能直接跳过 EM 选表。候选检索是本地匹配，不增加模型调用；Planner 阅读候选、选择任务仍需要模型调用。

PM、EM 分别召回至多 4 个候选，不注入完整目录。排序综合目标输出覆盖、意图词匹配、首步对已知概念的覆盖和流程长度；首步能同时使用账户与告警线索的流程优先于只能使用其中一个线索的泛流程。实际 SQL 投影字段通过 Semantic 补充输出概念，WHERE-only 字段不视为可返回结果。PM 要求候选流程覆盖请求的目标概念，EM 可以服务于调查中的一个中间目标。召回只是候选，仍需 Planner 判断条件、覆盖范围及返回值限制是否适合当前问题。

任务选择协议：

- `add_task.procedure: {id, step, inputs}`：选择多步流程当前步骤。
- `add_task.episode: {id, step: s1, inputs}`：选择单步经验。
- `add_task.table: 表名`：在 SM 中选择表。

三种选择互斥。流程首步的 T ID 作为 `run`，后续步骤填写同一个 `run`。`active_runs` 向 Planner 提供流程、已完成步骤和下一步依赖；它不是 G 节点下的两三句话指导文本。ITT 仍为 G/E/T，依赖后续步骤的 T 在前一步真实返回后建立，因此不需要一次性插入整条链。

后续参数只能绑定本次上游原始查询结果，不能使用训练样本值或 Reviewer 推测。空结果、错误、截断、未明确选择的多个候选均不能直接绑定后续步骤。步骤失败后交回 Planner，可以换流程、选单步或回退 SM；不会机械执行整条链。

运行时按类型绑定 SQL AST 字面量，把 `bound_sql` 交给 Executor，再校验返回 SQL 的 AST。PM/EM 不能任意改写模板条件；SM 路径允许根据实际 SQL 错误修复一次。

## 真实目录示例

`proc_38c6a754cd6b54d83b20b8f2`：从隔离邮件事件解析邮件 URL。

1. `s1` 引用 `proc_41e44aec9d237e871004161d`：在 `EmailPostDeliveryEvents` 中查询邮件处理动作，返回消息 ID。
2. `s2` 引用 `proc_404d99a96dcdff2441888a0d`：用第一步真实返回的 `NetworkMessageId` 查询 `EmailUrlInfo`。

这是一个由单步片段组合成的两步流程，并不代表所有邮件调查的完整计划。该流程保留的限制包括：查询到邮件中的 URL 不证明用户点击，单 URL 返回限制可能需要继续调查。

## 重建、验证与日志

在项目根目录运行：

```bash
.venv/bin/python -c 'from src.memory.longterm_memory.procedural_memory.flows.catalog import main; main()'
.venv/bin/python -m pytest tests/test_layered_investigation_memory.py tests/test_procedural_workflow.py -q
```

构建输入是 `../trajectory_snippets/excytin/catalog.json` 历史联合目录；输出是本目录的 `catalog.json`、`split_audit.json` 和 EM 目录。拆分保留全部 419 条记录及 320 个 SQL 模板，不使用测试集、不在线回写新记忆。

日志分别记录 `procedural_retrievals`、`episodic_retrievals` 和 `memory_retrievals`（SM）；`memory_routing_history` 保留 PM/EM 的顺序。任务上下文分别放在 `task_procedural_contexts`、`task_episodic_contexts` 和 `task_semantic_contexts`。PM 内部引用 EM 原子步骤不计为一次独立 EM 检索。

`procedural_memory_enabled=False` 同时关闭 PM/EM，保留原 SM-only 对照；`semantic_memory_enabled=False` 关闭三层记忆。当前实现未接入旧版 `MemoryContextService`。

## 失败案例调整层（2026-10-19）

训练目录保持原样；运行时按 `catalog.json` + EM 基底 → `activation.json` → `adaptations.json` 加载，并记录三个来源的指纹。

- `activation.json` 暂停 **57 条上轮最终选择为零的流程**，不是未进入检索候选的流程；保留每条历史选择次数。暂停项不会被检索，也不能通过旧候选强行绑定。解除暂停需要明确编辑该清单，重新构建训练基底不会自动恢复它们。
- 对 **266 道失败案例（46 道运行失败、220 道零分）**逐案留下 SQL 顺序、Reviewer 摘要和关键原始证据审阅记录。并非逐字阅读每个宽结果单元格。
- 合并为 **13 条新流程、16 条旧流程指导调整**，新增 **9 条 EM / SQL 模板**。当前 **55 条 PM 可用、57 条暂停，329 条 EM 可用**。同一流程来自多组案例的指导合并为一个补丁；不按案例数机械增加流程。
- 新增与修订全部标记 `evaluation_failure_adaptation`。这是利用测试失败案例调优，不能将后续同一测试集的得分称为独立泛化评估；未把参考答案写入 SQL 参数。
- `deferred` 保存 20 项未启用的延伸设计，主要缺少嵌套 JSON 派生、可靠跨事件关联键或操作者审计证据。它们不是运行时流程；部分已有前两步取证覆盖，后续由 EM/SM 调查。格式错误、歧义与评分冲突也不能仅靠 PM 保证解决。

[完整审计](failure_review/audit.json)、[266 条逐案记录](failure_review/cases.json)、[真实 SQL 链回放](failure_review/sql_validation.json)。SQL 回放证明模板可执行、参数依赖可绑定，不证明模型一定选择正确或最终回答正确；本轮没有重新跑 266 题评分。

本地保留审阅工作目录时，可以重建调整层：

```bash
.venv/bin/python scripts/build_failure_memory_adaptations.py runtime/pm_failure_review_20261019
```

调整层中的 `new_episodes` 与 `new_templates` 在运行时归入 EM；`new_flows` 只组合这些或已有 EM。它们与不可变训练 EM 分开保存，以便追溯与撤销测试案例调优。移走 `adaptations.json` 可恢复训练记忆与现有屏蔽清单的组合。

本轮校验：13 条新流程做了 15 次真实依赖链回放，共 34 次 EXPLAIN 和 34 次只读 SELECT；13 个相同自然语言检索样例由 12 个进入 top4 提升至 13 个，SAP UPN 专用链由第 5 升至第 1。34 项相关回归测试通过；项目 tests 套件仍有 6 项既有失败，详见[验证台账](failure_review/verification.json)和[检索对比](failure_review/retrieval_validation.json)。这不是模型端到端准确率测试。
