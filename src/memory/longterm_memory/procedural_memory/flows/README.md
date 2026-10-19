# Procedural Memory：多步调查流程

训练基底目录包含 **99 条多步流程、223 个流程步骤**。它们来自已审计的 237 条训练轨迹联合目录，保留原来的步骤依赖、参数、注意事项和训练证据，没有重新生成 SQL。

每个步骤的 `episodic_id` 引用 [Episodic 单步目录](../../episodic_memory/investigation_snippets/README.md)中的片段，`template_id` 对应其中的 SQL 模板。PM 自身不再保存 SQL 模板字典。运行时交叉校验两个目录的指纹和每一条引用；缺失引用时直接报错。

## 当前检索与执行

```text
context + question / 当前 ITT
  → Planner 先建立/更新类型化 G/E，不建 T
  → Planner 发起 memory_query（统一记忆检索入口）
  → PM：实体概念与角色 + 调查意图 + 目标输出 + 当前 Schema
      命中适用流程 → 一次建立全部 T → 按顺序执行，运行时绑定真实上游结果
      未命中或不适用 ↓
  → 组合 PM：最多 3 条已有流程，终点输出与下一条输入须类型/命名空间兼容
      命中 → 会话内组合，建立全部 T；不新增持久 SQL 模板
      未命中或不适用 ↓
  → EM：同样检索条件，召回单步调查片段
      命中适用片段 → 建立一个 T；发现新线索后重新从 PM 开始检索
      未命中或不适用 ↓
  → SM：召回候选表及相关字段 → Planner 选表建立 T
  → Executor 输出当前 T 的只读 SQL
  → 数据库 → Reviewer → PM 有可绑定的后续步骤则继续 Executor
  → 单步结束 / PM 完成或暂停中断 → Verifier
      证据充分：回答；否则：Planner 更新 ITT，继续流程或重新选路
```

检索和选择都由 Planner 驱动；Executor 不检索记忆。本层没有候选时，运行时自动查询下一层；有候选但 Planner 判断不适用时，可显式指定 `memory_query.layer: composed/episodic/semantic`；同 known/wanted 的后续请求也会继续下探，不依赖意图措辞完全相同。普通 table 任务会记录本地组合/EM 检查与 SM 回退。候选检索是本地匹配，不增加模型调用；Planner 阅读候选、选择任务仍需要模型调用。

PM、EM 分别召回至多 4 个候选，不注入完整目录。排序综合目标输出覆盖、意图词匹配、首步对已知概念的覆盖和流程长度；首步能同时使用账户与告警线索的流程优先于只能使用其中一个线索的泛流程。实际 SQL 投影字段通过 Semantic 补充输出概念，WHERE-only 字段不视为可返回结果。PM 的必要外部 known 按 AND 匹配（包括后续步骤的外部输入）；wanted 按实际 DAG 终点输出匹配，中间结果不能冒充终点。PM 要求候选流程覆盖请求的目标概念及指定类型，EM 可以服务于调查中的一个中间目标。召回只是候选，仍需 Planner 判断条件、覆盖范围及返回值限制是否适合当前问题。

任务选择协议：

- `add_task.procedure: {id, step, inputs}`：选择多步流程首步；运行时自动展开其余步骤。
- `add_task.episode: {id, step: s1, inputs}`：选择单步经验。
- `add_task.table: 表名`：在 SM 中选择表。

三种选择互斥。选定 PM 后，系统在当前 E 下预先建立全部 T，首步 T ID 作为 `run`。多个 E 共同满足 known 时，一个 T 通过 `input_entity_ids` 记录所有输入边，仍仅执行一次；输入 E 必须属于同一 G。`active_runs` 给出步骤与 T 的对应关系、完成记录和运行状态；ITT 中每个 T 的 `procedure` 元数据给出 `depends_on` 和 `binding_state`。待执行 T 不预填未知参数或 SQL。

