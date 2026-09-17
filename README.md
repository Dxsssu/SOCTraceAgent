# SOCTraceAgent

SOCTraceAgent 是面向安全告警溯源的多智能体研究原型，由 **Planner、Executor、Reviewer** 围绕渐进式 TTT（Traceback Task Tree，溯源任务树）协作调查。项目包含本地 Splunk/BOTS 调查、ExCyTIn-Bench 测评、三类长期记忆，以及外部 Agent 对比实验。Python 包名和部分运行时标识沿用 `socagent` / `SOCAgent`。

当前主要能力：

- **告警调查**：LLM 规划任务、选择 MCP 工具，通过 Splunk REST API 查询真实日志，逐步复盘和重规划。
- **共享工作记忆**：SQLite 持久化事件、版本化 TTT、执行证据、轮次复盘和审计消息。
- **长期记忆**：按角色注入 Semantic / Episodic / Procedural Memory，支持仓库 JSON 快照和 Neo4j 后端。
- **Web 作战室**：创建和查看事件，展示任务树、执行记录与复盘，通过 Socket.IO 推送更新。
- **测评与基线**：ExCyTIn SQL 调查流程、并行测评、结果落盘，以及 OpenCode、Qwen Code、Cline 和论文 BaselineAgent 的配对实验入口。

## 快速开始

