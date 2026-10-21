# 训练集路径记忆 v2：完整 SQL 回放与流程归并

当前训练集 418 道题、8 个 incident 的官方路径均已完整复现。验证直接执行最终导出的、合并后的 PM/EM；每一步同时检查目标实体值、指定告警 SystemAlertId、与前一步实体的连接。所有已保存的起点类型入口均通过。

这是**给定官方起点和已选流程的数据库回放**，不是 Agent 自主选路或问答准确率。原始问题中有至少一个起点字面值的题目为354道；其余64道还需要在线系统识别/补充起点。没有修改现有 workflow 或其角色提示词。

## 结果

| 项目 | 结果 |
|---|---:|
| 严格数据库路径复现 | 418 / 418 |
| 每个起点变体均通过的题目 | 418 / 418 |
| SQL 查询次数（覆盖所有入口与必要分页） | 4,091 |
| 单元测试 | 44 通过 |
| SM | 23个概念、94个事件映射 |
| EM | 169条 |
| PM策略卡 | 85条（原232条） |
| 保留的精确类型路线 | 232条，作为PM内部变体 |
| 合并后的事件场景 | 397组（原403组） |
| 只需要单步EM的题目 | 12道 |

证据分别存于 full_replay_v2.json、validation_report.json、cases/ 每题的 live_validation。旧版19/418结果和原始失败记录保留于 archive/ 以及每题 live_validation_v1。

## 修复了什么

1. **CSV导入损坏JSON。** 原始CSV按标准引号规则读取后，当前八个事件所需告警均可严格解析。旧MySQL导入损坏引号/反斜杠，例如incident_5原表144行仅9行JSON_VALID。新适配器先解析CSV，再严格解析JSON，不做猜测式修复。
2. **按incident裁剪遗漏告警。** 新适配器从原始全量 SecurityIncident.AlertIds 读取事件成员，再从全量 SecurityAlert CSV 获取实际告警记录。例如incident_5官方图68个告警，旧裁剪数据缺3个，全量原始日志存在这些记录。适配器不读取图节点、训练答案或中间实体。
3. **不能只用最新快照。** 部分目标实体只在历史告警快照中出现。保留不同实体载荷快照，完全相同载荷去重；一次输入/输出关联必须位于同一个 SourceRowHash，禁止跨快照拼接。
4. **查询只返回需要的字段。** 原始实体解码后形成会话临时投影。EM的SQL按输入类型、输入值、事件范围和输出类型筛选，只返回目标实体原始字段及必要来源；不向LLM传整段Entities。超过一页继续分页，不静默截断。

## 物理数据边界

backend.py 在本地 MySQL 连接内创建临时 LTMEntityEvidence 和 LTMInputEvidence。后者是相同内容的输入侧副本，用于规避MySQL临时表不能自关联的限制。连接关闭后临时表消失。

**没有修改原业务表、全量CSV或现有导入脚本。** 此次修复的是 LTM 独立回放的数据适配层；旧 SecurityAlert 表的导入问题仍存在。不能绕过此适配器直接把新SQL丢给旧workflow，未来接入时必须一并接入适配器或另行修复正式数据导入。

数据来源只包含原始 SecurityIncident 和 SecurityAlert CSV，来源SHA256保存在回放报告中。成员范围来自事件日志，不能从官方答案或图路径补造日志。源日志缺失、范围内无效JSON都会明确暴露；无关incident的无效行不会污染当前incident。

## SM、EM、PM的结构

- semantic.json：concepts / events / tables。每个概念保留 source_mapping（原日志语义）和 mapping（当前SQL物理投影），GraphValue用于复现官方标识规则，RawComponents保留原始值。
- episodic.json：每条含known / wanted / SQL / parameters / postprocessor / source_refs。输入值和事件标题均为绑定参数，SID与账户名、设备ID与主机名等命名空间不混用。
- procedural.json：策略级guide、known.any_of、wanted.any_of、variants、scenarios。策略说明只概括调查阶段，执行必须选择具体类型路线及事件场景。
- case_memory_index.json：每题的PM、场景、类型路线和该场景下允许的起点类型；单步题直接引用EM。

旧单题原型仍保留在上级目录供原测试使用。本目录是新的完整训练库，不与上级样例或 catalog.json 中间产物叠加加载。

## PM为什么从232条合并为85条

旧版本把账户SID、账户名、AAD账户ID等输出差异，以及同类实体连续追踪的次数，都作为顶层PM差异，粒度偏细。

新版按**有序调查阶段**合并，例如“账户→主机→URL”。同阶段连续追踪在策略层归为一类，但具体路线仍保留每一步，不删除依赖。不同阶段顺序不合并；标识类型的区别放入精确路线，不视作等价标识。

因此，顶层策略卡从232降到85（减少约63.4%）；原232条精确路线仍作为内部变体保留，避免丢失覆盖。相同路线的不同输入类型通过第一步 episode_by_input 映射选择EM，不再重复存整条路线。相同事件序列合并为397组场景，allowed_inputs限制每条路线可用的入口类型。

以后检索可以先看85条策略摘要，再展开少量适配路线与场景；当前没有改动检索器。不能把85条卡解释为只剩85条固定SQL链，也不能将一个场景的入口任意用于另一个场景。

## 复现的判定

runtime.py 只接收显式选择的PM/场景/路线与种子实体，执行期间不读取题目、图或答案。

replay_one.py 作为测试夹具提供官方起点，并在**整条路线执行结束后**核验：

1. 第一步从提供的起点查到官方桥接实体；
2. 后续每一步输出值由实际前一步候选传递得到；
3. 证据行属于官方指定告警，并且matched_inputs包含官方上一步实体；
4. 全部步骤和全部保存的起点入口均通过。

这验证的是官方路径可由这些片段组合执行，不证明候选唯一，也不把告警共现当作因果、同一操作者或攻击先后。保留候选与来源，最终题意判断仍属于后续Verifier。

## 命令

```bash
# 全量、串行复现（无需LLM，使用当前本地MySQL及原始日志CSV）
.venv/bin/python -m src.memory.longterm_memory.path_memory_v1.training.replay_all

# 单题复现
.venv/bin/python -m src.memory.longterm_memory.path_memory_v1.training.replay_one --case incident_55_train_000

# 去重、来源覆盖、类型、依赖与回放状态审计
.venv/bin/python -m src.memory.longterm_memory.path_memory_v1.training.audit

# 单元测试
.venv/bin/python -m pytest src/memory/longterm_memory/path_memory_v1/tests src/memory/longterm_memory/path_memory_v1/training/tests -q
```

初次构建仍为确定性逐条解析官方路径，每条拆解、验证、合并、落盘后再处理下一条；没有声称逐条人工语义审阅或418次LLM生成。历史每10条数量见MILESTONES.md，其中232为归并前的PM数量。
