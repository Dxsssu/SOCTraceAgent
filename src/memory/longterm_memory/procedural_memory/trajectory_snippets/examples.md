# 训练片段示例与回放证据

这些例子来自最终目录的真实训练支持。检索测试使用已登记的实体概念和意图原文；数据库回放使用训练参数，并验证每个派生参数来自本次上游结果。它们不代表自然语言选片段或新事件调查准确率。

## 邮件 ID 查询发件人

已知 InternetMessageId，查询 SenderDisplayName 与 SenderFromAddress。

- 训练轨迹：222；片段：`proc_11dbe257722388e085cd7e89`。
- 起点概念：`Email_ID`；意图：按上一步得到的 InternetMessageId 查询发件人
- 精确检索：通过；查询步数：1。

```sql
SELECT InternetMessageId, SenderDisplayName, SenderFromAddress FROM EmailEvents WHERE InternetMessageId IN (:p1) LIMIT 100
```

训练回放返回 3 行。`:p1` 为 string[]，运行时展开为独立绑定参数；不把邮件 ID 拼接进 SQL。

## 五步邮件调查链

邮件处置记录 → InternetMessageId → NetworkMessageId → URL → 点击账户 UPN → SID；共五次查询。

- 训练轨迹：178；片段：`proc_f14be15152e7891ef704cde0`。
- 起点概念：`Action`；意图：按点击账号UPN查询身份登录记录中的SID候选
- 精确检索：通过；查询步数：5。

| 步骤 | 输出字段向下一步传递 |
| --- | --- |
| s1 → s2 | `InternetMessageId` → `InternetMessageId` |
| s2 → s3 | `NetworkMessageId` → `NetworkMessageId` |
| s3 → s4 | `Url` → `Url` |
| s4 → s5 | `AccountUpn` → `AccountUpn` |

实际逐步返回行数：1 → 1 → 1 → 2 → 21；全部步骤通过。

## 同一邮件的双分支

第一步同时输出两种邮件 ID：InternetMessageId 给发件人分支，NetworkMessageId 给 URL 分支。

- 训练轨迹：222；片段：`proc_3cce7d309146babc5a846c3e`。
- 起点概念：`Action`；意图：从投递后移除/隔离事件出发，回溯邮件发件人与邮件内URL
- 精确检索：通过；查询步数：3。

| 步骤 | 输出字段向下一步传递 |
| --- | --- |
| s1 → s2 | `InternetMessageId` → `InternetMessageId` |
| s1 → s3 | `NetworkMessageId` → `NetworkMessageId` |

实际逐步返回行数：6 → 3 → 3；全部步骤通过。

## 六步点击事件扩展

URL 点击记录分别按 IP、账户 UPN、ReportId 扩展，再对 IP/账户结果做去重查询；共六步。

- 训练轨迹：227；片段：`proc_85e0259c4737152631239a6d`。
- 起点概念：`URL`；意图：从一个已知恶意URL点击事件出发，提取设备IP、用户账户和报告ID等标识，并在这些维度上检索是否存在其他URL点击事件。
- 精确检索：通过；查询步数：6。

| 步骤 | 输出字段向下一步传递 |
| --- | --- |
| s1 → s2 | `IPAddress` → `IPAddress` |
| s1 → s3 | `AccountUpn` → `AccountUpn` |
| s1 → s4 | `ReportId` → `ReportId` |
| s2 → s5 | `IPAddress` → `IPAddress` |
| s3 → s6 | `AccountUpn` → `AccountUpn` |

实际逐步返回行数：1 → 1 → 1 → 1 → 1 → 1；全部步骤通过。

## 未通过的路径也保留

第 223 条轨迹包含 JSON 字段提取 IP、再查告警证据的候选关系。虽然原观察中的字段和值可以核对，上游查询达到原始 LIMIT 10，结果可能截断，因此相关多步候选未获验证通过，详见 `excytin/rejected_replays.json`。没有为通过测试而扩大 LIMIT 或省掉必要依赖。

未知意图检索返回 `route="semantic"`；上述为离线验证示例；片段保留 candidate 状态，当前已由 session.py 接入 SQL workflow，实时运行仍需核验 Schema、输入与依赖。
