# 外部 Agent 与 ExCyTIn 测评

此目录集中存放 OpenCode、Qwen Code、Cline、Pi Coding Agent 的源码、锁定的 CLI 安装包和运行结果。
使用各项目原生 CLI 调查循环，不重新实现其 Planner，也不向它们注入 SOCTraceAgent 的 SM/PM/EM。

## 目录

- `repos/opencode/`、`repos/qwen-code/`、`repos/cline/`：官方 Git 仓库的下载副本，忽略提交。
- `repos/pi/`：Pi 官方仓库的源码下载副本，CLI 源码位于 `packages/coding-agent/`。
- `versions.json`：下载源码的 commit 和实际运行的 npm CLI 版本。源码快照与 npm 发布版分别记录，不能视为同一版本。
- `runtime/package.json`、`runtime/package-lock.json`：锁定 CLI 依赖，纳入版本控制。
- `runtime/node_modules/`：本地安装，不修改系统全局 CLI，不纳入版本控制。
- `runs/<时间>/<agent>/<case>/`：SQL 轨迹、原生 CLI 输出、模型 usage、答案和评分；忽略提交。
- `../src/benchmarks/external_agents/`：统一模型代理、MCP 服务和 Python runner。

重新安装（需要 Node.js 22+、npm、Git，以及项目 `uv sync`）：

```bash
.venv/bin/python external_agents/bootstrap.py
```

## Workflow

```text
同一组题目（只提取 context、question）
  → 每个 Agent / 每道题创建临时工作目录和独立会话配置
  → 原生 CLI 调用 DeepSeek-V4.1-Flash
  → MCP execute_sql → 只读校验 → 对应 incident 的 MySQL
  → 查询结果返回 Agent，自行决定后续调查
  → MCP submit_answer → 收回 answer
  → 调查结束后读取标准答案，统一评分、落盘
```

四个 CLI 的模型请求都经过本地 OpenAI-compatible 代理。代理使用根目录 `.env` 的
`PARATERA_API_KEY`、`PARATERA_BASE_URL`、`PARATERA_MODEL`，真实模型密钥及数据库密码不传入 CLI。
MCP 子进程只持有临时本地访问令牌。数据库密码从 `docker/excytin-mysql/.env` 加载。
生成的 CLI 配置放入临时目录，结束后删除；不覆盖用户已有的 CLI 配置。

默认同时运行 3 个 Agent/题目任务，共用进程级 2500 RPM / 500 万 TPM 的限流器，
按已有 `PARATERA_RATE_UTILIZATION=0.8` 留余量。分别启动多个 runner 进程不共享额度。
代理固定 temperature=0，通常关闭 thinking；GLM-5.3 系列不支持关闭思考，改为显式启用，
思考强度使用 `PARATERA_REASONING_EFFORT`（默认 `low`），调查和裁判设置均写入 manifest。
上游完成后转换为 SSE，保留原生工具调用。
这会改变首 token 时延，不能用于比较原生流式交互时延。

新实验使用 `excytin-paper-v1` 协议，默认对齐论文 Table 2：每题最多 **25 个动作**，
每次 SQL（含错误、空结果和本地拒绝的 SQL）或提交答案各算一步。提交立即结束；
第 25 步若仍查询，则执行该查询后结束，记 `budget_exhausted`、reward=0，不能再提交。
论文其他实验可通过 `--max-steps 15` 对齐。默认不额外限制模型请求次数和整题耗时；
可选 `--max-llm-calls`、`--timeout` 均以 0 表示关闭，非零值写入 manifest，属于额外实验条件。
CLI 正常退出但未提交记 `no_submission`；超时、进程错误、模型调用上限等保留为未评分。
不会从普通对话中猜测最终答案。

默认使用上游 `secgym/evaluator.py` 的答案判断、答案 reflection、步骤判断、步骤 reflection
和 0.4 衰减奖励；只替换网络传输，不使用自定义简化裁判。需先准备本地官方源码
`data/excytin-bench/github/secgym/evaluator.py`。裁判默认复用调查模型的配置（当前为 DeepSeek），模型名、接口地址、密钥及请求超时均继承
`PARATERA_*`，无需 GPT-4o 接口。仅在需要单独覆盖时配置：

```dotenv
# 以下均为可选覆盖；默认不设置，随调查模型配置变化
# EXCYTIN_JUDGE_MODEL=DeepSeek-V4.1-Flash
# EXCYTIN_JUDGE_BASE_URL=https://your-provider.example/v1
# EXCYTIN_JUDGE_API_KEY=your_judge_key
```

