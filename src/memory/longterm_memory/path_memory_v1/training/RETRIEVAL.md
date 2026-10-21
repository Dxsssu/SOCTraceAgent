# 独立 LTM 检索实验

实现位于 `training/retrieval.py`，未接入或修改 Planner / Executor / Expert / Verifier workflow。当前使用完整训练记忆目录，旧版单案例检索器保持兼容。

## 检索与执行

1. LLM 仅从 context/question 解析 known、wanted、事件背景与约束；known 的值必须逐字出现在输入中，类型来自 SM 概念目录。constraints 仅允许答案的事件、关系、时间、范围要求，每条需要 constraint_sources 中的原文依据；不能把“输入尚无答案”、模型操作规则、概念注意事项当作答案约束。
2. PM 按已知类型、目标类型及当前日志实际存在的事件标题筛选，候选粒度为 PM + scenario + route + seed concept。标题存在只是执行前提，不是适用性结论。
3. TF-IDF 词项余弦召回 top-8，LLM 判断语义适用性、约束及输入绑定。可注入 embedding 回调，采用 0.3 词项 + 0.7 向量余弦混合排序。未配置 embedding 时如实使用 lexical + LLM，不产生虚构的向量得分。
4. PM 命中后，按 ID 加载 EM，执行类型明确的顺序依赖。每一步消费前一步实际输出，保留所有候选及告警来源；中途空结果/SQL错误停止该路线。
5. 无适用 PM 时，召回输入类型匹配的 EM。事件与可输出类型通过只读 SQL 获取：必须是包含当前实体的实际告警，且存在对应输出类型（排除仅返回同一输入值的无进展关联）。探测不返回未知实体的具体值，计入元数据查询次数。LLM 选择可产生下一步线索的操作，不要求一步直接返回最终答案。
6. EM 无匹配时，从 SM 概念、字段和关联键生成候选操作，LLM 选择输入/输出类型及事件范围，再由受限编译器产生参数化只读 SQL。这个版本只支持已有物理适配器提供的“同告警实体关联”，不支持任意跨表关联或任意生成 SQL。
7. 新实体加入独立的工作实体列表，回到 PM 检索。默认最多 5 轮；相同候选与同一组可用输入不重复尝试。每轮 PM/EM/SM 选择和执行证据均留档。
8. 独立 LLM 证据审核仅能返回已观测且出现在其证据上下文内的候选。返回 `answer_candidate`，不等价于经过官方答案判定的正确答案。摘要每步最多 40 条证据，显式标记省略数量，完整结果仍保留在 trace。没有足够证据时继续调查或明确停止。

`needs_evidence` 与 `no_match` 均触发下一层回退；不会根据 LLM 的请求凭空绑定缺失实体。当前记忆的 route 输入是单一概念的一组实体；未声称支持尚未存储的多种输入的 AND 连接或 PM 自动组合。

## 数据与边界

- 检索器只加载 semantic/episodic/procedural 三份目录；不读取 case_memory_index、官方路径、训练题答案或来源案例 ID。
- 测试驱动仅传 context/question；incident 是外部提供的数据库范围。评价时才读取参考答案。
- SQL 使用原始 CSV 派生的会话临时投影，原始数据库表不变。图规范化标识仍沿用现有回放适配器，不能据此声称对任意大小写、URL或弱标识都已实现实体消歧。
- 当前记忆由这些训练题构建，因此测试是训练内检索与执行验证，不是独立测试集准确率。
- PM/EM 召回失败可以是真实知识缺口，也可能是意图识别、召回 top-k 或 LLM 判断失误；报告保留候选 ID、判断理由和 SQL 证据用于区分。
- Fancybag 请求超时/连接异常及 408/429/500/502/503/504，等待 5–10 秒最多重试 2 次。JSON 格式错误反馈原始响应和解析错误，最多重试一次；候选 ID/输入类型绑定错误也最多反馈修复一次。约束检查不通过则拒绝选择或继续调查，不返回未验证答案。其他错误直接记录。此配置只属于独立实验。

## 运行

```bash
.venv/bin/python -m src.memory.longterm_memory.path_memory_v1.training.test_retrieval_live \
  --provider fancybag --model deepseek-v4.1-flash \
  --cases incident_55_train_000 incident_5_train_000 incident_34_train_000 \
  --ablations \
  --output src/memory/longterm_memory/path_memory_v1/training/retrieval_test_results_v2.json
```

`--ablations` 额外在第一题上屏蔽 PM，以及同时屏蔽 PM/EM，验证回退分支；不是自然未命中的统计。正常三题不屏蔽记忆。`--embedding` 需配置提供方的 embedding 模型和可用接口；默认 Fancybag 使用 LLM 辅助检索。

```bash
.venv/bin/python -m pytest \
  src/memory/longterm_memory/path_memory_v1/tests \
  src/memory/longterm_memory/path_memory_v1/training/tests -q
```

初版实验保留在 `retrieval_test_results.json`；改进后的实验在 `retrieval_test_results_v2.json`。初版正常三题均返回参考答案（其中 URL 尾斜杠差异采用额外规范化指标报告），两项回退测试因模型错误地要求单步直接返回最终答案而失败；改进选择提示词后重新测试，不隐藏初版失败记录。

复测记录另外保存在 `retrieval_normal_v3.json`、`retrieval_em_v3.json`、`retrieval_sm_v3.json`、`retrieval_single_step_v4.json`、`retrieval_em_final.json` 和 `retrieval_pm_final.json`。每份文件保留模型响应、SQL、绑定、结果和评分，开发中失败的实验未删除。

## 开发中定位到的问题

- 单步候选错误地按“是否直接返回最终答案”判断，拒绝桥接实体：选择提示词明确允许中间线索，且记录空结果/历史尝试。这一多步探索行为仍需更多题验证，不能因单步分支测试通过就声称稳定。
- 模板类型可用但具体告警不存在目标实体：增加只返回事件标题和可输出类型的只读元数据探测。
- 模型把 URL 和主机混绑给同一单类型操作：每个候选显式给出 bindable_entity_ids；结构校验失败反馈一次再重试。
- 意图误将“原文未给答案”当作永久约束，使查到答案反而被拒绝：意图提示词禁止这种约束，并要求约束原文来源。失败报告与修正后的报告均保留。
- JSON 空响应：独立检索器反馈解析错误并重试一次；这不是 SQL 重放，修复期间没有执行新查询。

最终测试分组见 `RETRIEVAL_RESULTS.md`。PM 优先并不保证最少 SQL；在 Manatee Tempest 样例中 PM 为两步，而 EM/SM 可直接通过目标告警查询一步得到答案。
