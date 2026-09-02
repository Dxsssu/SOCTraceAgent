# SOCAgent

SOCAgent 是一个面向安全告警溯源场景的三角色多智能体原型。当前版本以本地 `SQLite` 作为共享状态中心，通过 `Planner`、`Executor`、`Reviewer` 三个角色围绕同一事件进行多轮协作，逐步形成 `TTT`（Traceback Task Tree，溯源任务树）、执行记录和轮次复盘结果。

项目目前更接近“可运行的架构骨架”而不是完整产品：

- 有清晰的多角色状态流转
- 有持久化的共享黑板和消息留痕
- 有可用的 Web 首页和 war room 界面
- 有基于 Socket.IO 的事件实时推送
- 有 LLM 接口封装
- 有独立的多轮链路烟雾测试
- 还没有接入真实外部安全工具

## 项目目标

这个仓库当前验证的是一条最小闭环：

1. `Planner` 接收告警并初始化 `TTT`
2. `Executor` 领取 `TTT` 中的 L3 叶子节点并执行
3. `Reviewer` 汇总本轮执行结果并生成 `RoundReview`
4. `Planner` 根据 `RoundReview` 更新下一轮 `TTT`
5. 如果还有未完成叶子节点，则继续下一轮；否则事件结束

对应的核心业务链路是：

```text
Event -> TTT -> Execution -> RoundReview -> TTT
```

这里的结构化消息只是审计、调试和未来前端展示用的观测层，不是业务事实来源。业务事实来源始终是 SQLite 中的持久化状态。

## 当前架构

### 角色职责

- `Planner`
  - 读取新告警
  - 初始化第一版 `TTT`
  - 在每轮结束后基于 `RoundReview` 更新下一轮 `TTT`

- `Executor`
  - 从当前 `TTT` 中领取一个待执行的 L3 叶子节点
  - 选择工具并生成 `Execution`
  - 将叶子节点状态推进为 `done` 或 `n/a`

- `Reviewer`
  - 汇总当前轮的 `Execution`
  - 识别已验证结论、证据缺口、能力缺口
  - 写入 `RoundReview` 并把事件状态切回 `replanning`

### 共享状态对象

- `Event`
  - 事件主对象，维护当前轮次和总体状态

- `TracebackTaskTree`
  - 按版本保存的任务树快照
  - 严格限制为三层：`L1 -> L2 -> L3`

- `Execution`
  - `Executor` 针对某个 L3 叶子节点的一次执行记录

- `RoundReview`
  - `Reviewer` 在每轮结束后的总结

- `MessageEnvelope`
  - 用于审计和调试的结构化消息

### 状态流转

`EventStatus` 当前定义如下：

```text
pending -> planned -> executing -> reviewing -> replanning -> planned/completed
```

其中：

- `pending`：新事件，等待 `Planner`
- `planned`：已有待执行 `TTT`
- `executing`：`Executor` 正在处理叶子节点
- `reviewing`：本轮单个 L3 已执行完成，等待 `Reviewer`
- `replanning`：`Reviewer` 已输出总结，等待 `Planner` 更新下一轮
- `completed`：任务树无开放叶子节点，事件结束

## TTT 约束

当前实现里，`Planner` 输出的 `TTT` 必须满足这些规则：

- 必须是完整快照，不是增量 patch
- 严格只有三层
- `L1` 表示阶段性目标
- `L2` 表示待验证的子问题或假设
- `L3` 表示可执行意图
- `node_id` 使用纯数字分层编号，例如 `1`、`1-2`、`1-2-3`
- `Executor` 只消费 `L3` 叶子节点

节点状态使用：

- `todo`
- `in_progress`
- `done`
- `n/a`

## 目录结构