Executor 按流程步骤顺序执行，每一步仍经过 Reviewer 回写证据。系统根据 `bindings` 从本次真实上游结果绑定下一步；正常衔接时不再调用 Planner 或 Verifier，流程结束、暂停、中断或预算耗尽时才交给 Verifier。依赖可以指向更早步骤，不限于紧邻的前一步。集合参数传递所有有效去重值；标量出现多个候选时暂停，不擅自取第一个。

后续外部已知参数可在首步的 `procedure.step_inputs` 中按步骤提供，例如 `step_inputs: {s2: {p2: known_value}}`。依赖参数必须来自实际结果，不能用训练值或外部参数覆盖。缺少外部参数或标量歧义时，Planner 使用 `select_task` 选择已有 T，并携带相同的 `procedure: {id, step, run, inputs, selections}` 补充输入或选择，不重复建立 T。空结果、失败或截断导致依赖无法绑定时，阻塞未执行 T，交回 Planner 另选路径。

EM 返回以前未见的有效实体值且 Verifier 尚不能回答时，Planner 先把有成功 SQL 来源的线索加入 E，再清除之前的层级跳过状态，要求 Planner 根据新线索再次提交 `memory_query`，重新检查 PM。没有适配 PM 再选 EM，两层均无适配方案才进入 SM。重复的 EM 记录不会反复触发这一重置。

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

日志分别记录 `procedural_retrievals`、`episodic_retrievals` 和 `memory_retrievals`（SM）；`memory_routing_history` 保留 PM/EM 的顺序。任务上下文分别放在 `task_procedural_contexts`、`task_episodic_contexts` 和 `task_semantic_contexts`。PM 内部引用 EM 原子步骤不计为一次独立 EM 检索。`procedural_flow_runs` 记录流程状态、任务映射及实际完成步骤；预建的 T 属于已规划步骤，不代表已经执行，实际执行次数应从 executions 统计。

`procedural_memory_enabled=False` 同时关闭 PM/EM，保留原 SM-only 对照；`semantic_memory_enabled=False` 关闭三层记忆。当前实现未接入旧版 `MemoryContextService`。

## 失败案例调整层（2026-10-19）

训练目录保持原样；运行时按 `catalog.json` + EM 基底 → `activation.json` → `adaptations.json` 加载，并记录三个来源的指纹。

- `activation.json` 暂停 **57 条上轮最终选择为零的流程**，不是未进入检索候选的流程；保留每条历史选择次数。暂停项不会被检索，也不能通过旧候选强行绑定。解除暂停需要明确编辑该清单，重新构建训练基底不会自动恢复它们。
- 对 **266 道失败案例（46 道运行失败、220 道零分）**逐案留下 SQL 顺序、Reviewer 摘要和关键原始证据审阅记录。并非逐字阅读每个宽结果单元格。
- 合并为 **13 条新流程、16 条旧流程指导调整**，新增 **9 条 EM / SQL 模板**。该轮结束时 **55 条 PM 可用、57 条暂停，329 条 EM 可用**。同一流程来自多组案例的指导合并为一个补丁；不按案例数机械增加流程。
- 新增与修订全部标记 `evaluation_failure_adaptation`。这是利用测试失败案例调优，不能将后续同一测试集的得分称为独立泛化评估；未把参考答案写入 SQL 参数。
- `deferred` 保存 20 项未启用的延伸设计，主要缺少嵌套 JSON 派生、可靠跨事件关联键或操作者审计证据。它们不是运行时流程；部分已有前两步取证覆盖，后续由 EM/SM 调查。格式错误、歧义与评分冲突也不能仅靠 PM 保证解决。

[完整审计](failure_review/audit.json)、[266 条逐案记录](failure_review/cases.json)、[真实 SQL 链回放](failure_review/sql_validation.json)。SQL 回放证明模板可执行、参数依赖可绑定，不证明模型一定选择正确或最终回答正确；本轮没有重新跑 266 题评分。

本地保留审阅工作目录时，可以重建调整层：

