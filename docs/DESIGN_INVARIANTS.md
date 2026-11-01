# ScholarGuide 设计不变量（v2 实现约束）

状态：实现约束，用于 v2（有状态多轮）落地。与 `ARCHITECTURE.md` 第 5、6 节一致；不改动
`PROJECT_PLAN.md` 与 `PROPOSAL_*.md` 中已冻结的承诺。

一句话：**每条不变量都能落成一个断言函数或一个故障用例，否则不写进来。**

## 为什么需要这一层

v2 新增的是「时间」这个维度：论文证据不变，但任务判断会在反馈后失效。没有显式不变量时，
最常见的退化是「看起来还能跑，但旧判断悄悄混进新结果」。以下六条分别锁住
证据、复用、阈值、交付、成本、动作合法性。

## 六条不变量

### I1 证据完整性

任何 `relevance >= 1` 的判断，必须同时满足：`evidence_verified = true`，且
`evidence_quote` 是论文摘要规范化后的**连续子串**（长度 >= 12）。

- 违反症状：推荐列表里出现没有引文的论文；或引文在摘要更新后失效却未降级。
- 强制点：写入 assessment 时断言；`select_papers` 只接受已验证行（不靠读取时兜底）。
- 断言：`assert_assessment_evidence(row)`
- 对应贡献：C3（有依据推荐）

### I2 键完备

判断的可复用性只由 `assessment_key` 决定：

```text
content_hash  = hash(title, abstract)                    # 归一化后
semantic_key  = hash(topic, purpose, category, model, prompt_version)
assessment_key = (paper_id, content_hash, semantic_key)
```

`task_revision` 只用于审计与回溯，**不参与**复用判定。任一组成字段变化即视为不可复用。

- 违反症状：用户改了研究用途，旧的相关性判断仍被拿来排序；或改 K 就全量重评。
- 强制点：复用前逐项比对；事件记录 `reused` 与 `reason`（如 `semantic_key_changed`）。
- 断言：`assert_reuse_key(assessment, current_task, current_model)`
- 对应贡献：C2（持续证据管理）

### I3 阈值只源于可数量

弃权、置信度与「是否部分交付」的判据，只能由**可数事实**构成：已验证引文数、配额覆盖数、
候选总数、来源分布。模型输出的 `relevance`（0/1/2）未校准，只允许参与排序，不允许作为阈值输入。

- 违反症状：同一条任务重跑两次，弃权与否随模型打分抖动而变。
- 强制点：现有弃权/部分交付判据只读 `ControlState` 的可数字段（gaps、预算、候选数）；
  `relevance` 只参与排序。新增阈值必须通过 `FactCounts` 构造。
- 断言：`assert_threshold_source(facts)`（拒绝非 `FactCounts` 输入）
- 对应贡献：C3

### I4 交付一致性

一次 FINISH 交付内，所有进入清单的判断必须对当前任务有效（无 stale）。存在 stale 时，
要么重评，要么把该论文移出候选；不允许新旧判断混排后交付。

- 违反症状：反馈后只重算了部分候选，列表一半按新用途、一半按旧用途。
- 强制点：`verify_delivery()` 检查每个 `assessment_key` 与当前任务/模型一致，否则拒绝交付；
  强制部分交付路径同样过滤 stale 与无证据项。
- 断言：`assert_delivery_consistent(check)`（`reuse.py`）
- 对应贡献：C2、C3

### I5 成本守恒

每个动作记录 `reserved`、`settled`、`refunded`，满足
`0 <= settled <= reserved` 且 `settled + refunded = reserved`；失败动作必须全额退。
`cost_units` 口径固定：1 次 search = 1，1 篇 assess = 1，1 次 plan/refine = 1。

- 违反症状：trace 里的调用数与 metrics 对不上；预算耗尽却没有记录。
- 强制点：预算结算与状态变更同一事务提交；run 结束时做一次总量校验。
- 断言：`assert_budget_conservation(ledger_rows)`（`controller.py`）
- 对应贡献：C4（质量–成本–交互权衡，实现层面）

### I6 动作合法性由程序枚举

`allowed_actions(state, budget)` 是唯一合法动作来源。模型只能从中选择并给出参数；
越界动作被拒绝并记 `invalid_action`。模型不能凭输出文本制造 `FINISH`。

- 违反症状：缺口仍存在时状态标成 `complete`；预算为 0 仍在搜。
- 强制点：controller 分发前校验；被拒后把合法集合写回观察。
- 断言：`assert_action_allowed(action, state)`（`controller.py`）
- 对应贡献：C1、C3

## 与冻结规格的对应关系

| 不变量 | ARCHITECTURE 对应 | 测试类别 |
|---|---|---|
| I1 | §4.D 程序可检查：引文子串；§4.C evidence_quote | 单元 + 注入 |
| I2 | §5 正确复用：content_hash / semantic_key / assessment_key | 单元 |
| I3 | 本文新增（阈值来源约束） | 单元 |
| I4 | §4.D `stale_assessment_ids`；§5 表「主题/用途/类别改变」 | 故障 |
| I5 | §6 预算与上限 | 单元 |
| I6 | §4.B「程序保留动作合法性验证」；§6 第 5 条 | 故障 |

`unsatisfiable` 是独立的 partial reason：任务约束本身冲突（如配额大于 K）时直接报告，
不伪装成「搜不到」。


## 四个只属于本系统的故障用例

1. **陈旧判断污染**：改 `purpose` 后，旧判断不得被标记为有效。
2. **半更新**：交付前所有判断的 `assessment_key` 必须一致指向当前任务。
3. **澄清上瘾**：问了也不改变清单的歧义，不得触发 ASK。
4. **假收敛**：配额必然不可满足时，状态必须是 `partial`（reason `unsatisfiable`），不是 `complete`。

## 明确不做（避免把工具设计当成功能清单）

- 不引入多路召回融合、重排模型、向量库：单源 arXiv + 摘要级理解是已冻结范围。
- 不引入多 Agent、Critic Agent：§4.D 明确不增加每步运行的评审者。
- 不引入 OAuth、遥测、跨用户共享缓存：单用户本地系统不需要，且会稀释 C1/C2 的评测。
