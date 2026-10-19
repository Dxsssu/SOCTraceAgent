# Episodic Memory：单步调查片段

训练基底在这里存储 **320 条单步调查片段和 320 个 SQL 模板**，由原 419 条联合目录中所有单步记录原样迁移而来。每条记录保留起点实体类型、调查意图、一个步骤、类型化参数、输出字段、注意事项及训练支持证据。

例如 `proc_404d99a96dcdff2441888a0d` 表示“根据 NetworkMessageId 查询邮件关联 URL”。它既可以在 PM 未命中时由 Planner 单独检索选用，也可以被 PM 多步流程引用。保留 `proc_*` 原 ID 是为了稳定引用与审计；所属记忆类型由目录和 `memory_type: episodic` 区分。

默认顺序为 **PM 流程 → EM 单步片段 → SM 表字段**。Planner 用 `add_task.episode: {id, step: s1, inputs}` 选择当前片段，运行时验证参数并生成绑定 SQL，Executor 返回 SQL；执行后继续 Reviewer、Verifier 和 Planner 循环。仅向模型传递检索候选与当前选中片段，不加载全量 SQL 模板。

本目录中的 EM 是成功的单步调查经验，不限于 SQL 失败恢复。历史 `recovery_rules.json` 和旧 EM 服务仍未接入本次 workflow，也不计入这 320 条。

SQL 模板限制仍保留，不能把新场景误当成与训练场景相同。Planner 判断片段不适用时，应继续到 SM 自主选表与生成 SQL。模板执行失败也交回规划，不直接改写模板。

完整协议、重建命令和日志说明见 [Procedural 流程文档](../../procedural_memory/flows/README.md)。

2026-10-19 的失败案例调整层另外提供 9 条单步片段和模板，运行时 EM 共 329 条。新片段保存在 PM 目录的 `flows/adaptations.json` 中以隔离测试集来源，但加载后按单步 EM 检索和绑定，PM 仅持有引用。训练基底仍保持 320 条且不回写。
