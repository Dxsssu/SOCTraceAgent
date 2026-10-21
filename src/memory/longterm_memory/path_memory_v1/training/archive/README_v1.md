# 全训练集官方路径 LTM 构建稿

## 范围及完成情况

当前 train 目录共 418 道题、8 个 incident。由 manifest.json 固定来源文件、数组索引和 SHA256，不读取测试集，也不导入旧记忆。保留原来的单题原型，本目录是全训练集构建稿，尚未替换其检索入口或接入 workflow。

构建方式是**基于官方类型和路径的确定性逐条解析**，不是 418 次 LLM 生成，也不是逐条人工语义审阅。build_next() 每次只处理一条完整训练记录：读取 context/question/solution/官方路径，核对路径边及最短长度，补起点/终点实体，逐个告警拆步，校验并合并记忆，保存该题来源与结果，随后才进入下一条。serial_review 只是串行调用这一单条事务；没有并行、整组题一次生成或预设最终记忆数量。

构建完成后，对已有流程再做结构归并，不重新生成调查路径。原始 415 条去重事件范围方案保留在 catalog.json 作为构建中间产物；最终加载用下面三份文件，不能把中间产物与最终文件同时当作记忆库。

| 记忆 | 数量 | 内容 |
|---|---:|---|
| SM | 23 个概念、94 个事件映射 | 22 类实体标识 + 事件标题；类型、JSON位置、派生规则、原始值与规范化值的区别 |
| EM | 169 条 | 有方向的单步实体关联，输入、输出、SQL、类型化解析、证据及失败处理 |
| PM | 232 条 | 2–5步流程、EM引用、依赖绑定、可选入口、403组事件场景 |

12 道单步题只引用 EM，不重复建一个单步 PM。多步 PM 分布：2步88条，3步99条，4步39条，5步6条。

## 三份最终文件

- semantic.json：concepts / events / tables。
- episodic.json：episodes，每条有 known / wanted / signature / sql / postprocessor / source_refs。
- procedural.json：procedures，每条有 known.any_of / wanted / guide / variants / scenarios / policies / source_refs。
- case_memory_index.json：每道训练题最终对应哪个 PM、场景、允许入口，或直接对应哪些 EM。

SM 的当前物理映射以官方图的来源 SecurityAlert.Entities 为准，仅覆盖官方路径出现的字段，不声称覆盖环境的全部日志表。每个实体都以 Type 与属性组合区分；进程复合标识保留 PID、创建时间和命令行；命令行文件名线索不是进程镜像名；Name@UPNSuffix 派生值不自动等于真实 UPN。

EM 的 SQL 按 AlertName 绑定查询，只读取 SystemAlertId、AlertName、TimeGenerated、Entities。当前是 SQL + 本地类型化解析的片段：known实体由 postprocessor 校验，而非所有过滤都在 SQL 内完成。Entities 不发送给 LLM，解析后只返回所需实体、原始组成字段及告警来源。这是保守的官方路径转译基线，尚未优化为各业务日志的最小投影 SQL。

只有有效 JSON 才执行类型化解析。数据损坏时明确阻断，不静默跳过、猜补字段或使用官方答案代替日志结果。官方 graph_value 的小写/拼接规则仅用于复现图；raw_components 保留原始值，不能把规范化图值无条件用于真实URL等语义判断。

## 去重规则

- SM：实体类型 + 标识属性相同才合并。SID、AAD ID、账户名分别存储；邮件 SenderIP 与普通IP实体分开。
- EM：输入概念、输出概念、方向、关系、查询方式、候选传递方式一致才合并。事件标题参数化；不同方向不合并。
- PM：中间/最终输出概念序列、步骤顺序及执行策略一致时复用骨架。不同起点保存为入口 variants，不同完整事件序列保存为 scenarios。
- 必须选择一个完整 scenario 及其 allowed_variants 中的入口，禁止跨场景任意拼接事件标题。单步只保留为 EM。

来源合并到 source_refs，具体案例值与参考答案放在 cases/，不进入可检索记忆卡。每条案例还记录弱标识桥接、原文是否出现起点、逐步证据和新增/复用数量。

## 验证与实际限制

- 418/418 官方路径的边、最短长度与逐步目标见证验证通过。它是图记录复现，不是问答准确率。
- 418 条均做了本地数据库回放：19 条至少有一个入口完整复现；349 条因 Entities 无效 JSON 阻断，49 条返回空结果，1 条未找到预期见证。
- 例如 incident_5_train_000 的起点告警标题在当前 SecurityAlert 中查询为0行。不能把全部失败都归因于JSON，也不能据此声称SQL路径已全部可用。
- 354 道题原文包含至少一个官方起点值；其余64道需进一步解析或补充调查。回放使用官方起点实体，因此不是在线意图识别或自主规划评测。
- 一条图路径仅说明告警共现关系，不证明同一操作者、同一设备或因果。命令行文件名等弱连接需后续核实。
- 当前不修改数据库、共享解析器、现有检索和workflow；这批是可审阅构建稿，不能未经适配直接用于生产流程。

完整统计见 validation_report.json；每10条的累计记录见 MILESTONES.md。后半段进度集中返回，未实时逐10条发消息，该文件保留了完整记录。

## 示例

原先 incident_55 第0题映射到 pm_f9d9bf90f6864806：

URL → 账户SID → 主机名 → URL。

它通过3个EM引用构成顺序依赖，事件场景 scope_61a8894a7e67 指定起点点击、可疑探索、目标点击三个事件范围。原来的单题 AlertEvidence 原型仍保留；本训练稿选择 SecurityAlert 作为统一官方来源，数据库兼容性尚未达到原型的可执行程度。

## 命令

```bash
# 单次仅处理下一条；已全部完成时返回 complete
.venv/bin/python -m src.memory.longterm_memory.path_memory_v1.training.build_one --next

# 校验现有记忆、去重结果、来源覆盖和依赖
.venv/bin/python -m src.memory.longterm_memory.path_memory_v1.training.audit

# 测试原型和本次新增的类型、依赖、范围及来源隔离
.venv/bin/python -m pytest src/memory/longterm_memory/path_memory_v1/tests src/memory/longterm_memory/path_memory_v1/training/tests -q

# 指定一个已构建案例，重新执行数据库验证
.venv/bin/python -m src.memory.longterm_memory.path_memory_v1.training.replay_one --case incident_55_train_000
```
