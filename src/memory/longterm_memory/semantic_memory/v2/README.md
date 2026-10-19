# Semantic Memory v2：宽概念召回、调查意图排序

更新：2026-10-18。当前 MySQL workflow 默认启用；旧 SM、PM、EM 仍关闭，本地 Splunk workflow 未接入。

## 核心结构

保留两张扁平字典，运行时只读取 `excytin/concepts.json` 与 `excytin/tables.json`。不增加记忆 Agent、向量数据库或概念图谱，不把完整 schema 注入角色。

- 概念字典：concept → aliases、fields；每条映射包含 table、field、type、kind、meaning。
- 表简介字典：table → description、time_field、intent_keywords、intent_priority。
- 两份文件共享 environment_id、catalog_id；内容变化后重建标识，加载时检查配套关系。

当前覆盖环境资料中的全部 **58 张表、1882 个表字段**，共 2115 条映射。常用实体概念之外，每个字段都有 `Field:字段名` 物理检索入口（例如 `Field:Entities`）；其说明来自对应表的 L2 字段资料，不推断同名字段可关联。物理索引不进入 Planner 初始概念词表，只在请求时返回相关字段。

告警、身份、云活动等表补入邮件 ID、主机、账户、IP、文件等常用概念；增加 Alert_ID、Alert_Title、Action_Type。告警 IP 保留证据角色，告警时间不映射成 Login_Time。目标数据库中不存在的表或字段仍由实时 schema 过滤。

覆盖检查：`.venv/bin/python scripts/audit_semantic_coverage.py --mysql-ports 3306 3307 3308 3309 3310 3311 3312 3313`。该命令检查完整源 schema、所有 PM SQL 模板引用、指定实时数据库，发现遗漏退出非零；结果保存到 `runtime/semantic_memory_v2/coverage.json`。回归测试检查每一个源字段都能检索到，并检查 PM 覆盖。新增表或字段后应更新 L2 资料及表说明，重新运行 `python -m src.memory.longterm_memory.semantic_memory.v2.build` 和覆盖检查。

## 从错误 IP 分类恢复

Source_IP、Login_IP、Remote_IP、Local_IP、Destination_IP 合并为宽概念 IP，并保留为别名。每个物理字段仍有独立 kind 与 meaning：

| 表 | 字段 | kind / 含义 |
|---|---|---|
| SigninLogs | IPAddress | login_ip：身份验证客户端 IP |
| DeviceNetworkEvents | RemoteIP | remote_ip：网络连接远端 IP |
| DeviceNetworkEvents | LocalIP | local_ip：本地 IP |
| AZFWNetworkRule | SourceIp | source_ip：防火墙流量源 IP |
| UrlClickEvents | IPAddress | click_ip：点击事件客户端 IP |

IP 使用 `kind_policy: hint`。即使输入 Source_IP/source_ip，登录表仍可以被召回。提示只影响次级排序，不把不同字段角色视为可互换值。

邮件 ID 与哈希算法仍保留严格 kind：InternetMessageId 不等于 NetworkMessageId，SHA1 不等于 SHA256。未澄清的已知 Email_ID/File_Hash 不自动绑定给 Executor。其他候选通过字段含义说明观察角色，不能由宽概念推断 JOIN 关系。

## 检索规则

```yaml
semantic_query:
  known: [IP]
  wanted: [Login_Time, Application]
  intent: 查询指定 IP 的 SAP 身份验证时间和目标应用
```

1. known 至少一个概念；wanted 可为空。支持字符串或 `{concept: Email_ID, kind: network_message_id}`。intent 可省略，workflow 自动保留原始 question 作为排序上下文。
2. 通过概念或别名召回至少包含一个 known 字段的表。IP 角色不硬过滤；其他明确 kind 仍按其含义过滤。
3. 按目标数据库 schema 快照剔除不存在的表和字段，使用数据库真实类型。完整快照仅在程序内使用。
4. 表内只投影 known/wanted 对应字段及时间字段。全部概念覆盖记为 direct，否则 partial，显式列出 missing、unresolved、ambiguities。
5. 基于表简介字典的人工维护关键词进行确定性意图排序：匹配词数量封顶 3，每词 10 分，有命中时应用表优先级；再按覆盖程度、缺失数量、角色提示匹配和表名排序。分数是规则排序依据，不是置信度或概率。英文使用词边界，中文使用短语匹配。
6. 每次最多返回 5 张表，提供 total/has_more/next_offset，可继续翻页。意图不匹配不会删除表，未知概念不会自动扩展成全量 schema。

认证意图优先 SigninLogs；终端网络连接优先 DeviceNetworkEvents；防火墙意图优先 AZFW 表。规则不是自然语言理解模型，不保证覆盖所有同义表达，需要维护词汇并由 Planner 阅读简介做最终选择。

Login_Time → CreatedDateTime 表示身份验证事件创建时间；Event_Time 和表简介 time_field → TimeGenerated 表示日志生成时间。Process_Start → ProcessCreationTime。不能把这些时间无条件互换，也不能凭背景日期虚构精确查询范围。

## Planner 与 Executor 接入

```text
E 实体 + question / 本轮调查意图
  → Planner semantic_query
  → 本地召回与排序，返回候选简介和相关字段
  → Planner operations / add_task.table
  → 程序绑定所选表已经检索到的字段上下文
  → Executor 生成 SQL
  → 只读、表名及字段校验 → MySQL
  → Reviewer → Verifier → Planner
```

