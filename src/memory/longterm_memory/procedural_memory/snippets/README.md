# Procedural Memory：训练集调查片段

> 历史基线：本版先定义 40 个意图规则，再扫描训练 SQL，并非逐条完整阅读后不限数量提取。新的完整轨迹重建结果见 [trajectory_snippets](../trajectory_snippets/README.md)。两版独立保留，均未接入当前 workflow。

此目录是重建后的 Snippet 设计与实现，格式名为 `investigation-snippets-1`，独立于旧版 `excytin_bench` PM v5。已完成离线构建、本地检索、适用性检查、参数化 SQL 编译和训练样例数据库回放。**尚未接入当前 workflow**；当前 workflow 仍使用 Semantic Memory v2。

## 1. 记忆单位和检索入口

一条记忆对应“起点实体类型 + 一个明确的调查意图”。同一条记录绑定 Planner 动作、Executor SQL 模板、输入输出和注意事项，不让 Planner 从两份独立列表中拼接步骤。

- `trigger_entity`：概念和实体 kind，例如 `Email_ID / network_message_id`。
- `intent / intent_aliases`：规范意图及明确的中英文别名。
- `applicability`：环境、所需输入、标识类型与目标 schema 校验要求。
- `steps`：Planner 动作、输出字段语义、SQL 模板及其输入规格。
- `caution / fallback`：空结果、多候选、截断、schema 不符及执行错误的处理。
- `status`：`active` 或 `candidate`。它表示训练支持范围，不是正确率。

现有 ITT 使用 G→E→T；这里的起点对应用户所说的 F/frontier，即当前待拓展的 E 实体，不新增第四类 ITT 节点。

### 邮件 ID 查发件人示例

以下为目录中片段的精简展示；模板 ID 由构建程序根据 SQL 与参数类型生成。

```yaml
id: proc_mail_sender
trigger_entity:
  concept: Email_ID
  kind: network_message_id
intent: 查询邮件发件人
applicability:
  environment: excytin-azure-mysql
  required_inputs: [value]
  target_schema_check: required
steps:
  - id: lookup
    depends_on: []
    planner_guide: 在 EmailEvents 按 NetworkMessageId 查询 SenderFromAddress，并保留关联键。
    outputs:
      SenderFromAddress:
        entity_concept: Email_Sender
        entity_kind: header_from
    executor_sql_templates:
      - inputs:
          value:
            field: NetworkMessageId
            type: string
            operator: EQ
            source: trigger.value
        sql: SELECT NetworkMessageId, SenderFromAddress FROM EmailEvents WHERE NetworkMessageId = :value LIMIT 100
caution:
  - From 信头地址不等于信封地址，也不证明真实操作者身份。
  - 多条结果保留为候选；空结果不证明邮件不存在。
```

同一片段可能有 EQ、IN、LIKE 等模板变体，这些是**替代查询方式**，不是要全部执行的任务。每个变体单独记录训练支持，Planner 根据已知输入形式选择；只需要一条查询时只建立一个 T。

首批 40 条均为原子调查片段，不把不同轨迹中的查询拼成未经证实的多步流程。跨表调查通过新实体继续检索来组合：URL → `proc_url_mail_ids` → 多个 `Email_ID/network_message_id` → `proc_mail_sender`。返回字段的 `entity_concept/entity_kind` 显式传递标识语义；没有明确实体映射的字段保留为空，作为事实交给 Reviewer，不猜测其类型。后续若沉淀固定多步 Snippet，需要逐步补齐真实顺序、输出到输入的绑定与分支证据；当前加载器明确拒绝多步记录。

## 2. 训练构建方式

训练问题目录包含 **418 题**；已有成功轨迹可与其中 **237 题**唯一对齐，包含 **1,605 次 SQL 尝试**。本轮读取这些训练材料，不使用 test 问题、test 答案或之前测试中的失败案例作为片段证据。未有轨迹支持的其余题目不依据答案臆造 SQL。

训练轨迹发布文件存在 top-level `key` 与对话错配问题，因此沿用项目已有对齐器：以真实首条 user prompt 中的题目匹配 train。构建复用旧代码中的训练解析和 AST 参数化函数，不读取旧 PM 产物来冒充新构建，也不依赖在线 EM 服务。

步骤：