```bash
.venv/bin/python scripts/build_failure_memory_adaptations.py runtime/pm_failure_review_20261019
```

调整层中的 `new_episodes` 与 `new_templates` 在运行时归入 EM；`new_flows` 只组合这些或已有 EM。它们与不可变训练 EM 分开保存，以便追溯与撤销测试案例调优。移走 `adaptations.json` 可恢复训练记忆与现有屏蔽清单的组合。

本轮校验：13 条新流程做了 15 次真实依赖链回放，共 34 次 EXPLAIN 和 34 次只读 SELECT；13 个相同自然语言检索样例由 12 个进入 top4 提升至 13 个，SAP UPN 专用链由第 5 升至第 1。34 项相关回归测试通过；项目 tests 套件仍有 6 项既有失败，详见[验证台账](failure_review/verification.json)和[检索对比](failure_review/retrieval_validation.json)。这不是模型端到端准确率测试。

## 查询投影与主机名匹配优化

运行时以 `investigation-projection-host-boundary-v1` 策略生成当前调查的 SQL 视图，不改写训练目录或模板 ID。选中片段时保存当次 `memory_query`，PM 后续步骤沿用同一目标，避免之后检索改变执行范围。

普通单表列投影只保留 known/wanted 对应字段、出边依赖字段、已投影的过滤/排序字段，以及告警、实体角色、主机/账户关联字段和一个时间字段。AdditionalFields 不默认保留；当目标明确需要该字段或后续 parse_json 依赖它时保留。没有识别到目标输出/依赖，或遇到 DISTINCT、聚合、连接等复杂投影时保留原查询，避免改变结果语义。SQL 输出列清单与观察解析契约同步更新，审计上下文 `query_optimization` 记录原始与实际列。

正向主机等值条件或无通配符 LIKE 的短名自动转换为 `DeviceName = 'host' OR DeviceName LIKE 'host.%' ESCAPE '!'`，使用域名点号边界。完整域名、显式通配符模式、否定条件及非主机字段不作此转换。原 WHERE 的其他条件与 LIMIT 保持不变。

数据库返回行数限制和 workflow 的 20,000 字符限制仍然保留；裁剪字段减少长 JSON 带来的截断，但不保证所有查询都不截断。


## 220 道零分题的官方路径迭代（2026-10-19）

在上述调整层之后加载 `official_path_adaptations.json`，单独记录指纹。完整检查 220 道已完成但 reward=0 的案例及官方图路径，验证每条路径的边和最短距离；逆向提取查询步骤，再用实际数据库结果验证正向参数依赖。新增 6 条 EM、10 条 PM，并对 3 条已有 EM 补充指导。合并后为 **65 条可用 PM、57 条暂停 PM、335 条 EM**。

同一告警主题与实体标识筛选的两份 EM 合并为一个模板，保留双方需要的证据字段；多条 SAP 流程复用相同原子模板，但不同的起终告警主题、目标角色和步骤指导保留为不同 SOP。新增 PM 的每步都引用 EM，不复制独立 SQL。已知信息必须来自当前 context 或前一步实际结果；标准答案和官方隐藏节点 ID 只用于离线审阅，不能用于在线绑定。

SecurityAlert.Entities 的跨步依赖新增严格的 `entity_identifier` 变换：按实体 Type 和指定标识字段提取，解析 `$ref`，支持官方定义的复合进程标识。无效 JSON、失效引用或多候选不能静默选第一项。原始证据 EM 的候选输出只表示可能包含所需信息，不等于已验证的类型化输出；PM 使用单独的 `evidence_outputs` 契约。投影优化保留依赖与类型化终点需要的证据列。

