# ExCyTIn Semantic Memory：L1 / L2

SM 采用本地 YAML 两层存储，不再保存聚合 `semantic_memory.json`，在线也不从 Neo4j 读取 SM。

- `l1.yaml`：全局表目录，`log_sources` 中每项只有 `table / desc / entities`。覆盖 ExCyTIn 建表 SQL 中实际可查询的 58 张表；用途与实体标签由逐表审核的 `catalog_annotations.json` 提供，每个实体标签都有具体字段作为依据。
- `l2/<精确表名>.yaml`：单表深度字典，只有 `table_name / core_columns`。每个字段只有 `name / type / desc`。共 1,882 个字段，完整保留，`core_columns` 不代表只截取部分字段。
- `build_manifest.json`：单独保存源 `.meta` 类型、构建来源、schema 哈希、各 incident 可用表与排除表，不注入 Agent。`catalog` 保留离线 PM/EM 的源 schema 统计，`query_catalog` 才是在线可查询统计。
- `descriptions.json`：离线构建的已审核说明来源，保留中英文解释和参考链接。

L1 示例（来自实际构建）：

```yaml
log_sources:
- table: EmailEvents
  desc: 记录 Microsoft Defender for Office 365 的邮件处理信息，包括发件人、收件人、身份验证结果、威胁判定、投递操作及位置。
  entities: [domain, ip, mail_id, user, workspace]
```

L2 的 `type` 表示 ExCyTIn 实际 MySQL 列类型。8 份原始建表 SQL 的所有列均为 `TEXT`，本项目导入脚本采用相同规则；原始 Azure 的 `string/datetime/dynamic` 等类型放在 `desc` 中说明，不混作 SQL 类型。字段解释保留目标/发起进程等角色差异。

原始 `.meta` 包含 60 张表，但原始 SQL 和本项目导入流程均排除 `AzureDiagnostics`、`LAQueryLogs`，因此它们不进入 L1/L2。在线目录是 8 个 incident 的可查询 schema 并集，不保证单个 incident 都有全部表或字段。`TenantId` 在这些日志中表示工作区，以 `workspace` 标注；`tenant` 只在存在目录租户字段时使用。资源 URI 不标为被访问的 URL。

## 构建

```bash
uv run python -m src.memory.longterm_memory.semantic_memory.excytin_bench.build_knowledge
# 自定义输出：L2 会写到该 L1 文件旁边的 l2/ 目录
uv run python -m src.memory.longterm_memory.semantic_memory.excytin_bench.build_knowledge --output /tmp/sm/l1.yaml
```

构建器读取 `data/excytin-bench/huggingface/data.zip` 的 365 份 `.meta` 和 8 份原始 SQL，校验可查询表、SQL 类型、字段说明和实体标签依据，不读取日志行或案件答案。除 manifest 的生成时间外，产物可重复构建。

## 在线读取

`SemanticMemoryStore.catalog()` 仅读取 L1；`read_table("DeviceProcessEvents")` 严格核对 L1 中的精确表名，再读取对应的单张 L2。未知表、大小写错误、路径穿越、重复字段或卡片身份不一致都会拒绝。

PM 未命中时，选择助手接收 question、context 和完整 L1（含 desc、entities），一次返回一个精确表名或 null。程序读取完整 L2，向 Planner 提供字段名、类型和解释。L1 不删掉尾部表，L2 不按字节预算截断。选择助手输出最多 192 token、关闭 thinking；非法输出不重试，不加载其他表。相同问题与库版本复用缓存。

SM 始终使用本地 L1/L2，`SOCAGENT_LTM_BACKEND` 只控制 PM/EM 的 JSON 或 Neo4j 后端。SM 内容更新后需要重新创建 repository / 重启运行进程。

## 兼容与验证

`store.load_knowledge()` 为 PM/EM 离线构建及旧检索接口临时生成兼容 read model，依据 manifest 的原始 schema 类型保留稳定 ID、schema 哈希和推导关联键，维持离线构建兼容；不再持久化这份聚合视图。PM 的来源摘要覆盖 L1 和全部 L2。旧 `import_neo4j.py` 仅保留为历史图导出工具，正常运行和 PM/EM 导入均不需要导入 SM 图。

```bash
uv run --with pytest python -m pytest tests/test_sm_fallback.py tests/test_excytin_semantic_memory.py tests/test_excytin_procedural_memory.py tests/test_excytin_episodic_memory.py -q
```

测试校验与原始 `.meta` 的全部表字段一致、构建可重复、按表精确加载、完整字段解释进入 Planner，以及 PM/EM 构建兼容性。

## 实际数据审计

```bash
uv run python -m src.memory.longterm_memory.semantic_memory.excytin_bench.audit
```

`audit_report.json` 记录与 8 份建表 SQL、394 个 CSV 表头及 `.meta` 的核验结果。L1 全部实体标注已逐表检查字段依据；L2 字段名和类型全量核验，现有字段语义说明仅重点复核，不能把结构测试通过解释为所有语义已被外部验证。审计未连接运行中的数据库，也不验证所有日志行。