1. 在 `definitions.py` 声明调查意图、起点字段、目标输出及注意事项；这里不填写 SQL 或案件值。
2. 扫描全部对齐训练尝试；只接受成功且有行返回的单表过滤 SELECT。
3. 本版提取一个过滤字段、一个输入参数的原子操作，支持 EQ、IN、LIKE。保留训练 SQL 的比较方式、排序及 DISTINCT；不把 OR 改成 AND，不将 LIKE 擅自改成等值。
4. 每条支持 SQL 必须实际投影该片段要求的全部输出字段；训练中的 SELECT * 可支持其真实 schema 字段。不同 SQL 各自只返回一部分字段时，不将其并集假装成一条完整支持证据。
5. 用 `:value` 替换具体值，显式选择需要的列，加上原过滤字段作关联键，统一 `LIMIT 100`。关联键是经 schema 检查增加的投影，审计单独记录。
6. AST 校验单条只读 SELECT、真实表字段、输入参数、输出字段、LIMIT 与没有案件常量。复用模板的 input type 也纳入模板标识。
7. 每个模板至少 2 个 episode、2 个 incident 支持才标记 active；其余有证据的模板保留 candidate。片段至少有一个 active 模板才为 active。
8. 使用原训练参数在相应 incident MySQL 中做 EXPLAIN 和 SELECT；每个模板优先取不同事件、不同 episode，最多 2 组样例。

运行时 `snippets.json` 不保存训练问题、答案、SQL 实参或日志行。`build_audit.json` 单独保存来源文件摘要、episode/attempt 引用、支持计数、拒绝原因与构建策略；不注入 Agent 提示词。精简 schema 快照供本地检查，亦不发送给 Planner。

## 3. 快通道与回退协议

```text
新 E/F 实体 + 当前调查意图
  → SnippetMemory.retrieve
  → 概念 + 意图/别名匹配
  → 检查标识 kind、环境、模板状态、目标 schema
  → 返回少量 Planner guide、输出语义、可选模板输入规格
  → Planner 选定 snippet_id / template_id，建立 T，提供已知参数
  → 程序按 ID 取 canonical 模板，再次检查并单独绑定参数
  → 执行 → Reviewer 提取事实 → Verifier 判定 → 新实体继续检索
```

- **意图检索**采用规范意图、片段 ID 或明确别名的精确匹配，忽略空格、下划线、连字符和英文大小写。当前不使用向量相似度或未经校准的阈值；自由文本未匹配时返回 `route: semantic`。Planner 接入时需把本轮意图归一为明确的短意图。这是一种保守的基础实现，尚未评估自然语言召回率。
- **标识类型**：邮件 ID、设备 ID、用户对象 ID、哈希必须明确 kind，类型不符不执行。IP 仍遵循 SM v2 的宽概念思路，Source_IP/Login_IP 等可归一为 IP，通过调查意图选择片段；返回物理角色说明，不把不同角色 IP 宣称为同一实体关系。
- **省去 SM 选表**：仅在已有片段适用时跳过模型层面的 SM 选表。执行前仍须检查目标库存在模板要求的表字段。当前在线检查是字段存在性检查；字段改名、消失会触发回退，但不能自动识别字段语义已改变。
- **没有目标 schema**：检索可提供建议，但标记 `needs_live_schema`；`prepare` 拒绝执行。空 schema 也不能回退成离线快照并伪装为检查通过。
- **候选片段**：默认不检索、不执行，只有显式 `include_candidates=True` 的研究验证调用才可使用。
- **输入缺失**：不得造值；Planner 先获取实体。一个 IN 模板接收列表，单实体可显式包装为长度 1 的列表。LIKE 必须显式给出模式，不自动增加 `%`。
- **多值和行数**：每次最多绑定 50 个列表值；`prepare_batches` 对最多 1,000 个输入分批，先校验所有批次再交付执行。必须执行全部批次并保持行与实体关联；任一批失败或达到 100 行时，整体结果仍不完整，不能据此穷尽排除实体。
- **回退**：未命中、不支持的意图、环境/schema 不符 → SM；缺少参数 → Planner 获取输入；空结果 → 核对实体与日志覆盖后 SM；多候选 → Planner 消歧；SQL 报错 → 保留失败模板和错误，按现有有限重试策略处理，仍失败则 SM。网络类错误继续由 workflow 现有重试机制负责，不修改记忆内容。
- **禁止静默删条件**：模板无法适用时切换路线；不要为了通过校验移除关键过滤条件或改查别的表。SM 未覆盖的表字段仍需元数据探查或更新目录，不能保证回退总能完成任务。

