# 通信设计

## 目标

对于当前 `Planner / Executor / Reviewer` 三角色版本的 SOCAgent，通信应分成两层：

1. `业务通信层`：负责 Agent 之间的真实协作
2. `观测消息层`：负责日志、审计、调试和未来前端展示

第一版实现应优先保证业务通信简单、稳定、可追踪。

## 推荐方案

不要让 `Planner`、`Executor`、`Reviewer` 通过彼此直接消费“聊天消息”来协作。

应采用以下方式：

1. 以共享持久化状态作为主通信机制
2. 以结构化消息作为辅助审计和观测机制

具体来说：

- `Planner` 读取 `Event` 和最新的 `RoundReview`，然后写入新的 `TTT` 快照
- `Executor` 读取最新的 `TTT`，领取一个可执行叶子节点，执行工具后写入 `Execution`
- `Reviewer` 读取当前轮的 `Execution` 结果，总结后写入 `RoundReview`

因此，真实的业务交接链路应为：

`Event -> TTT -> Execution -> RoundReview -> TTT`

## 为什么这比直接照搬 DeepSOC 更合适

`deepsoc` 的真实协作机制，本质上也是数据库状态驱动，RabbitMQ 主要用于前端通知。

对于当前精简后的三角色架构：

- 不需要再保留 `Task -> Action -> Command` 这些中间通信层
- 第一版不需要 RabbitMQ
- 必须保留显式、可持久化的状态流转

这样更贴合当前项目的溯源流程，也更容易实现和维护。

## messaging 目录的职责范围

`src/messaging` 只负责定义消息契约和事件类型，不负责实现工作流本身。

建议包含以下内容：

1. `message_types.py`
   定义内部消息或事件类型，例如：
   - `TTT_INITIALIZED`
   - `LEAF_CLAIMED`
   - `EXECUTION_COMPLETED`
   - `ROUND_REVIEW_CREATED`
   - `TTT_UPDATED`

2. `models.py`
   定义轻量消息封装结构，用于审计和追踪，例如：
   - `event_id`
   - `round_id`
   - `from_role`
   - `to_role`
   - `message_type`
   - `payload`
   - `created_at`

3. `bus.py`
   定义一个最小消息总线接口，例如：
   - `publish(message)`
   - `list_messages(event_id, round_id=None)`

第一版 `bus.py` 应优先面向持久化存储实现，而不是先做队列驱动实现。

## 业务通信规则

业务协作建议遵循以下规则：

1. `Planner` 不直接向 `Reviewer` 下发执行指令
2. `Reviewer` 不直接改写 `TTT`
3. `Executor` 不负责重规划
4. 任意跨 Agent 的交接，都必须对应一个可持久化的状态对象

这意味着：

- `Planner -> Executor`：通过 `TTT` 叶子节点交接
- `Executor -> Reviewer`：通过 `Execution` 交接
- `Reviewer -> Planner`：通过 `RoundReview` 交接

## 观测消息规则

结构化消息依然有价值，但它们只应用于：

- 审计
- 调试
- 回放
- 未来前端展示

例如：

- `Planner` 发布 `TTT_INITIALIZED`
- `Executor` 发布 `LEAF_CLAIMED` 和 `EXECUTION_COMPLETED`
- `Reviewer` 发布 `ROUND_REVIEW_CREATED`

这些消息不应成为唯一事实来源，唯一事实来源应始终是共享持久化状态。

## 第一阶段编码建议

`src/messaging` 的第一阶段建议只做三件事：

1. 定义消息类型枚举
2. 定义轻量消息封装结构
3. 定义最小消息总线接口

当前阶段不要引入 RabbitMQ。

只有在后续确实需要以下能力时，再考虑增加消息队列：

- 对外流式输出
- 实时前端推送
- 多机部署下的解耦通信