也可用 `--judge-model` 覆盖模型名。优先级为命令行、`EXCYTIN_JUDGE_MODEL`、调查模型。
评分流程沿用论文，但论文使用 GPT-4o、本项目使用 DeepSeek；manifest 记录实际裁判配置。裁判格式错误、步骤缺失、API 错误
均保留为 `judge_error`，不变成 0 分。缺失题目或未评分题目会使总体正确率、平均 reward 为
`null`；另提供明确标记的已评分子集指标。

代理仅向模型暴露查询、提交以及必要的 MCP 分发/工具发现接口；
OpenCode 配置还拒绝其他工具，Qwen 禁用核心文件和 Shell 工具。
Pi 使用原生 MCP direct 模式，仅启用 `execute_sql` / `submit_answer`，禁用其他扩展、技能、上下文文件和持久会话；
模型与 MCP 配置写入每题独立的 `PI_CODING_AGENT_DIR`，关闭遥测、版本检查及模型目录联网刷新。
临时工作目录及工具约束不是操作系统沙箱，不能用来执行不可信插件。
这是统一工具条件下的 CLI 基线，不是未经配置的默认 CLI，也不是论文官方 ExcytinEnv。

## 运行

### PromptSauceAgent + Fancybag

已接入论文原版 `PromptSauceAgent` 的完整调查、官方 evaluator 评分、进度统计与断点续跑流程。原始源码位于 `data/excytin-bench/github/secgym/agents/prompt_sauce_agent.py`。保留其告警/实体调查提示词、历史维护及 SQL/答案解析，只替换模型传输；每题调用 `reset()`，不注入本项目 SM/PM/EM。

原始类在最后一步读取未初始化的 `submit_summary`，适配层显式设为 `False`，避免末步 AttributeError，并保持每题 25 个总动作的共同预算，不额外强制提交。manifest 记录该适配版本、Agent/解析工具源码哈希、模型、接口及评分协议；改动这些条件时必须换结果目录。

运行前确保根目录 `.env` 已填写 `FANCYBAG_API_KEY`，MySQL incident 容器与 `docker/excytin-mysql/.env` 可用。Python 依赖与论文 Baseline 相同（当前环境已安装），新环境在项目根目录执行以下命令；不需要安装 OpenCode/Pi CLI：

```bash
uv sync
uv pip install --python .venv/bin/python 'ag2==0.14.0' 'azure-ai-inference==1.0.0b9'
```

这两项上游依赖暂未纳入项目锁文件；后续再次执行 `uv sync` 后，需要重新安装。

先抽 10 道题验证：

```bash
.venv/bin/python -m src.benchmarks.external_agents.resume \
  --agent prompt_sauce --provider fancybag \
  --model deepseek-v4.1-flash \
  --sample-size 10 --seed 42 --workers 3 --max-steps 25 \
  --output results/prompt_sauce/fancybag-deepseek-v4.1-flash/smoke10
```

全量 589 道测试题：

```bash
.venv/bin/python -m src.benchmarks.external_agents.resume \
  --agent prompt_sauce --provider fancybag \
  --model deepseek-v4.1-flash \
  --workers 3 --max-steps 25 \
  --output results/prompt_sauce/fancybag-deepseek-v4.1-flash/full589
```

`--provider` 只覆盖当前进程，不修改 `.env`；`--model` 写入所选 provider 的运行时配置，不会误写到另一个接口。调查与裁判默认都使用 Fancybag，已有 `EXCYTIN_JUDGE_BASE_URL/API_KEY` 会覆盖裁判连接，需保持未设置才能保证同一接口。模型名使用接口要求的小写 `deepseek-v4.1-flash`。

重复相同命令可续跑：已评分题（含错误答案或预算耗尽）跳过；调查完成但裁判失败只补评；调查失败重跑并保留旧尝试。`summary.json` 保存指标及结果，`progress.json` 保存完成进度，`cases/<case>/result.json` 保存 SQL、答案、评分、实际 token 使用量。不同题集的小规模测试与全量结果使用不同目录。

离线回归：

```bash
uv run --with pytest python -m pytest tests/test_prompt_sauce.py tests/test_opencode_resume.py tests/test_external_paper_protocol.py -q
```

### OpenCode 全量运行与断点续跑

论文原版 `BaselineAgent` 也支持同一进度显示和续跑规则：

