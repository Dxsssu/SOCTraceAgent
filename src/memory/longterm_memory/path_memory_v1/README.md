# 基于一条训练路径的 LTM 单元原型

仅新增本目录。没有修改或接入 Planner、Executor、Reviewer、Verifier、ITT 或 workflow。

## 来源和边界

训练集 `incident_55_qa_incident_o1-ga_c42.json` 的数组第 0 项：已知一次恶意 URL 点击，查同设备另一条恶意点击 URL。

官方 shortest_alert_path 为 `104 → 2 → 57 → 14 → 107`：起点点击告警 → 账户 SID → 探索活动告警 → 主机 → 目标点击告警。起点 URL 实体为 91，目标 URL 实体为 97，二者不在 shortest_alert_path 数组内。

图路径是告警与实体关系路径，不直接等于 SQL 路径。按 AlertEvidence 的物理存储，将其转成三次同 AlertId 关联查询：URL → SID → DeviceName → URL。此处只证明关联，不证明攻击时序或用户就是操作者。

`training_example.json` 单独存放官方题目、答案、节点、来源哈希与测试输入。MemoryStore 只加载三份记忆配置，不加载该参考答案。回放中的三个事件标题来自官方路径离线映射；因此它是给定计划的执行测试，不是在线检索/自主规划效果测试。

## SM：输入输出的语义与物理映射

`semantic.json`：`concepts` 加 `tables`。

每个 concept 包含 `meaning / kind / fields`；field 包含表、字段、类型和可选 EntityType。

| 概念 | 物理字段 | 约束 |
|---|---|---|
| URL | AlertEvidence.RemoteUrl | EntityType=Url；保留协议和路径 |
| Account_SID | AlertEvidence.AccountSid | EntityType=User；不是 UPN/AAD ID |
| Device_Name | AlertEvidence.DeviceName | EntityType=Machine；完整域名不降级为短名 |
| Alert_ID | AlertEvidence.AlertId | 同告警证据行连接键 |
| Alert_Title | AlertEvidence.Title | 事件主题筛选，相同标题不等于同一告警 |

表简介另存 TimeGenerated、EntityType、EvidenceRole 的含义。不保存当前案例的 SID、主机或目标 URL。

## EM：单步查询片段

`episodic.json`：3 条 `episodes`。每条有 `id / name / known / wanted / relation / sql / row_limit / evidence_fields / cautions / source_refs`，必要时加派生参数。

| EM | known | wanted |
|---|---|---|
| em_url_to_sid | URL、起点事件标题 | AccountSid |
| em_sid_to_device | SID 集合、中间事件标题 | DeviceName |
| em_device_to_url | 设备集合、目标事件标题 | RemoteUrl |

known/wanted 包含 concept、kind、cardinality。wanted 的 one 表示每行单值，不代表查询只有一行。不同实体通常分行保存，SQL 必须通过 AlertId 关联，不能假设同一行同时含账户、主机和 URL。

所有值使用绑定参数。SQL 只返回任务所需字段和告警证据，100 行预算加 1 行溢出哨兵。设备输入为短名时允许匹配完整域名首段；输入为完整域名时只精确匹配。多个同名设备仍保留为候选，不擅自选首个。

## PM：依赖明确的完整流程

`procedural.json`：1 条 `procedures`，名称“通过账户与主机关联追踪另一条URL点击事件”。

包含 `id / name / known / wanted / guide / steps / policies / source_refs`。

- T1 引用 em_url_to_sid：输入 url 与 start_title。
- T2 引用 em_sid_to_device：sids 来自 T1.AccountSid 全部去重值，标题来自 bridge_title。
- T3 引用 em_device_to_url：devices 来自 T2.DeviceName 全部去重值，标题来自 target_title。

PM 不复制 SQL，而是引用 EM 并声明参数依赖。空结果、超限、SQL 异常或无效输出停止后续执行；输出只是候选及证据，不替代 Verifier。执行器只实现单链条执行契约，不包含自动重规划或 workflow 的重试机制。独立检索入口见下文。

## 测试和回放

在项目根目录运行：

```bash
.venv/bin/python -m pytest src/memory/longterm_memory/path_memory_v1/tests -q
.venv/bin/python -m src.memory.longterm_memory.path_memory_v1.replay
```