需要 Python **3.13+** 和 [uv](https://docs.astral.sh/uv/)。在仓库根目录执行：

```bash
uv sync
# 仅首次配置时复制；已有 .env 时直接编辑
cp .env.example .env
```

编辑 `.env`，至少配置：

```dotenv
PARATERA_API_KEY=your_api_key
# 首次运行使用仓库快照，无需启动 Neo4j
SOCAGENT_LTM_BACKEND=snapshot
```

然后启动：

```bash
uv run python main.py
```

默认打开 <http://127.0.0.1:5008>。无参数启动会自动初始化 SQLite，在同一进程内启动三个角色的后台线程和 Web 服务。在首页创建事件后，可进入对应 war room 查看调查进展。

启动页面无需准备所有测评服务；**完成 Splunk 调查需要可用的 LLM、Splunk 服务和已导入的日志数据**。Planner / Reviewer 调用失败或输出不合法会显式记录失败，不会自动替换为模拟结果。

也可以分别运行以下入口。分进程模式下，在四个终端分别启动 Web 和三个角色，并使用相同的 `SOCAGENT_DB_PATH`：

```bash
uv run python main.py -init-db        # 仅初始化数据库
uv run python main.py -web            # 仅启动 Web
uv run python main.py -role planner
uv run python main.py -role executor
uv run python main.py -role reviewer
```

角色名也兼容 `_planner`、`_executor`、`_reviewer`。

## 调查架构

```text
告警 Event
  -> Planner：建立最小 TTT，选择 next_task_id
  -> Executor：执行一个 L3，记录 Execution 与证据
  -> Reviewer：生成 RoundReview
  -> Planner：提交版本化增量更新
  -> 下一个 L3 / 目标解决 / 调查失败
```

| 角色 | 职责 | 长期记忆权限 |
| --- | --- | --- |
| Planner | 建立目标与问题、展开任务、选择下一步、更新 TTT | PM + SM |
| Executor | 生成和执行查询、记录真实 observation | 首次仅 SM；ExCyTIn 查询错误或空结果修复时使用 EM |
| Reviewer | 检查证据、结论与缺口，反馈后续调查方向 | SM |

本地调查每轮执行一个 L3，再进入复盘与重规划。业务事实以 SQLite 持久化状态为准，`MessageEnvelope` 是审计与展示层。默认数据库为 `runtime/socagent.db`，包含 `events`、`ttt_snapshots`、`executions`、`round_reviews`、`messages`。

```text
pending -> planned -> executing -> reviewing -> replanning
              ^                                    |
              +------------------------------------+
                                                   -> completed / failed
```

`completed` 要求 L1 目标明确为 `resolved`；没有待执行任务不代表成功。角色处理异常也可能将事件置为 `failed`。

## TTT 约束

TTT 随调查逐步展开，普通 SQLite 调查与 ExCyTIn 使用同一套校验逻辑。

- 初始只建立一个固定 L1 目标、少量 L2 问题，通常展开一个 L3。L2 可以暂时没有子任务。
- L1/L2 状态：`open / resolved / blocked / n/a`；L3 状态：`todo / in_progress / done / blocked / n/a`。
- L3 执行结束不会自动解决父问题。空结果不直接否定假设；ExCyTIn 的查询修复保留在原 L3 内。
- 节点保留稳定 `node_id`、显式 `level`、`result_summary`、`evidence_refs`；不使用 `answer_requirements`。
- `evidence_refs` 指向真实执行记录：普通调查使用 Execution UUID，ExCyTIn 使用单次调查内唯一的 `E{step_no}`。引用需结合 event_id 查找，SM 描述不是案件证据。
- Planner 用 `next_task_id` 指定下一项可执行 L3，用 `selection_reason` 说明理由；不再依照编号排序。
- 初始化输出完整树，后续只输出 `base_version + updates + next_task_id`。支持 `add_node` 和 `update_node`，不允许删除节点、改 ID 或改目标；重开节点必须说明原因。
- Workflow 校验版本、层级、引用和任务选择后生成完整快照。SQLite 保存 schema 2.0 快照，写入前检查期望版本，旧快照仍可读取。
- 目标仍 open 且没有可执行任务时必须继续展开或明确 blocked。普通调查将阻塞/非法计划标记为 failed；ExCyTIn 允许明确阻塞或预算耗尽时提交已有信息，这不表示目标已经解决。

例如，首次查告警取得进程 ID 后，再在“创建时间是什么”问题下追加任务：

```yaml
response_type: TTT_UPDATE
base_version: 5  # 必须与输入快照一致
updates:
  - operation: add_node
    parent_id: Q2
    node:
      node_id: T2
      level: 3
      title: 读取已定位进程的创建记录
      status: todo
      result_summary: ""
      evidence_refs: []
      children: []
next_task_id: T2
selection_reason: E1 已提供进程 ID，当前仍缺少创建时间
```

## Splunk / BOTS 调查

本地 Executor 从注册的 MCP 工具描述中通过 LLM 选择工具。当前日志查询工具为 `log_search`，由 [src/tools/splunk.py](src/tools/splunk.py) 将自然语言意图转换为查询规格与 SPL，再调用 Splunk REST API，返回摘要和样本事件。

BOTS Docker 配置见 [data/splunk-bots-docker](data/splunk-bots-docker/README.md)。准备好对应应用包和数据后，可先启动 bots1：

```bash
docker compose -f data/splunk-bots-docker/docker-compose.yml up -d bots1
```

首次启动需要下载、导入数据，容器启动不代表索引已就绪。根目录 `.env` 中的用户名、密码需与实际部署一致；bots1 推荐使用管理 API 地址：

```dotenv
SPLUNK_DEFAULT_DATASET=botsv1
SPLUNK_BOTSV1_BASE_URL=https://127.0.0.1:8089
SPLUNK_BOTSV1_INDEX=botsv1
```

`8000` 是 bots1 的 Web 端口，`8089` 是管理 API 端口。现有 bots2 / bots3 配置使用 Web 端口 `8020` / `8030`，工具包含 Web 代理路径回退；若部署不支持这些代理路径，需要映射其管理 API 并修改对应 `BASE_URL`。

通过首页或 API 创建事件，例如：

```bash
curl -X POST http://127.0.0.1:5008/api/event/create \
  -H 'Content-Type: application/json' \
  -d '{"event_name":"DNS 日志调查","message":"查询 botsv1 的 DNS 请求日志，分析源 IP、目的 IP 与域名。","context":{"splunk_dataset":"botsv1"},"severity":"medium"}'
```

Web 提供事件列表、详情、执行记录、复盘与层级查询；具体路由见 [src/webapp/server.py](src/webapp/server.py)。实时消息来自本地 SQLite watcher。

## 长期记忆

长期记忆由 [MemoryContextService](src/workflow/context.py) 检索和裁剪后注入角色，不向 Agent 暴露通用 Cypher 或数据库访问接口。

| 类型 | 内容与来源 | 详细说明 |
| --- | --- | --- |
| Semantic Memory（SM） | 从实际 schema 汇总表、字段、类型和关联键，提供中英文描述 | [构建与导入](src/memory/longterm_memory/semantic_memory/excytin_bench/README.md) |
| Episodic Memory（EM） | 来自训练轨迹的脱敏 SQL 尝试、错误和修复关系 | [构建与导入](src/memory/longterm_memory/episodic_memory/excytin_bench/README.md) |
| Procedural Memory（PM） | 调查阶段、流程和适用条件，连接 schema 与训练经验 | [构建与导入](src/memory/longterm_memory/procedural_memory/excytin_bench/README.md) |

普通事件通过 `context.memory_profile` 指定 `excytin_bench`，或设置默认 `SOCAGENT_MEMORY_PROFILE=excytin_bench`，才注入该 Profile 的记忆；事件配置优先。不设置时，普通 Splunk 调查不会注入 ExCyTIn 记忆。指定 Profile 不会把本地 Splunk Executor 切换为 MySQL 测评流程。

后端通过 `SOCAGENT_LTM_BACKEND` 选择：

- `snapshot`：读取仓库内的 JSON 快照，无需 Neo4j。
- `neo4j`：从已导入的 Neo4j 图加载，连接失败会报错。
- `auto`：存在非空 `NEO4J_PASSWORD` 时尝试 Neo4j，连接失败回退到快照；否则直接读取快照。

`.env.example` 含 Neo4j 占位密码，首次运行建议显式使用 `snapshot`。使用图后端时，按 [Neo4j 部署说明](docker/neo4j/README.md) 启动服务，将连接配置导出到进程或写入根目录 `.env`，再依次导入 **SM → EM → PM**。重建 JSON 不会自动更新 Neo4j。

当前检索使用 schema、文本匹配和关联关系；虽然 `LLMClient.embed()` 已提供 embedding 调用入口，长期记忆检索尚未使用向量检索。

## ExCyTIn-Bench 测评

测评使用独立的同步 workflow，不通过 Web 或后台 SQLite 轮询运行：

```text
context + question -> Planner / TTT -> Executor 生成只读 SQL
    -> 环境执行 -> observation -> Reviewer -> Planner 更新 / 提交答案
```

[src/benchmarks/excytin_bench/agent.py](src/benchmarks/excytin_bench/agent.py) 提供 SecGym 的 `reset()`、`act()`、`get_logging()` 接口，可由官方 `ExcytinEnv.step()` 执行 SQL。仓库自己的 runner 使用兼容的 MySQL 执行边界，负责抽题、并行运行和落盘；应区分自有 runner 与官方环境的成绩。

运行前需准备：

1. 测试题目：默认目录 `data/excytin-bench/huggingface/questions/test/`。数据目录不纳入 Git，需要单独准备上游数据；当前加载器要求八个 incident 的题目文件齐全。
2. MySQL 数据：按 [MySQL 部署说明](docker/excytin-mysql/README.md) 准备数据、生成导入文件并启动所需 incident 容器。
3. LLM 配置，以及 JSON 快照或已导入的 Neo4j 记忆。

使用快照后端随机抽取 10 题：

```bash
set -a
source docker/excytin-mysql/.env
set +a

SOCAGENT_LTM_BACKEND=snapshot uv run python -m src.benchmarks.excytin_bench.runner \
  --sample-size 10 --seed 42 --max-steps 25 --workers 3 --no-llm-eval
```

可用 `--incident incident_5` 限定抽题范围，`--question-dir` 和 `--output-dir` 覆盖输入、输出目录。runner **默认开启 LLM 裁判**；上例显式关闭，仅计算匹配和查询指标。需要模糊答案与部分步骤评分时改为 `--llm-eval`，会额外调用模型。

默认每题最多 25 个 action，预留最后一步提交答案，即最多 24 次 SQL。每个 L3 内最多尝试 3 条 SQL；首次仅使用 SM，SQL error 或空结果后才检索 EM 辅助修复。Reviewer 判断答案证据充分时可直接提交；预算耗尽或明确阻塞时也可提交已有信息，这不代表目标已解决。

每次运行输出到 `runtime/excytin_bench/runs/<run-id>/`：

```text
manifest.json       # 抽题、种子与运行配置
results.jsonl       # 逐题结果
summary.json        # 汇总指标
summary.md          # 可读结果表
cases/*.json        # action / observation、TTT、Review、记忆命中
```

指标包括 Exact / Containment Match、SQL 成功率、schema 错误率、空结果率、修复率、动作数、记忆命中和用时。包含匹配不能直接当作正确率；LLM 评分只有在环境、裁判模型与配置一致时才适合与论文成绩比较。完整协议见 [ExCyTIn Workflow 文档](src/benchmarks/excytin_bench/README.md)。

## 外部 Agent 基线

[external_agents](external_agents/README.md) 管理 OpenCode、Qwen Code、Cline 的原生 CLI 安装、版本和实验结果。它们通过统一 MCP 接口执行只读 SQL、提交答案，不注入本项目的 SM / PM / EM。

除 Python 环境外，需要 Node.js **22+**、npm、Git，以及对应题目和 MySQL 数据：

```bash
uv run python external_agents/bootstrap.py
uv run python -m src.benchmarks.external_agents.runner \
  --agents opencode qwen-code cline --incident incident_5 --qid 39 \
  --workers 3 --max-steps 25 --llm-eval
```

外部 CLI runner 不传 `--llm-eval` 时默认不调用裁判。每题使用独立会话和临时配置，模型请求通过本地兼容代理。批量入口 `src.benchmarks.external_agents.batch` 支持三个 CLI、论文 `BaselineAgent` 和 SOCTraceAgent 的五框架配对实验，`src.benchmarks.external_agents.analyze` 汇总结果与成本。版本条件、论文额外依赖和断点复用规则见[基线文档](external_agents/README.md)。

## 配置参考

配置从根目录 `.env` 加载，完整示例见 [.env.example](.env.example)，依赖以 [pyproject.toml](pyproject.toml) 和 `uv.lock` 为准。

| 配置 | 默认值 / 用途 |
| --- | --- |
| `PARATERA_API_KEY` | LLM API 密钥，实际模型调用必需 |
| `PARATERA_BASE_URL` / `PARATERA_MODEL` | `https://ai.paratera.com/v1/` / `DeepSeek-V4.1-Flash` |
| `PARATERA_REASONING_EFFORT` / `PARATERA_THINKING_ENABLED` | `low` / `false` |
| `PARATERA_REQUEST_TIMEOUT_SECONDS` / `PARATERA_MAX_RETRIES` | `120` 秒 / `0` 次自动重试 |
| `PARATERA_EMBEDDING_MODEL` | `GLM-Embedding-3` |
| `SOCAGENT_DB_PATH` / `SOCAGENT_POLL_INTERVAL` | `runtime/socagent.db` / `5` 秒 |
| `SOCAGENT_WEB_HOST` / `SOCAGENT_WEB_PORT` | `127.0.0.1` / `5008` |
| `SOCAGENT_MEMORY_PROFILE` | 默认空；当前支持 `excytin_bench` |
| `SOCAGENT_LTM_BACKEND` | 默认 `auto`；快速开始推荐 `snapshot` |
| `NEO4J_URI` / `NEO4J_USERNAME` / `NEO4J_PASSWORD` / `NEO4J_DATABASE` | 图记忆连接配置 |
| `SPLUNK_USERNAME` / `SPLUNK_PASSWORD` / `SPLUNK_VERIFY_TLS` | Splunk 身份验证与 TLS 设置 |
| `SPLUNK_BOTSV{1,2,3}_BASE_URL` / `SPLUNK_BOTSV{1,2,3}_INDEX` | 各数据集端点与索引 |
| `SOCAGENT_BENCHMARK_WORKERS` | 自有测评 CLI 默认并行数 `3` |
| `PARATERA_RPM_LIMIT` / `PARATERA_TPM_LIMIT` | `2500` / `5000000` |
| `PARATERA_RATE_UTILIZATION` | `0.8`，限流预算使用比例 |
| `PARATERA_OUTPUT_TOKEN_RESERVE` | `16384`，请求前预留的输出 token 估算 |

统一聊天入口共享**进程内**限流器，完成后按 API usage 修正预算。token 预留不是输出长度上限，也不能保证绝不触发 429。分别启动的进程不共享额度；embedding 调用不经过该聊天限流器。

## 测试与验证

项目已有 pytest / unittest 用例，覆盖 TTT 增量更新、角色记忆权限、三类记忆、测评 workflow / runner、LLM 配置与限流、外部 Agent 适配。

运行离线测试，排除需要真实 LLM 和 Splunk 的集成用例：

```bash
uv run --with pytest python -m pytest tests -q \
  --ignore=tests/test_splunk_ttt_integration.py
```

`pytest` 尚未列入项目依赖，以上通过 `--with pytest` 临时提供。部分记忆重建测试需要本地上游数据，缺失时会跳过。

准备好真实服务后，再运行以下联调。它们会发起真实 LLM / Splunk 请求：

```bash
uv run python tests/test_splunk_ttt_integration.py
uv run python tests/test_multi_agent_loop.py --timeout 240 --target-rounds 2
```

多轮烟雾脚本会创建测试数据库、启动角色进程并轮询闭环状态；可用 `--show-process-logs` 查看角色日志。

## 目录导航

```text
SOCTraceAgent/
├── main.py                         # 一体启动 / 单角色 / Web / 初始化入口
├── src/
│   ├── agent/                      # Planner、Executor、Reviewer、LLM 与限流
│   ├── workflow/                   # 本地流程装配、角色记忆视图
│   ├── schema/                     # Event、TTT、版本化 updates、Execution、Review
│   ├── memory/
│   │   ├── working_memory/         # SQLite TTT 快照
│   │   └── longterm_memory/        # SM / EM / PM 快照、构建、Neo4j 导入
│   ├── tools/                      # MCP 注册与 Splunk 查询
│   ├── benchmarks/
│   │   ├── excytin_bench/          # 同步 workflow、SecGym 适配、自有 runner
│   │   └── external_agents/        # CLI 代理、MCP、批量评测与分析
│   ├── storage/                    # SQLite 业务状态
│   ├── messaging/                  # 结构化消息与消息总线
│   └── webapp/                     # Flask + Socket.IO、模板与静态资源
├── external_agents/                # 基线安装脚本、版本与本地运行目录
├── docker/                         # ExCyTIn MySQL 与 Neo4j 部署
├── data/                           # BOTS 配置与本地外部数据
├── tests/                          # 离线用例、真实服务集成与烟雾脚本
├── docs/                           # 研究记录和架构材料
└── runtime/                        # 本地数据库、日志与测评产物（不提交）
```

通信协议见 [src/messaging/README.md](src/messaging/README.md)。

## 当前边界

- 本项目是单机研究原型；SQLite 共享状态与 watcher 尚不面向分布式调度。
- 一体启动使用后台线程，分进程运行仍需自行管理进程生命周期。
- Web 尚无用户认证与权限控制，默认用于本地调试。
- 本地调查的真实工具集中于 Splunk；资产查询、威胁情报等能力尚未接入。
- 外部数据、数据库服务和原生 CLI 需要单独准备；`uv sync` 只安装 Python 项目依赖。
- 原生 CLI 基线使用统一代理、工具和预算约束，比较结果需同时报告这些条件以及各框架是否使用长期记忆。
