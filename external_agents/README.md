# 外部 Agent 与 ExCyTIn 测评

此目录集中存放 OpenCode、Qwen Code、Cline 的源码、锁定的 CLI 安装包和运行结果。
使用各项目原生 CLI 调查循环，不重新实现其 Planner，也不向它们注入 SOCTraceAgent 的 SM/PM/EM。

## 目录

- `repos/opencode/`、`repos/qwen-code/`、`repos/cline/`：官方 Git 仓库的下载副本，忽略提交。
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

三个 CLI 的模型请求都经过本地 OpenAI-compatible 代理。代理使用根目录 `.env` 的
`PARATERA_API_KEY`、`PARATERA_BASE_URL`、`PARATERA_MODEL`，真实模型密钥及数据库密码不传入 CLI。
MCP 子进程只持有临时本地访问令牌。数据库密码从 `docker/excytin-mysql/.env` 加载。
生成的 CLI 配置放入临时目录，结束后删除；不覆盖用户已有的 CLI 配置。

默认同时运行 3 个 Agent/题目任务，共用进程级 2500 RPM / 500 万 TPM 的限流器，
按已有 `PARATERA_RATE_UTILIZATION=0.8` 留余量。分别启动多个 runner 进程不共享额度。
代理固定 temperature=0、关闭 thinking；上游完成后转换为 SSE，保留原生工具调用。
这会改变首 token 时延，不能用于比较原生流式交互时延。

每题最多 24 次 SQL 尝试（含错误和空结果）+ 1 次提交；到达查询预算后只能提交。
另有 100 次模型请求保护和默认 600 秒超时。没有 submit_answer 会明确记为
`no_submission` / `timeout`，不会从普通对话中猜测最终答案。

代理仅向模型暴露查询、提交以及必要的 MCP 分发/工具发现接口；
OpenCode 配置还拒绝其他工具，Qwen 禁用核心文件和 Shell 工具。
临时工作目录及工具约束不是操作系统沙箱，不能用来执行不可信插件。
这是统一工具条件下的 CLI 基线，不是未经配置的默认 CLI，也不是论文官方 ExcytinEnv。

## 运行

先确保题目数据存在，所选 incident 的 MySQL 容器健康。

三个 Agent 跑同一个已知样例（qid 从 0 开始）：

```bash
.venv/bin/python -m src.benchmarks.external_agents.runner \
  --agents opencode qwen-code cline --incident incident_5 --qid 39 \
  --workers 3 --max-steps 25 --llm-eval
```

固定 seed 抽取同一 incident 的 10 题，每个 Agent 使用完全相同的题目：

```bash
.venv/bin/python -m src.benchmarks.external_agents.runner \
  --incident incident_5 --sample-size 10 --seed 42 --workers 3
```

单独运行：`--agents cline`、`--agents qwen-code` 或 `--agents opencode`。
不传 `--llm-eval` 时只计算严格匹配和辅助包含匹配，不额外调用裁判。
传入该参数时复用项目的 `llm_judge_evaluation`：答案正确为 1，否则按标准步骤倒序加权。
包含匹配不能替代正确率；附带说明的正确答案可能严格匹配为 false。

`manifest.json` 记录题目、模型、版本、预算及适配条件；`summary.json` 汇总运行结果。
每题 `result.json` 中保存 `answer`、`actions`、`usage`、`llm_calls`、耗时及评分。
`usage` 是上游模型返回的原始使用量，额外裁判调用不在此数组中。
若与 SOCTraceAgent 比较，应采用同样题目、模型、预算、裁判；分别报告是否提供记忆。

## 官方来源

- OpenCode：https://github.com/anomalyco/opencode
- Qwen Code：https://github.com/QwenLM/qwen-code
- Cline：https://github.com/cline/cline

本地完整源码和各自许可证位于 `repos/`。下载目录不作为嵌套 Git 仓库提交到本项目。

## 五框架配对批量实验

三个原生 CLI、原论文仓库 `BaselineAgent` 和当前 SOCTraceAgent：

```bash
.venv/bin/python -m src.benchmarks.external_agents.batch \
  --output runtime/excytin_bench/five_agents_20260914 \
  --per-incident 15 --seed 20260914 --workers 3
.venv/bin/python -m src.benchmarks.external_agents.analyze \
  runtime/excytin_bench/five_agents_20260914
```

`incident_38` 的测试集只有 11 题，自动全部使用，不重复抽样、不使用训练集补足，
所以默认选择 116 道题，执行 580 次调查。输入中只包含 context 和 question。
五者模型请求与裁判请求都通过同一进程限流器。论文框架使用原始提示词、历史和动作解析，
只替换传输层；我们的框架使用 Neo4j 记忆。

同一输出目录再次运行会读取原 manifest，并跳过已有完整结果，不重复付费调查。
模型裁判报错的已完成调查可复用原答案补评。已有失败记录不会自动重跑，避免只保留成功尝试。
`progress.json` 记录进度，`analysis.md` / `analysis.json` 汇总配对结果和成本。
论文 Baseline 依赖实验环境的 `ag2==0.14.0` 和 `azure-ai-inference==1.0.0b9`；
这是原仓库兼容依赖，不能替换为不提供 `autogen` 导入的 AG2 1.x。

## Ollama Qwen3-32B：关闭思考，论文 Agent 10 题

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