```bash
.venv/bin/python -m src.benchmarks.external_agents.resume \
  --agent paper_baseline --model DeepSeek-V4.1-Flash --workers 3 --max-steps 25
```

结果保存到 `results/paper_baseline/DeepSeek-V4.1-Flash/`。
使用本地官方源码的原始提示词和动作解析，每题调用 `reset()`，保留 `cache_seed=41`；
调查和裁判均使用指定的 DeepSeek，评分和 25 步预算沿用统一协议。
manifest 记录论文 Agent 源码哈希，防止修改源码后混合续跑。
此入口需要本地官方源码和兼容依赖 `ag2==0.14.0`、`azure-ai-inference==1.0.0b9`。

同一入口支持 Pi，添加 `--agent pi` 即可，终端显示和重试规则相同：

```bash
.venv/bin/python -m src.benchmarks.external_agents.resume \
  --agent pi --model DeepSeek-V4.1-Flash --workers 3 --max-steps 25
```

Pi 的结果保存到 `results/pi/DeepSeek-V4.1-Flash/`，使用已安装的原生 Pi CLI 和 MCP 工具。
不指定 `--agent` 时仍默认 OpenCode，已有 OpenCode 结果可以继续使用原命令续跑。

在项目根目录执行（先启动 Docker 和 ExCyTIn MySQL 容器）：

```bash
.venv/bin/python -m src.benchmarks.external_agents.resume \
  --model DeepSeek-V4.1-Flash --workers 3 --max-steps 25
```

此入口默认运行全部 589 道 test 题，调查和裁判均指定为上述模型。
结果固定保存到 `results/opencode/DeepSeek-V4.1-Flash/`，不创建时间戳实验目录。
`cases/<case>/` 保存答案、SQL 轨迹和 CLI 输出；`summary.json` 保存汇总；
`progress.json` 保存进度；终端进度条显示处理题数、已完成评分题数、完全正确题数、
Reward 总分、已评分题目的平均 Reward、已完成跳过数、本轮失败数和 ETA。
评分统计包含续跑前已完成的结果；完全正确采用裁判结果，未评分/失败题不计入平均 Reward 的分母。
并发任务按实际完成顺序更新进度。无需安装额外进度条依赖。

重复执行相同命令时，已完成评分的题目直接跳过，包括答错、正常耗尽预算和正常退出但未提交的题目。
接口错误、超时、中断、`skipped`、损坏或缺失的结果会重新调查；调查已完成但裁判失败的题目只补评。
每次调用对待处理题目最多尝试一次，仍失败的题目在下一次执行命令时重试。
历史失败尝试保存在 `attempts/`，汇总使用各题当前结果，不将旧尝试混入准确率；旧尝试费用需另行累计。
Ctrl+C 会停止调查并保存已有结果，再执行相同命令即可续跑。
输出目录有进程锁，拒绝同时向同一目录启动两份实验；模型、题目、协议或 CLI 版本改变时拒绝混合结果。

仅联调 5 题时使用独立目录，避免与全量实验混合：

```bash
.venv/bin/python -m src.benchmarks.external_agents.resume \
  --model DeepSeek-V4.1-Flash --sample-size 5 --seed 42 \
  --output results/opencode/DeepSeek-V4.1-Flash/smoke5
```

以下 `runner` 为每次创建新目录的历史入口；需要断点续跑时使用上面的 `resume`。

先确保题目、官方 evaluator 源码存在，所选 incident 的 MySQL 容器健康。
连接后会检查 alert 层的 AlertInfo、AlertEvidence、SecurityAlert 三张表。
SQL 结果裁剪和数据库错误格式沿用官方环境；保留单条只读 SQL 校验。

不指定 `--incident` / `--qid` / `--sample-size` 时，运行完整 589 道 test 题；
四个 CLI 共 2356 次调查。默认开启与调查模型一致的 DeepSeek 裁判。先做小规模联调可使用下面的单题命令。

四个 Agent 跑同一个已知样例（qid 从 0 开始）：

```bash
.venv/bin/python -m src.benchmarks.external_agents.runner \
  --agents opencode qwen-code cline pi --incident incident_5 --qid 39 \
  --workers 3 --max-steps 25 --llm-eval
```

固定 seed 抽取同一 incident 的 10 题，每个 Agent 使用完全相同的题目：

```bash
.venv/bin/python -m src.benchmarks.external_agents.runner \
  --incident incident_5 --sample-size 10 --seed 42 --workers 3
```