第二条需要本机 incident_55 MySQL 和 docker/excytin-mysql/.env 中的连接密码，执行三条 SELECT，并更新本目录 replay_result.json。它直接使用绑定参数执行，不调用模型，不产生模型 token 消耗。

当前验证：13 项单元测试通过。真实日志每步各返回 1 行：

1. SID：S-1-5-21-561436748-3867387497-1732241745-3701。
2. 设备：mb-winclient.internal.niseko.alpineskihouse.co。
3. URL：https://ym018491661.wittytree-b6f239d6.northeurope.azurecontainerapps.io/。

官方答案无末尾斜杠，因此严格字符串匹配为 false，仅忽略末尾斜杠后匹配为 true。查询阶段未改变 URL。测试覆盖类型契约、未来依赖、参数缺失、输出契约、值绑定、多候选传递、短名/FQDN，以及空结果/错误/溢出/缺失输出中止。

该例仅覆盖一个训练路径及一张表，不能据此声称覆盖全量数据或保证自主选路准确。

## LLM 意图识别和 PM 匹配（独立入口）

`retrieval.py` 新增两个阶段，均只接受严格 JSON：

1. LLM 从原始 context/question 提取 known、wanted、constraints、event_context 和 retrieval_query。程序检查概念合法性、实体 ID 唯一性及实体值确实来自原文，并从检索文本去掉具体实体值。
2. 先按 known/wanted 类型过滤 PM，再让 LLM 根据调查关系、背景、约束判断适用性，输出 PM ID 和已有实体 ID 的参数绑定。允许 no_match；程序拒绝虚构 PM、虚构实体绑定、类型不符或漏检约束。

PM 新增 matching 区域：required_known、wanted_concept、retrieval_text、applicability、not_applicable、event_parameters、postconditions。检索入口只需要 URL，三个 Title 是执行参数，不要求新问题预先提供。

SM 新增 event_types：potential_malicious_url_click、suspicious_exploration、malicious_url_click_on_device，对应已登记的 AlertEvidence.Title 值。程序从这些映射填充执行参数，LLM 不能自行生成标题。映射缺失时返回 unresolved_event_mapping，禁止执行。映射仍源于训练环境，不代表适用于所有部署。

当前只有一条 PM，使用 direct_llm 模式，不计算或伪造相似度。MemoryRetriever 支持传入 embed(texts) 回调：按余弦相似度取 Top-K，再由 LLM 判断适用性；向量召回已用确定性假向量测试，尚未调用真实 Embedding 接口。无 Embedding 配置且候选超过 Top-K 时明确报错，不随意截取。

注意 matched 表示流程可作为调查候选，不表示事件关系已经证明。same_device、different_event 等后置条件仍需证据确认；当前原型不会替代 Verifier，也不会自动修改 ITT。无匹配时只返回状态，尚不调用 EM/SM 自动回退分支。

### 运行

```bash
# 不联网的单元测试
.venv/bin/python -m pytest src/memory/longterm_memory/path_memory_v1/tests -q

# 真实 LLM 意图识别和 PM 匹配
.venv/bin/python -m src.memory.longterm_memory.path_memory_v1.test_llm_example --provider fancybag

# 匹配成功后追加本地数据库三步回放
.venv/bin/python -m src.memory.longterm_memory.path_memory_v1.test_llm_example --provider fancybag --replay
```

真实调用脚本只把训练题的 context/question 传入检索器，官方答案只在执行后用于比较。API 超时、连接失败和部分临时 HTTP 错误等待 5–10 秒后最多重试两次；JSON/契约错误直接失败，不执行 SQL。该策略仅在独立测试入口生效，没有改动共享 LLM 或 workflow。

实测结果保存在 llm_test_result.json：Fancybag deepseek-v4.1-flash，2 次 LLM 调用，合计 1761 tokens，正确选择 pm_url_account_device_url；3 次 SQL 每步各 1 行，结果与官方答案忽略末尾斜杠后匹配。严格字符串比较仍为 false。当前单元测试 26 项通过，包括拒绝匹配、伪造绑定、约束漏检、映射缺失、类型过滤、向量排序和非法 JSON 等情形。单样例成功不代表多流程检索准确率。

## 全训练集后续修复

全训练集LTM已完成418条严格SQL路径复现，并将232条顶层PM归并为85条策略卡。使用独立原始日志适配器与MySQL临时投影，未修改workflow或原业务表；详见 [training/README.md](training/README.md)。本页上面的13/26项测试和单题数字是早期原型记录。
