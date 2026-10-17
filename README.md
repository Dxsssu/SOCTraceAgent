# SOCTraceAgent

SOCTraceAgent 是面向安全告警溯源的多智能体研究原型，由 **Planner、Executor、Reviewer** 围绕渐进式 TTT（Traceback Task Tree，溯源任务树）协作调查。项目包含本地 Splunk/BOTS 调查、ExCyTIn-Bench 测评、三类长期记忆，以及外部 Agent 对比实验。Python 包名和部分运行时标识沿用 `socagent` / `SOCAgent`。

当前主要能力：

- **告警调查**：LLM 规划任务、选择 MCP 工具，通过 Splunk REST API 查询真实日志，逐步复盘和重规划。
- **共享工作记忆**：SQLite 持久化事件、版本化 TTT、执行证据、轮次复盘和审计消息。
- **长期记忆**：按角色注入 Semantic / Episodic / Procedural Memory，SM 使用 L1/L2 YAML 文件；PM/EM 支持 JSON 快照和 Neo4j 后端。
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
  -> Planner：接收 context + question 与适用 PM，建立 ITT
  -> Executor：执行一个 L3，记录 Execution 与证据
  -> Reviewer：生成 RoundReview
  -> Planner：提交版本化增量更新
  -> 下一个 L3 / 目标解决 / 调查失败
```

| 角色 | 职责 | 长期记忆权限 |
| --- | --- | --- |
| Planner | 建立目标与问题、展开任务、选择下一步、更新 TTT | PM（含 semantic_refs） |
| Executor | 仅根据当前 T 构造查询；程序执行并回写 ITT | 正常仅使用 T；SQL error 后额外检索最多 1 条 EM |
| Reviewer | 审核真实结果，提供事实、缺口和建议 | 不检索记忆；仅任务树与执行证据 |
| Verifier（SQL workflow） | 每轮 Reviewer 后判断能否回答；能则直接输出答案，否则继续调查 | 不检索记忆 |

本地调查每轮执行一个 L3，再进入复盘与重规划。业务事实以 SQLite 持久化状态为准，`MessageEnvelope` 是审计与展示层。默认数据库为 `runtime/socagent.db`，包含 `events`、`ttt_snapshots`、`executions`、`round_reviews`、`messages`。

```text
pending -> planned -> executing -> reviewing -> replanning
              ^                                    |
              +------------------------------------+
                                                   -> completed / failed
```

`completed` 要求 L1 目标明确为 `resolved`；没有待执行任务不代表成功。角色处理异常也可能将事件置为 `failed`。

## ITT 约束

Planner 输入只保留 `context` 与 `question`。运行时构建事件 ID、轮次等管理信息；`runtime_info` 仅用于运行记录，不传入模型或 PM 选择助手。

PM 选择助手根据 question 和当前进展，从 active 查询模板的精简目录中一次选择最多一个模板。没有多阶段前提审核或独立复核。选中的模板传给 Planner；T 只保存 template_id，程序附加真实模板，Executor 绑定已知参数后由程序生成 SQL。模板只提供查询方法，不能改变 G 或证明答案；无适用 PM 时由轻量 SM 代理提供结构知识。

普通 SQLite 调查与 ExCyTIn 使用相同的 ITT YAML 列表协议：

- G 的 `objective` 由 LLM 从 question 提炼，每个需要独立回答的调查问题对应一个 G，不根据背景额外扩大目标。F 由 LLM 联合初始 context 和 question 初始化，描述服务于该问题的调查方向。每个 G 包含 F，每个 F 至少包含一个 T；不允许第四层。
- 节点字段为 `id`、`objective`、`status`，状态为 `todo/done/failed`。G/F 使用 `children`，T 使用 `semantic_refs`、`execution_code` 和 `execution_result`。
- T 的 `semantic_refs` 为 `[{table: 表名, fields: [字段名]}]`，保存相关表字段，可参考 PM、已有 ITT 知识及真实结构查询结果，结构未知时为 `[]`。校验字段格式，不限制引用来源；存储于内部节点 metadata，随任务传给 Executor，已结束任务保留原引用。
- 初始化与重规划均输出完整列表。已有 ID、目标、父子关系和历史路径保留；需要继续调查已结束路径时创建新任务。
- 未执行任务的执行字段为空；执行后由运行时写入真实代码和结果，Planner 更新必须原样保留。多次尝试使用 JSON 数组字符串保留全部代码和结果。
- Planner 按优先级排列列表，运行时选择首个祖先未结束的 todo 任务。未解决目标没有可执行任务时必须展开任务或明确失败。
- 内部仍使用 TTT 快照存储，并转换状态和字段；事件 ID、轮次、版本与 `next_task_id` 由运行时管理。SQLite 写入仍检查期望版本。

```yaml
- id: G1
  objective: 告警涉及的进程 ID 是什么？
  status: todo
  children:
    - id: F1
      objective: 沿告警证据定位进程
      status: todo
      children:
        - id: T1
          objective: 查询告警关联的进程 ID
          status: todo
          semantic_refs: []
          execution_code: ""
          execution_result: ""
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