单独运行：`--agents pi`、`--agents cline`、`--agents qwen-code` 或 `--agents opencode`。
Pi CLI 锁定为 `1.0.4`，使用 `runtime/node_modules/node/bin/node` 中的 Node `22.19.0`；不升级系统 Node。
默认开启官方 evaluator；`--no-llm-eval` 仅用于工具联调，禁用裁判，保留严格匹配和辅助包含匹配。
答案正确为 1，否则按标准中间步骤倒序加权；不是对 SQL 轨迹逐条打分。
包含匹配不能替代正确率；附带说明的正确答案可能严格匹配为 false。

`manifest.json` 记录题目、模型、版本、预算及适配条件；`summary.json` 汇总运行结果。
每题 `result.json` 中保存 `answer`、`actions`、`usage`、`llm_calls`、耗时及评分。
`usage` 为终止前完成的模型请求用量；`judge_usage` 独立保存裁判用量及失败补评开销。
`elapsed_seconds` 截止提交/动作耗尽，`process_elapsed_seconds` 包含 CLI 退出清理。
终止后拒绝新的模型调用，在途请求若随后返回，其 usage 单独进入 `post_terminal_usage`。
分析只累计真实 token usage，不硬编码单价估算费用。
若与 SOCTraceAgent 比较，应采用同样题目、模型、预算、裁判；分别报告是否提供记忆。

## 官方来源

- OpenCode：https://github.com/anomalyco/opencode
- Qwen Code：https://github.com/QwenLM/qwen-code
- Cline：https://github.com/cline/cline
- Pi：https://github.com/earendil-works/pi（原 `badlogic/pi-mono`）

本地完整源码和各自许可证位于 `repos/`。下载目录不作为嵌套 Git 仓库提交到本项目。

## 多框架配对批量实验

四个原生 CLI、原论文仓库 `BaselineAgent` 和当前 SOCTraceAgent：

```bash
.venv/bin/python -m src.benchmarks.external_agents.batch \
  --output runtime/excytin_bench/six_agents_with_pi \
  --max-steps 25 --workers 3
.venv/bin/python -m src.benchmarks.external_agents.analyze \
  runtime/excytin_bench/six_agents_with_pi
```

默认全量 589 题，六框架共 3534 次调查。可用 `--per-incident 15 --seed 20260914`
显式抽样为 116 题（incident_38 仅 11 题），六框架共 696 次，不使用训练集补足。
论文框架每题构造后先调用官方 `reset()`，再使用原始提示词、历史和动作解析。
所有框架共享 Bridge 的总动作限制和同一个官方评分流程。SOCTraceAgent 的内部策略仍会
自行预留答案提交步，记忆后端遵循 `SOCAGENT_LTM_BACKEND`。

批量入口支持 `--agents pi` 或任意 Agent 子集，新实验默认运行全部六个 Agent。
同一输出目录再次运行会读取原 manifest 的题目和 Agent 列表，并跳过已有完整结果，不重复付费调查。
旧协议目录不能直接续跑新协议；请使用新的输出目录，原有结果保持不变。
manifest 记录协议版本、题目内容及官方 evaluator 的 SHA-256、裁判模型/地址、预算和额外限制。
这些条件改变时拒绝续跑，避免混合不同实验。分析按 manifest 的计划题数检查完整性。
模型裁判报错的已完成调查可复用原答案补评。已有失败记录不会自动重跑，避免只保留成功尝试。
`progress.json` 记录进度，`analysis.md` / `analysis.json` 汇总配对结果和 token 开销。
论文 Baseline 依赖实验环境的 `ag2==0.14.0` 和 `azure-ai-inference==1.0.0b9`；
这是原仓库兼容依赖，不能替换为不提供 `autogen` 导入的 AG2 1.x。

## 保留的适配条件

这是对齐论文预算和评分协议的原生 CLI 实验，未声称执行程序与官方逐字相同：
保留原生 Agent 的工具调用协议、只读 SQL 防护、模型代理的 thinking 参数适配及缓冲 SSE。
实际模型、数据版本和这些条件需随结果报告。MySQL 配置对应 alert 层，不支持在同一次
入口中切换论文的 log/alert_only/full_db 实验。只修改了评估封装，不修改 Agent 自身策略。

## Ollama Qwen3-32B：关闭思考，论文 Agent 10 题