[逐案记录](official_path_review/cases.json)、[未启用的流程设计](official_path_review/deferred_sops.json)、[审计汇总](official_path_review/audit.json)分开保存。暂缓原因包括源告警缺失、实体 JSON 损坏、通用文件名跨主机弱关联及无法唯一判定角色。10 条 PM 通过实际依赖链重放，其中 Kerberos 流程仍保留两个 SID 候选；“可执行”不代表“答案唯一”。本轮未做 LLM 端到端重跑，不声明准确率提升。这是测试集驱动的调整，后续在同一批题上的成绩不属于独立泛化评估。

新增 PM 名称：

1. 收件箱规则操控 → 匿名登录：实体关联溯源
2. BEC可疑发信 → BEC规则欺诈：实体关联溯源
3. DCSync复制攻击 → 票据身份盗用 → 疑似横向移动：实体关联溯源
4. Kerberos哈希传递 → 哈希传递成功登录：实体关联溯源
5. 可疑收件箱规则 → 稳定账户关联 → BEC关联SAP登录
6. 匿名IP登录 → 稳定账户关联 → 风险SAP认证
7. BEC关联SAP登录 → 稳定账户关联 → BEC财务欺诈
8. 风险SAP认证 → 稳定账户关联 → BEC财务欺诈
9. 可疑收件箱规则 → 稳定账户关联 → 风险SAP认证
10. BEC财务欺诈 → 稳定账户关联 → BEC关联SAP登录

最终检查：10 条新增流程在人工构造 known/wanted 的正常检索中，原题英文意图与中文改写均为 10/10 进入 Top 4；未强制指定 PM ID。相关 56 项测试通过；接入后全套 543 项通过、6 项既有失败（见审计目录）。


## 8 道回归失败题的路径重建

在上一轮 220 题迭代基础上，逐条检查 10 题回归中的 8 道零分题。按官方最短图路径拆分关系查询，再验证当前数据库中的正向可执行链；不把官方隐藏 ID 或答案用作在线输入，也不声称 SQL 数全局最优。

- 新增 7 条 EM，复用并补全 1 条现有告警时间 EM；新增 3 条 PM。
- 将 4 条同结构的 IP→账户→目标事件 IP 流程合并为 1 条，保留 4 个独立方向变体；净减少 3 条重复 PM。
- 当前 **65 条可用 PM、57 条暂停 PM、342 条 EM**。历史训练目录不改写，调整仍单独保存在 official_path_adaptations.json。
- 另外纠正 6 条既有 PM 的目标或适用范围，防止把中间 SQL 返回的任意字段当作流程目标。

EM 合并条件是过滤条件、关联键、参数类型和输出角色一致；仅表名相同不能合并。PM 合并条件是步骤模板、依赖结构、起点类型和目标角色相同；不同告警阶段保留独立 variants、step_guides 与 source_cases。查主机 AAD 设备 ID、查账户 AAD 对象 ID、查 UPN、查执行账户不能互相替代。

检索允许明确标注 partial 覆盖，返回 covered/related/potential/missing 四类目标；原始 JSON 可能包含某信息不等于已经类型化验证。显式 SID/AAD ID 优先保持精确目标，辅助字段不再硬性屏蔽全部 PM。按当前目标、稀有意图词和流程适用范围排序；背景起点与目标阶段分开，matched_variant_id 记录具体方向。Planner 被要求只提交核心 wanted，告警时间不能直接替代登录时间。

新增流程：

1. PowerShell 投放线索 → 已观察设备 → 目标 LDAP 告警进程。
2. 重命名工具文件 → 稳定账户 → Impacket 目标告警账户角色。
3. 起点主机 PRT 进程 → 同文件候选主机集合 → 横向移动告警 IP。

真实 SQL 重放及独立复核保留全部上游候选；坏 JSON 覆盖数 UnparsedAlertCount>0 时不能宣称完整唯一。同告警主机/IP共现不等于逐项映射。Kerberos 题仍有两个 SID 候选，Impacket 题仍未唯一证明执行账户，不能以记忆匹配代替角色证据。