`retrieve` 返回路由建议；本模块不会自行调用 SM、LLM、数据库或修改 ITT。多条任务或查询后的回退调度属于未来 workflow 接入工作。

## 4. Executor 参数化接口

`prepare` 返回 `sql` 和 `params` 两个对象，SQL 使用 MySQL connector 的 `%s` 占位符。SQL 标识符来自本地已校验模板，值通过 `cursor.execute(sql, params)` 传递；不使用 Python 字符串插值或让 LLM 重写模板。

```python
from src.memory.longterm_memory.procedural_memory.snippets import SnippetMemory

memory = SnippetMemory.load()
view = memory.retrieve(
    trigger_entity="邮件ID", kind="network_message_id", intent="发件人",
    schema=target_schema,
)
# Planner 从 view 中选择 template_id；程序保留 canonical 内容，避免模型篡改 SQL。
prepared = memory.prepare(
    snippet_id="proc_mail_sender", template_id=selected_template_id,
    bindings={"value": known_network_message_id}, kind="network_message_id",
    schema=target_schema,
)
cursor.execute(prepared["sql"], prepared["params"])
```

未来 T 的 metadata 应由程序写入 `snippet_id、version、template_id、catalog_id、bindings、实体证据引用`，让重放可确认所用模板版本。Planner 不复制 SQL，不输出 `semantic_refs`。如果 fast path 直接绑定 canonical 模板，Executor 无需额外 LLM 请求；未命中则保留现有 SM→Executor 生成 SQL 路径。当前 workflow 的数据库执行协议还需要接收 SQL+params，因此本次没有直接打开 PM 开关。

## 5. 文件和复现

- [catalog.md](catalog.md)：40 条片段目录、类型、目标表、训练支持和状态。
- [excytin/snippets.json](excytin/snippets.json)：在线片段与精简 schema。
- [excytin/build_audit.json](excytin/build_audit.json)：训练来源和构建审计。
- [excytin/validation_report.json](excytin/validation_report.json)：训练回放与检索示例验证摘要。
- `definitions.py / build.py / store.py`：意图定义、构建器、检索与执行准备接口。
- `scripts/check_procedural_snippets.py`：真实 MySQL 只读回放。

```bash
# 分批构建：10、20、30、40；每次对相同 train 输入确定性重建前 N 条
uv run python -m src.memory.longterm_memory.procedural_memory.snippets.build --through 40
# 数据库密码从 docker/excytin-mysql/.env 读取，不进入报告
uv run python scripts/check_procedural_snippets.py --output runtime/procedural_memory_v2/replay_final.json --publish
uv run --with pytest python -m pytest tests/test_procedural_snippets.py -q
```

## 6. 验证边界

首批 40 条片段包含 61 个模板，16 条 active、24 条 candidate。候选被默认停用，所以“库中有 40 条”不等于线上可直接调用 40 条。

61 个模板中 20 个 active、41 个 candidate；来源引用去重后覆盖 105 个训练 episode、134 次成功查询、8 个 incident。同一训练步骤可支持不同调查意图，因此这些计数不应相加成独立样例数。

86 组训练参数回放均通过，每组包含 EXPLAIN 和真实 SELECT；109 个 IP 的列表分为 3 次查询。分批前的一次参数上限拒绝保留在 `runtime/procedural_memory_v2/replay_21_30.json`，最终回放报告另存，不覆盖首次失败证据。

共执行 88 条 SELECT 和对应的 88 条 EXPLAIN。2 组样例触及单批 100 行上限，报告已标记 `possibly_truncated`；它们通过的是 SQL 可执行性检查，不能当作完整调查结果。

10 个确定性检索/回退示例覆盖邮件 ID 类型、IP 角色提示、URL 点击与邮件关联、未知 DLP 意图、缺少 schema 和环境不符。新增 25 项单元测试通过；完整回归共 447 项通过（排除依赖外部 Splunk 的集成文件，6 条已有依赖弃用警告）。

这些验证证明模板在训练样例上的可执行性、参数和字段契约及本地检索路由。没有进行 Fancybag/Planner 端到端评测，也不证明未见事件准确率、片段选择正确率或最终调查答案正确。首版还不覆盖 JOIN、计算投影、复杂条件、多步固定链，也没有覆盖训练集全部可能的调查需求。