以下是独立的历史实验入口，仍保留其原先的 24 次 SQL 加一次提交和 600 秒软超时；
它复用同一个官方 evaluator 实现，但不属于上述 `excytin-paper-v1` 的 CLI/batch 运行协议。

保持到远端 Ollama 的端口转发开启，并确保本地 ExCyTIn MySQL 容器运行。
在项目根目录执行（当前实验环境已安装论文 Agent 所需的 AG2/Azure 依赖）：

```bash
.venv/bin/python -m src.benchmarks.external_agents.ollama_paper \
  --base-url http://127.0.0.1:11434 \
  --model qwen3:32b \
  --sample-size 10 --seed 42 \
  --max-steps 25 --num-ctx 32768 \
  --judge-model DeepSeek-V4.1-Flash \
  --output runtime/excytin_bench/qwen3_32b_no_think_reset_10
```

- 串行执行，终端进度条显示题数、当前动作/LLM 调用及裁判阶段。
- 保留论文原版 BaselineAgent 的提示词选择、历史与解析逻辑，仅替换传输。
  每题创建后调用官方 `reset()`（初始 cache_seed=41），使 Qwen3 使用 reset 后的
  `O1_PROMPT`。manifest 记录初始化版本，拒绝与旧的未 reset 实验混用输出目录。
  原仓库 Qwen3 解析器仍优先寻找 `<answer>`，对标准 `Action:` 可能触发其原有
  格式修复调用；这里保留这一官方行为，不另改解析器。
  每次发送模型请求时在最新用户消息末尾追加 `/no_think`，并通过 Ollama
  原生 `/api/chat` 设置 `think: false`，避免兼容接口忽略关闭思考参数。
  `Thought:` 调查理由仍是论文 Agent 的输出格式，与模型隐藏思考模式不同。
- 32K 上下文，单条回答最多 4096 tokens；默认每请求 180 秒、每题 600 秒软上限
  （调用间检查，最后一个在途请求可能超出）。题目为完整 test 集 seed=42 抽样，
  与前几轮的 10 题相同。每题最多 24 次 SQL 尝试加一次提交。
- 裁判使用根目录 `.env` 中的 ParaTera 地址/密钥，模型单独指定；SQL 密码自动从
  `docker/excytin-mysql/.env` 加载。不会改写全局模型配置。
- 直接复用论文 evaluator 的提示词、reflection 和 reward 算法；当前 DeepSeek 裁判
  与论文 GPT-4o 不同，不应作为官方可比成绩。统计包含总/平均 reward、正确率、
  SQL 成功次数、调查耗时以及调查/裁判 token 用量。
- 输出 `summary.md`、`summary.json`、`manifest.json`、`progress.json`、`run.log`，
  `cases/<case>/result.json` 保存逐题轨迹与评分，`llm.jsonl` 保存实际带 `/no_think`
  的请求与模型输出。
- 相同命令重新执行会复用已有调查，裁判失败可补评；不会自动重跑调查失败或中断
  的题目，避免覆盖失败记录。完整新实验请更换 `--output` 目录。
- API/数据库异常、思考未关闭、裁判解析失败均不会冒充 0 分；有未评分题目时，
  整体 reward 为 `null`，进程非零退出。正常耗尽动作预算但未提交计 0 分。

### ReAct + Fancybag

接入论文原版 `ReActAgent`：保留 3 条固定示例、完整对话历史、动作解析及
`Observation:` / `observation:` 停止词。模型传输使用 Fancybag，不注入本项目记忆。
适配层为上游缺失的 Azure 凭据模块提供空占位；实际凭据仍读取 `FANCYBAG_API_KEY`。
源文件与示例内容哈希、示例顺序写入 manifest，支持原有评分和断点续跑。
依赖与上面的 PromptSauce 相同。

先验证 1 题（Table 3 的每次尝试 15 动作预算）：

```bash
.venv/bin/python -m src.benchmarks.external_agents.resume \
  --agent react --provider fancybag --model deepseek-v4.1-flash \
  --sample-size 1 --seed 42 --workers 1 --max-steps 15 \
  --output results/react/fancybag-deepseek-v4.1-flash/smoke1
```

全量 589 题：

```bash
.venv/bin/python -m src.benchmarks.external_agents.resume \
  --agent react --provider fancybag --model deepseek-v4.1-flash \
  --workers 3 --max-steps 15 \
  --output results/react/fancybag-deepseek-v4.1-flash/full589-steps15
```