[逐案路径与查重记录](rebuild_review/20261019_regression8/cases.json)、[重建汇总](rebuild_review/20261019_regression8/audit.json)、[真实 Planner 查询与检索回放](rebuild_review/20261019_regression8/retrieval_validation.json)。8 次首轮模型调用的原始查询没有手工改写；重建后均能首轮召回对应记忆。这里只测首轮查询和离线 SQL/依赖，未重新运行完整调查并评分，不能宣称 8 题全部答对。

本地审阅目录存在时可重建与检查：

```bash
.venv/bin/python scripts/rebuild_regression_memories.py
.venv/bin/python scripts/validate_rebuilt_memory_retrieval.py
```

最终校验：真实首轮 Planner 查询回放 8/8 对应记忆排名第 1（6 PM、2 EM），不等同于模型实际选用或最终答对；完整测试 551 项通过、6 项既有失败。

## G/E-first 与端点契约审计（2026-10-19）

当前目录共 122 条 PM：**60 条参与自动执行、5 条因关联范围/身份风险受审计限制、57 条保持暂停；EM 共 352 条**。本轮没有按案例机械新增 PM；新增的 10 类 EM 保存在 `coverage_adaptations.json`，训练基底不回写。

`memory_contracts.json` 为全部 65 条启用 PM 保存实际 SQL 输入、范围过滤、DAG 终点输出、变体目标、标识符命名空间和组合限制。SQL 模板指纹变化会要求重新审计；加载时核对所有必要参数、物理表字段和投影。额外修正了一条 SecurityAlert 模板残留的 RemoteIP/Title 参数标签，改为实际使用的 Entities/AlertName；另为三条 EM 的 JSON 派生输入补齐 source_field/json_path，保留逻辑字段以维持原步骤依赖。29 条存在中间输出过度声明，30 条为 scope-only 入口，4 条含后续外部输入；现在分别限制终点覆盖、要求实际范围 E、提前验证所有必要输入。存在角色或关联范围风险的值保留 candidate 标记，不能当成唯一实体关系；5 条内部身份或关联范围不安全的流程同时限制直接自动执行与自动组合，原记录保留；对应任务可回退 EM/SM。发送人邮箱与账户 UPN、SID 与对象 ID 不互换。IP 输入沿用 SM 的 kind_policy=hint，可用已观测的同一地址跨角色检索；wanted 角色与自动组合桥接仍严格校验。

覆盖审计遍历 220 道零分题的 489 个官方目标跳步，检查全部 342 条原 EM，并执行 834 次原模板/源查询、404 次替代路径查询、516 次新增族查询，SQL 错误为 0。新增族按 SID、AAD 账户 ID、账户名、UPN、主机名、AAD 设备 ID、文件、命令提及文件、IP、URL 合并为 10 条。

严格的本地证据覆盖从 163/489 提升到 **322/489（65.85%）**；仍有 157 项仅部分支持、10 项在已检查路线中不可达。96/220 案例的全部目标跳步有本地支持。这个数字包括经验证可解析的原始实体证据，**不是在线召回率、完整调查成功率或测试集独立泛化成绩**。新族使用训练无关的参数化 SQL，但来源仍属于 evaluation_failure_adaptation。

[PM 审计摘要](contract_review/pm_summary.json)、[PM 契约校验](contract_review/pm_validation.json)、[EM 覆盖统计](contract_review/em_coverage.json)、[EM 逐跳审计](contract_review/em_hops.json)、[EM 新增族校验](contract_review/em_family_validation.json)、[全部 EM 参数与 SM 覆盖检查](contract_review/em_parameter_validation.json)。

本次改造相关 59 项回归测试通过；最近一轮全套为 564 通过、6 项既有旧服务/Splunk 测试失败。3 道 Fancybag/DeepSeek 集成烟雾共发出 5 次 SQL，均在后续模型调用中因 APIConnectionError 重试耗尽而中止，未完成端到端评分。详见 [验证记录](contract_review/verification.json) 与 [实际接口运行记录](contract_review/online_smoke.json)。