临时消融可在 `.env` 设置 `SOCAGENT_LTM_ENABLED=false`，关闭 workflow 的 SM/PM/EM 加载与检索；SQL 报错仍可依据真实错误重试。恢复时设为 `true` 并重启进程。默认值为 `true`。

长期记忆由 [MemoryContextService](src/workflow/context.py) 检索和裁剪后注入角色，不向 Agent 暴露通用 Cypher 或数据库访问接口。

| 类型 | 内容与来源 | 详细说明 |
| --- | --- | --- |
| Semantic Memory（SM） | L1 全局表目录（用途、实体）与 L2 单表字典（字段名、MySQL 类型、中文解释） | [构建与读取](src/memory/longterm_memory/semantic_memory/excytin_bench/README.md) |
| Episodic Memory（EM） | 三字段失败恢复：背景、报错、修复；21 条，覆盖训练历史 70 次错误 | [构建与导入](src/memory/longterm_memory/episodic_memory/excytin_bench/README.md) |
| Procedural Memory（PM） | 训练轨迹中重复的操作序列；以名称、状态、目标检索，按需读取输入槽和步骤 | [构建与导入](src/memory/longterm_memory/procedural_memory/excytin_bench/README.md) |

普通事件通过 `context.memory_profile` 指定 `excytin_bench`，或设置默认 `SOCAGENT_MEMORY_PROFILE=excytin_bench`，才注入该 Profile 的记忆；事件配置优先。不设置时，普通 Splunk 调查不会注入 ExCyTIn 记忆。指定 Profile 不会把本地 Splunk Executor 切换为 MySQL 测评流程。

SM 固定读取本地 `l1.yaml` 与 `l2/<表名>.yaml`。PM/EM 后端通过 `SOCAGENT_LTM_BACKEND` 选择：

- `snapshot`：读取仓库内的 JSON 快照，无需 Neo4j。
- `neo4j`：从已导入的 Neo4j 图加载，连接失败会报错。
- `auto`：存在非空 `NEO4J_PASSWORD` 时尝试 Neo4j，连接失败回退到快照；否则直接读取快照。

`.env.example` 含 Neo4j 占位密码，首次运行建议显式使用 `snapshot`。使用图后端时，按 [Neo4j 部署说明](docker/neo4j/README.md) 启动服务，将连接配置导出到进程或写入根目录 `.env`，再依次导入 **EM → PM**；导入时使用本地 L1/L2 校验 schema，无需 SM 图。重建 PM/EM JSON 不会自动更新 Neo4j。

PM v5 保存可复用查询模板：`id/name/status/purpose/inputs/sql_template/returns/semantic_refs`。从训练成功查询中归纳 46 条模板（34 active、12 candidate），案件具体值参数化，表字段离线校验。在线仅一次有界模型选择（不再前提审核/独立复核），Planner 在 T 中引用 `template_id`，程序装载模板，Executor 只返回参数，程序安全生成 SQL；PM 未命中时检索精简 SM。模板与构建审计详见 `src/memory/longterm_memory/procedural_memory/excytin_bench/README.md`。Reviewer 不检索长期记忆；SQL 报错后仍仅一次 EM 修复。

在线 SM 从完整 L1 选择一个精确表名，再读取对应 L2；EM v2 先按错误类型与对象查本地倒排索引，再以失败 SQL 的表/别名作用域消歧，优先报错位置并用 TF-IDF 相似度排序，最多取一条供 Executor 修复，未调用 embedding API。PM 使用以问题为目标的候选召回与适用性审查；引用存在不等于语义关联正确，真实执行仍需核实。

## ExCyTIn-Bench 测评