如需与现有 25 动作的 Base/Strategy 结果对齐，改为 `--max-steps 25`，
并使用新的 `full589-steps25` 目录。相同命令可续跑；调查与裁判均默认使用所选接口，
单独设置的 `EXCYTIN_JUDGE_*` 环境变量仍优先覆盖裁判配置。

### ExpeL + Fancybag

使用论文发布的 151 条 insights 和 237 条成功训练轨迹，保留原版提示词、
动作解析、完整调查历史和 `all-mpnet-base-v2` 的 context + question 向量 top-1 检索。
不重新训练规则，不使用本项目 SM/PM/EM。发布文件中的 key/trajectory 错配按
轨迹初始提问重新匹配训练集，无法唯一匹配时终止；修正数量及文件哈希记录在 manifest。
修正版经验文件保存到 `runtime/expel/`，不改动发布文件。

安装附加依赖（再次 `uv sync` 后需要重装）：

```bash
uv pip install --python .venv/bin/python -r external_agents/requirements-expel.txt
```

首次运行会下载 Hugging Face embedding 模型。每个进程只加载一次模型与经验索引，
各题检索共享索引；模型权重缓存后不必重复下载。调查与评分默认均使用 Fancybag。

先跑 1 题：

```bash
.venv/bin/python -m src.benchmarks.external_agents.resume \
  --agent expel --provider fancybag --model deepseek-v4.1-flash \
  --sample-size 1 --seed 42 --workers 1 --max-steps 15 \
  --output results/expel/fancybag-deepseek-v4.1-flash/smoke1
```

全量 589 题（与 ReAct 同为每题 15 动作，支持相同命令断点续跑）：

```bash
.venv/bin/python -m src.benchmarks.external_agents.resume \
  --agent expel --provider fancybag --model deepseek-v4.1-flash \
  --workers 3 --max-steps 15 \
  --output results/expel/fancybag-deepseek-v4.1-flash/full589-steps15
```

复现边界：这是发布规则的在线评测，不包含使用 Fancybag 重新进行规则提炼；
修正了上游经验 key 错配，并缓存进程内向量索引，均在 manifest 留痕。

### ReAct / ExpeL 单步交互修复版（single-step-v2）

默认全量命令：

```bash
./scripts/run_react.sh
./scripts/run_expel.sh
```

两条命令分别执行；需要并行时使用两个终端。追加 `smoke1` / `smoke10`
可运行固定 seed=42 的 1 / 10 题，默认 `full` 为全量 589 题。
均为 Fancybag `deepseek-v4.1-flash`、15 动作、3 并发；`smoke1` 使用 1 并发。
可用 `WORKERS=1 ./scripts/run_expel.sh` 修改并发。

新结果写入 `results/{react,expel}/fancybag-deepseek-v4.1-flash/single-step-v2/`，
不会复用旧版结果。相同脚本和模式可断点续跑。

该版本增加本地动作边界：只解析第一个完整 Action，丢弃其后的模型生成内容，
不把模型生成的 Observation 加入历史。SQL 中字符串、反引号、分号和方括号保持原样，
SQL 仍经过环境只读单语句校验。无成功 SQL 时禁止提交；格式错误最多重试一次，
重试仍失败则显式报错，不从后续文本寻找答案。两个 Agent 都发送 Observation stop，
本地边界不依赖服务端是否遵守 stop。每次请求附带简短单步约束，
`workflow.boundary_events` 保存截断/拒绝事件；manifest 记录协议及实现哈希。
这些是运行协议修正，已不再使用上游宽松解析器。

### Qwen Code 统一脚本

```bash
./scripts/run_qwen_code.sh          # 全量 589 题
./scripts/run_qwen_code.sh smoke10  # 固定抽样 10 题
./scripts/run_qwen_code.sh smoke1   # 固定抽样 1 题
```

与 ReAct / ExpeL 共用 resume 入口、中文进度输出和文件布局。默认 Fancybag
`deepseek-v4.1-flash`、15 动作、3 并发（smoke1 为 1）。全量目录为
`results/qwen-code/fancybag-deepseek-v4.1-flash/single-step-v2/full589-steps15/`，
包含 `manifest.json`、`progress.json`、`summary.json`、`cases/` 和重试时的 `attempts/`。
`single-step-v2` 是本组实验的目录标签；Qwen Code 保持原生工具调用循环，不套用论文 Agent 的文本动作解析器。
重复同一命令跳过已评分题，补评裁判失败题并重跑调查失败题；旧 batch 输出保留在原目录。