Planner 普通任务只增加 `table`，不再维护 semantic_refs 字段列表：

```yaml
operations:
  - op: add_task
    entity_id: E1
    table: SigninLogs
    objective: 按已知 IP 查询身份验证时间及目标应用
```

- 程序绑定的 table 是唯一权威选表来源。Planner 可显式填写 table；若省略但 objective 明确点名唯一已检索候选表，程序精确匹配该表，不猜测多个表或未知表。Executor 只看到所选表片段。
- 允许选择 partial 候选做中间调查，例如 URL → 邮件 ID → 发件人。partial 不代表可以凭空补齐缺失字段，也不能把未知条件当成已满足。
- 程序合并同表已检索字段。任务中明确点名的额外物理字段（如 DeviceId）会在同一概念字典内按精确名称、所选表及真实 schema 自动补齐类型和含义，记录 task_resolved_fields；不发送全部字段，也不采信字典外字段。不知道物理字段时仍需检索相应概念。
- 旧 semantic_refs 仍可供保存的调用者兼容使用，但不再要求包含全部 known/wanted 字段；新主提示词不要求输出它。若同时出现 table 与旧引用，二者必须一致。
- 每次规划最多 5 次检索，每个调查最多 32 次。缓存仅限当前调查；新调查/reset 后清空。规划只重发最近一次检索视图，已缓存的历史表仍可绑定。
- 无合适候选时允许显式 schema_probe，只检查限定范围的元数据。探查不会自动扩展持久目录。

SQL 除既有只读检查外，必须使用绑定表和已检索或在任务中明确点名、经字典与 schema 核实的字段。使用 sqlglot 解析物理表与列，支持表别名、输出别名、聚合与 CTE。普通任务拒绝 SELECT *、切换业务表和未检索字段；COUNT(*) 可用。JOIN 不自动推断，当前单步任务仅绑定一张业务表，跨表应分步推进。当前 ExCyTIn 数据库限定为默认库或 env_monitor_db。

字段/表校验错误进入已有的 Executor 有限格式修复流程；若两次响应仍不合法，当前运行报告失败，不无限重试，也不偷偷补入完整 schema。接口错误仍等待 5–10 秒后最多重试两次。数据库 SQL 错误按既有 error 字段传递。

“单次检索”描述常见查询路径，不是所有调查都只能检索一次。概念识别、跨表路径、空结果恢复和证据判断仍是 Agent 的职责；字段存在不证明选中记录与事件有关。

## 构建与验证

离线构建从现有 L2 卡片和 catalog_annotations 读取明确声明的列与含义。没有读取问题答案或日志行；原始案例值不会写入概念字典。运行时不依赖旧 LTM repository。

```bash
uv run python -m src.memory.longterm_memory.semantic_memory.v2.build
uv run --with pytest python -m pytest tests/test_semantic_memory_v2.py tests/test_semantic_workflow_v2.py -q
FANCYBAG_MODEL=deepseek-v4.1-flash uv run python scripts/run_semantic_v2_fancybag.py --cases incident_322-q0000 incident_166-q0032 incident_5-q0026
```

API 测试保存完整角色输入输出、SQL、检索结果和标准答案对照，位于 runtime/semantic_v2_fancybag/ 时间戳目录。答案仅在运行后用于评分，不注入 Agent。必须分别报告检索候选、有效选表、SQL 成功率与答案质量，不能用 SQL 成功代替选表正确。

修改前对照运行：`runtime/semantic_v2_fancybag/20261018-194014/`。这组三题中只有进程题形成有效业务表选择；点击题为检索协议失败，SAP 题为错误概念召回及恢复失败。属于固定案例诊断，不是随机准确率估计。

## 本次修改后的验证结果

完整回归 422 项通过（其中 Semantic Memory 检索与接入相关 44 项）；原有 9 个检索示例与 3 项实际 MySQL 查询检查通过。

Fancybag / deepseek-v4.1-flash 第一轮三题保存于 `runtime/semantic_v2_fancybag/20261018-201628/`，进程题修复接入问题后的针对性复测保存于 `runtime/semantic_v2_fancybag/20261018-202742/`，后者 `assessment.json` 汇总两个阶段并保留首次失败。

| 案例 | 首个业务表 | 实际结果 |
|---|---|---|
| incident_322-q0000 点击者邮箱 | UrlClickEvents | 1 条 SQL，邮箱与标准答案一致 |
| incident_166-q0032 SAP 认证时间 | SigninLogs | 继续查询登录/风险数据源，7 条查询均为空；预算耗尽后报告证据不足 |
| incident_5-q0026 bcdedit 进程时间 | DeviceProcessEvents | 第一轮因字段接入与 Planner 协议错误失败；字段补齐和唯一表名识别调整后复测，5 条 SQL 定位 PID 8136，时间与库记录一致 |

进程题标准答案有 7 位小数，数据库和返回值有 6 位，严格评分未通过。其复测还发生过创建时间与日志时间混用，随后由 Planner 修正。选择用途合理的表不等于保证 SQL 条件正确或答案正确。

第一轮出现一次 502 和一次连接错误，均恢复；进程题复测未记录 API 异常。固定案例只能验证这条路径，不是整体准确率估计。第一轮总 token 为 131,202，针对性复测为 61,120，不能据此声称整轮 token 降低。
