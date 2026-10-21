# LTM 独立检索与执行测试结果

模型：Fancybag / deepseek-v4.1-flash。只修改独立 LTM 模块，未接入 workflow。
最终固定版本：4 道不同训练题，4 次正常检索 + 4 次显式屏蔽记忆层的回退测试。严格字符串匹配 7/8；另 1 次仅 URL 尾斜杠不同，去除尾斜杠后匹配。57 项单元测试通过。

| 题目 | 模式 | 实际使用 | 调查 SQL | 元数据 SQL | tokens | 结果 |
|---|---|---|---:|---:|---:|---|
| incident_55_train_000 | normal | PM | 3 | 2 | 13840 | URL尾斜杠差异 |
| incident_34_train_000 | normal | PM | 2 | 5 | 12325 | 严格一致 |
| incident_5_train_000 | normal | PM | 2 | 2 | 13871 | 严格一致 |
| incident_5_train_000 | without_PM | EM | 1 | 2 | 7041 | 严格一致 |
| incident_5_train_000 | SM_only | SM | 1 | 2 | 8268 | 严格一致 |
| incident_55_train_025 | normal | EM | 1 | 3 | 6707 | 严格一致 |
| incident_55_train_025 | without_PM | EM | 1 | 3 | 6799 | 严格一致 |
| incident_55_train_025 | SM_only | SM | 1 | 3 | 11324 | 严格一致 |

## 解读

- PM 正常检索覆盖恶意 URL 关联、Manatee Tempest URL、Run Key 账户 SID。设备 ID 单步题自然回退到 EM。
- Manatee Tempest 与设备 ID 两题各自显式屏蔽 PM，以及屏蔽 PM+EM，分别验证 EM 与 SM；这些实验不能统计为自然的 PM 未命中率。
- Manatee Tempest 的 PM 使用两步，EM/SM 使用一步：流程复用优先不保证 SQL 最少。
- 这些题参与过记忆构建，因此不是独立测试集准确率。检索输入没有官方路径、答案、case_memory_index；标签只用于执行结束后的评价。
- 开发阶段多步 URL 题的纯 EM/SM 探索曾失败；最终回退测试覆盖的是上述两个可直接查询的场景，不能据此宣称任意多步探索已稳定。
- 开发中的空响应、混合类型绑定、把“答案未知”当作永久约束等失败记录均保留，未从原报告删除。
- SM 执行目前限定为受控的同告警实体关联编译器，不是任意表 SQL 生成器。

## 原始记录

- [retrieval_multistep_final.json](retrieval_multistep_final.json)
- [retrieval_constraint_fix.json](retrieval_constraint_fix.json)
- [retrieval_single_step_final.json](retrieval_single_step_final.json)