```text
SOCAgent/
├─ main.py                         # 入口：初始化数据库 / 启动角色 / 启动 Web
├─ pyproject.toml                 # 项目元数据
├─ src/
│  ├─ agent/
│  │  ├─ planner.py               # Planner 运行时
│  │  ├─ executor.py              # Executor 运行时
│  │  ├─ reviewer.py              # Reviewer 运行时
│  │  └─ llm.py                   # OpenAI 兼容 LLM 封装
│  ├─ workflow/
│  │  ├─ orchestrator.py          # 三角色、共享状态和 Memory View 装配
│  │  └─ context.py               # 角色级长期记忆权限与检索上下文
│  ├─ benchmarks/excytin_bench/   # ExCyTIn 外部动作 workflow 与 SecGym Agent 适配器
│  ├─ memory/longterm_memory/     # Semantic / Episodic / Procedural Memory
│  ├─ memory/working_memory/
│  │  └─ ttt_store.py             # TTT 快照与节点状态维护
│  ├─ messaging/
│  │  ├─ bus.py                   # SQLite / 内存消息总线
│  │  ├─ models.py                # MessageEnvelope / MessageQuery
│  │  ├─ message_types.py         # 消息类型与角色名
│  │  └─ README.md                # 通信设计说明
│  ├─ schema/
│  │  ├─ event.py                 # Event / EventStatus / SeverityLevel
│  │  ├─ execution.py             # Execution
│  │  ├─ round_review.py          # RoundReview
│  │  └─ ttt.py                   # TTT / TTTNode
│  └─ storage/
│     └─ sqlite.py                # Event / Execution / RoundReview 持久化
│  └─ webapp/
│     ├─ server.py                # Flask + Socket.IO Web 层
│     ├─ templates/
│     │  ├─ index.html            # 首页：创建事件 + 事件列表
│     │  └─ warroom.html          # 作战室：消息流 + 状态 + 执行记录
│     └─ static/
│        ├─ css/
│        └─ js/
├─ tests/
│  └─ test_multi_agent_loop.py    # 多轮闭环烟雾测试脚本
├─ runtime/                       # 本地运行数据（不提交到 Git）
│  └─ socagent.db                 # 默认 SQLite 数据库（运行时自动创建）
└─ data/
   └─ splunk-bots-docker/         # 预置的 BOTS/Splunk 相关数据
```

## 运行环境

### Python

`pyproject.toml` 当前要求：

- Python `>= 3.13`

### 依赖

当前项目依赖很少：

- `flask`
- `flask-socketio`
- `openai`
- `python-dotenv`
- `pyyaml`

推荐使用 `uv` 安装：

```bash
uv sync
```

如果你不用 `uv`，也可以手动安装：

```bash
pip install flask flask-socketio openai python-dotenv pyyaml
```

## 环境变量

项目通过 `.env` 加载配置。当前代码读取的变量如下：

```env
DEEPSEEK_API_KEY=your_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_REASONING_EFFORT=high
DEEPSEEK_THINKING_ENABLED=true
SOCAGENT_DB_PATH=runtime/socagent.db
SOCAGENT_POLL_INTERVAL=5
SOCAGENT_WEB_HOST=127.0.0.1
SOCAGENT_WEB_PORT=5008
SOCAGENT_MEMORY_PROFILE=
SOCAGENT_LTM_BACKEND=auto
SPLUNK_USERNAME=admin
SPLUNK_PASSWORD=changeme
SPLUNK_VERIFY_TLS=false
SPLUNK_DEFAULT_DATASET=botsv1
SPLUNK_BOTSV1_BASE_URL=http://127.0.0.1:8000
SPLUNK_BOTSV1_INDEX=botsv1
SPLUNK_BOTSV2_BASE_URL=http://127.0.0.1:8020
SPLUNK_BOTSV2_INDEX=botsv2
SPLUNK_BOTSV3_BASE_URL=http://127.0.0.1:8030
SPLUNK_BOTSV3_INDEX=botsv3
```

说明：

- `DEEPSEEK_*`
  - 由 `src/agent/llm.py` 使用
  - 采用 OpenAI 兼容接口调用模型
- `SOCAGENT_DB_PATH`
  - 所有角色共享的 SQLite 文件路径
- `SOCAGENT_POLL_INTERVAL`
  - 角色运行时轮询数据库的时间间隔，单位秒
- `SOCAGENT_WEB_HOST` / `SOCAGENT_WEB_PORT`
  - Web 首页和 war room 的监听地址
- `SOCAGENT_MEMORY_PROFILE`
  - 可选的默认长期记忆 Profile；当前支持 `excytin_bench`
  - 事件中的 `context.memory_profile` 优先级更高
- `SOCAGENT_LTM_BACKEND`
  - `neo4j`：强制从已导入的 Neo4j 图加载三层 Memory
  - `snapshot`：从仓库中的脱敏 JSON 快照加载
  - `auto`：存在 `NEO4J_PASSWORD` 时优先 Neo4j，否则使用快照
- `SPLUNK_*`
  - 由 `src/tools/splunk.py` 使用
  - 用于配置 Splunk Docker 的用户名、密码、数据集默认值与端口映射
  - 默认按 `botsv1 -> 8000`、`botsv2 -> 8020`、`botsv3 -> 8030` 连接

注意：

- 仓库中的 `.env` 不应该保存真实密钥，建议改为占位值并使用你自己的 API Key
- 如果真实密钥已经入库，应该立即轮换
- `Planner` 和 `Reviewer` 现在要求真实 LLM 可用；如果 `DEEPSEEK_API_KEY` 缺失或模型返回非法 YAML，事件会直接进入 `failed`，不会再自动生成模拟结果

## 初始化与启动

### 1. 初始化本地状态

```bash
python main.py -init-db
```

这一步会初始化：

- `events`
- `executions`
- `round_reviews`
- `ttt_snapshots`
- `messages`