测评使用独立的同步 workflow，不通过 Web 或后台 SQLite 轮询运行：

```text
context + question -> Planner / ITT -> 当前 T -> Executor 生成只读 SQL
    -> 环境执行 -> 程序回写 T 的 SQL/结果/状态 -> Reviewer -> Verifier -> Planner 更新 / 提交答案
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

默认每题最多 25 个 action，预留最后一步提交答案，即最多 24 次 SQL。每个 L3 内最多尝试 2 条 SQL；每次仅传当前 T，首次 SQL error 后检索一次 EM 并重试一次，空结果直接交给 Reviewer，不在 Executor 内重试；Reviewer 不检索 SM/PM。Verifier 在每轮 Reviewer 后判断能否回答：能则直接生成并提交答案，不能则继续规划；预算耗尽且仍不能回答时提交“无法确认答案：调查结束，尚无充分证据。”

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

[external_agents](external_agents/README.md) 管理 OpenCode、Qwen Code、Cline、Pi 的原生 CLI 安装、版本和实验结果。它们通过统一 MCP 接口执行只读 SQL、提交答案，不注入本项目的 SM / PM / EM。

除 Python 环境外，需要 Node.js **22+**、npm、Git，以及对应题目和 MySQL 数据：

```bash
uv run python external_agents/bootstrap.py
uv run python -m src.benchmarks.external_agents.runner \
  --agents opencode qwen-code cline --incident incident_5 --qid 39 \
  --workers 3 --max-steps 25 --llm-eval
```

外部 CLI runner 默认使用论文官方 evaluator，裁判默认复用调查模型的 DeepSeek 配置，`--no-llm-eval` 可关闭评分。默认全量测试集，每题 25 个总动作（查询和提交各计一步，达到上限立即结束），可用 `--max-steps 15`。每题使用独立会话和临时配置。批量入口 `src.benchmarks.external_agents.batch` 支持四个 CLI、论文 `BaselineAgent` 和 SOCTraceAgent 的六框架配对实验；默认全量，也可显式 `--per-incident` 抽样。分析会区分评分失败和 0 分。新协议拒绝混用旧实验目录。版本条件、论文额外依赖和断点复用规则见[基线文档](external_agents/README.md)。

## 配置参考

支持 ParaTera 和 Fancybag；默认保持 ParaTera。使用 Fancybag 时，在根目录 `.env` 填入并切换：

```dotenv
SOCAGENT_LLM_PROVIDER=fancybag
FANCYBAG_API_KEY=填入你的key
FANCYBAG_BASE_URL=https://sub.fancybag.cn/v1
FANCYBAG_MODEL=deepseek-v4.1-flash
```

重启 Agent 进程使配置生效。Fancybag 使用独立的 `FANCYBAG_*` 配置，不会回退使用 ParaTera 的密钥；切回时设为 `SOCAGENT_LLM_PROVIDER=paratera`。聊天限流与输出 token 预留仍共用现有 `PARATERA_RPM_LIMIT`、`PARATERA_TPM_LIMIT`、`PARATERA_RATE_UTILIZATION` 和 `PARATERA_OUTPUT_TOKEN_RESERVE` 配置。Fancybag 的 embedding 模型默认留空，有需要时按账号支持情况设置 `FANCYBAG_EMBEDDING_MODEL`。

XTY 接口使用独立配置，填写 `.env` 中的 `XTY_API_KEY` 后可切换：

```dotenv
SOCAGENT_LLM_PROVIDER=xty
XTY_API_KEY=your_api_key_here
XTY_BASE_URL=https://api.xty.app/v1
XTY_MODEL=deepseek-v4.1-flash
```

实验入口 `src.benchmarks.external_agents.resume` 也支持 `--provider xty`，只覆盖当前进程的接口选择。实际聊天请求发送到 `/v1/chat/completions`；embedding 模型默认留空。

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

PM 未命中时使用 L1/L2 SM 兜底：一次模型调用读取完整全局表目录（table、desc、entities），选择一个精确表名，由程序读取对应 L2 卡片。Planner 接收完整字段名、MySQL 类型和中文解释（保留源类型说明），不截断表目录或单表字段。选择助手输出最多 192 token、关闭 thinking、不进行格式重试；不传完整 ITT 或执行历史。相同任务和 SM 库复用缓存（含未命中）。SM 也未命中时，Planner 应安排结构查询，不猜字段。