### 2. 启动 Web 界面

```bash
python main.py -web
```

默认会启动在：

```text
http://127.0.0.1:5008
```

首页能力：

- 创建事件
- 查看事件列表
- 进入单事件 war room

war room 能看到：

- 事件基本信息
- 实时消息流
- 当前轮次
- 执行记录
- RoundReview 结果
- TTT / Execution / Review 层级概览

### 3. 分别启动三个角色

项目当前不是单进程调度器，而是通过三个独立进程轮询同一个 SQLite 文件。

分别打开三个终端执行：

```bash
python main.py -role planner
python main.py -role executor
python main.py -role reviewer
```

角色名也兼容：

- `_planner`
- `_executor`
- `_reviewer`

### 4. 写入事件

当前版本已经有 Web/API，两种方式都可以：

- 通过首页表单创建事件
- 通过测试脚本创建测试事件
- 或在你自己的脚本中调用 `SQLiteStorage.save_event(...)`

### 5. 推荐的联调启动顺序

建议开四个终端：

```bash
python main.py -web
python main.py -role planner
python main.py -role executor
python main.py -role reviewer
```

然后在浏览器打开首页创建事件，进入 war room 观察闭环推进。

## 推荐的本地验证方式

最直接的验证入口是烟雾测试脚本：

```bash
python tests/test_multi_agent_loop.py
```

这个脚本会：

1. 创建测试专用 SQLite 文件
2. 启动 `Planner / Executor / Reviewer`
3. 直接写入一条测试事件
4. 持续轮询 `Event / TTT / Execution / RoundReview / Message`
5. 判断多轮链路是否形成闭环

常用参数示例：

```bash
python tests/test_multi_agent_loop.py --timeout 240 --target-rounds 2
python tests/test_multi_agent_loop.py --show-process-logs
python tests/test_multi_agent_loop.py --db-path runtime/test_multi_agent_loop.db
```

## 当前实现细节

### Workflow 与长期记忆权限

`src/workflow/orchestrator.py` 统一装配三个 Agent Runtime。长期记忆由
`src/workflow/context.py` 检索并裁剪后注入角色，不向 Agent 暴露通用数据库或
Cypher 执行接口：

```text
Event -> Workflow -> PM + SM -> Planner -> TTT
TTT L3 -> Workflow -> SM + EM -> Executor -> Execution
Execution -> Workflow -> SM -> Reviewer -> RoundReview
RoundReview -> Workflow -> PM + SM -> Planner
```

每个 workflow 轮次只执行一个 L3。Executor 完成或失败后都会立即把事件切换到
`reviewing`；Reviewer 针对该次执行生成反馈，再由 Planner 保留完整 TTT 并更新下一轮，
从而让后续查询能够使用上一条查询返回的 observation。

角色权限固定为：

- Planner：Procedural Memory + Semantic Memory
- Executor：首次查询只使用 Semantic Memory；SQL error 修复时再使用 Episodic Memory
- Reviewer：Semantic Memory

ExCyTIn-Bench 事件需要显式声明 Profile：

```json
{
  "context": {
    "memory_profile": "excytin_bench"
  }
}
```

未声明 Profile 时不注入 ExCyTIn-Bench 记忆，从而保持现有 Splunk/BOTS 工作流行为。
若使用 Neo4j 后端，启动前需要将 `docker/neo4j/.env` 中的配置导出到当前进程，
或在项目根目录 `.env` 中设置 `NEO4J_URI/USERNAME/PASSWORD/DATABASE`。

### ExCyTIn-Bench 独立 Workflow

`src/benchmarks/excytin_bench/workflow.py` 实现同步的 benchmark 状态机，和上面的
后台轮询 workflow 相互独立。它不会在 Executor 内部连接 MySQL，而是将每条只读
SQL 返回给 `ExcytinEnv.step()`；下一次收到环境 observation 后，才依次执行 Reviewer
审查、Planner 重规划和下一个 L3 查询。这样官方环境仍然负责 SQL 执行、步数限制、
结果截断和最终评分。

`src/benchmarks/excytin_bench/agent.py` 提供 SecGym 所需的 `name`、`reset()`、
`act(observation)` 和 `get_logging()` 接口。完整接入示例和角色记忆边界见
`src/benchmarks/excytin_bench/README.md`。

### 数据存储

当前状态全部保存在同一个 SQLite 文件中：

- `events`
- `executions`
- `round_reviews`
- `ttt_snapshots`
- `messages`

其中：

- `src/storage/sqlite.py` 负责 `Event / Execution / RoundReview`
- `src/memory/working_memory/ttt_store.py` 负责 `TTT`
- `src/messaging/bus.py` 负责结构化消息留痕

### Web 交互层

`src/webapp/server.py` 当前提供了一个最小 Web 层，整体交互方式参考 `deepsoc`：

- 首页通过 HTTP API 创建事件、拉取事件列表
- war room 首屏通过 HTTP 拉取：
  - 事件详情
  - 消息流
  - 执行记录
  - RoundReview
  - 轮次层级结构
- 页面通过 Socket.IO `join(event_id)` 进入事件房间
- 后端通过 SQLite watcher 检测数据库变化，并把新消息和状态变化实时推送到 war room

这意味着当前 Web 界面不是前端假数据模拟，而是直接消费本项目自己的真实 SQLite 状态。

### LLM 调用

`src/agent/llm.py` 当前通过 `OpenAI` Python SDK 调用 OpenAI 兼容接口，但环境变量命名采用 `DEEPSEEK_*`。这意味着：

- 目前默认是给 DeepSeek 兼容接口准备的
- 如果你切换到其他 OpenAI 兼容服务，只需要替换 `base_url / model / api_key`

### 工具执行现状

`Executor` 当前已经接入一套真实的 Splunk MCP 工具能力：

- `log_search` 会根据 TTT 叶子节点的自然语言意图，先由 LLM 在内置路由阶段选择工具
- 选中 `log_search` 后，再由 `src/tools/splunk.py` 把调查意图翻译成查询规格与 SPL
- 最终通过 Splunk REST API 执行真实查询，并返回摘要与样本事件

当前 `src/tools` 只保留一个 MCP server：`src/tools/splunk.py`。`Executor` 不再依赖标题关键词硬编码挑工具，而是读取当前可用 MCP tools 的描述、用途与限制，用 LLM 做一次内置工具选择，然后调用被选中的 tool。

### Splunk 工具最小调用示例

结构化查询：

```python
from src.tools import SplunkSearchTool

tool = SplunkSearchTool()
result = tool.search(
    spec={
        "dataset": "botsv1",
        "sourcetype": "WinEventLog:Security",
        "keywords": ["failed login"],
        "ip": "11.22.33.44",
        "limit": 5,
        "fields": ["_time", "host", "user", "src"],
    }
)
```

自然语言调查意图：

```python
from src.tools import SplunkSearchTool

tool = SplunkSearchTool()
spec = tool.interpret_intent(
    "查询 botsv1 中和 11.22.33.44 相关的失败登录日志",
    dataset="botsv1",
)
query = tool.build_query(spec)
result = tool.search(spec=spec)
```

## 已实现能力

- 三角色运行时拆分清晰
- 基于 SQLite 的跨进程共享状态
- Web 首页和 war room 界面
- 基于 Socket.IO 的实时消息推送
- TTT 版本化快照
- 节点领取与状态推进
- 执行结果持久化
- 轮次复盘持久化
- 结构化消息留痕
- 独立的多轮闭环测试脚本
- LLM 结果校验与失败显式落库

## 当前局限

- 没有真实外部工具接入
- 没有统一的 supervisor 管理三个角色进程
- SQLite 适合单机原型，不适合复杂并发和分布式部署
- `Executor` 的工具选择仍是规则驱动占位实现
- 测试脚本是终端脚本，不是标准 `pytest` 用例
- 当前 Web 层没有用户认证、权限和持久会话管理
- 当前实时推送基于本地 SQLite watcher，不是消息队列架构

## 后续建议

- 为 `Executor` 接入真实日志检索、资产查询、威胁情报工具
- 为 `Planner` / `Reviewer` 增加更稳定的输出约束与校验
- 为 war room 增加更细粒度的 TTT 可视化和节点 drill-down
- 增加数据库快照查看和回放工具
- 增加标准化测试覆盖，而不只依赖烟雾测试
- 如果后续需要更稳定的实时推送，再把当前 watcher 扩展成消息队列或事件流架构

## 相关文件

- 入口：[main.py](/d:/Research/SOCAgent/main.py)
- Web 服务：[src/webapp/server.py](/d:/Research/SOCAgent/src/webapp/server.py)
- 首页模板：[src/webapp/templates/index.html](/d:/Research/SOCAgent/src/webapp/templates/index.html)
- War Room 模板：[src/webapp/templates/warroom.html](/d:/Research/SOCAgent/src/webapp/templates/warroom.html)
- 多轮测试：[tests/test_multi_agent_loop.py](/d:/Research/SOCAgent/tests/test_multi_agent_loop.py)
- 通信设计：[src/messaging/README.md](/d:/Research/SOCAgent/src/messaging/README.md)
- Planner：[src/agent/planner.py](/d:/Research/SOCAgent/src/agent/planner.py)
- Executor：[src/agent/executor.py](/d:/Research/SOCAgent/src/agent/executor.py)
- Reviewer：[src/agent/reviewer.py](/d:/Research/SOCAgent/src/agent/reviewer.py)
