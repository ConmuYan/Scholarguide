# FB-1.1 评测轮采集交付 · Qu Kaiyuan（竞品 #2 Ai2 Asta Paper Finder）

本目录为 FrontierLitBench **FB-1.1 评测轮**的原始运行采集，与 `frontierlitbench/`（Karen Ho 的 FB-1.0 Track C 语料构建）同级，互补构成 FrontierLitBench 两部分工作。

## 交付内容

| 文件 | 说明 |
|---|---|
| `Ai2Asta-deep-FB1.1.zip` | 正式交付包（kit 命名规范：产品-模式-FB1.1），含 prompts×15、raw×45、normalized×45、run-log.json |
| `runlog_system_block.png` | run-log 的 system 块截图（45 次运行配置登记，供核对配置一致性） |

## 运行概况

- **产品**：Ai2 Asta Paper Finder（asta.allen.ai，竞品 #2）
- **配置**：Deep 模式（产品默认）、免费匿名档、45 次运行（15 题 FB-01~FB-15 × 3 次）全程一致
- **结果**：45/45 全部 `status: complete`，无 failed/blocked
- **回答分布**：23 次 JSON 完整回答（5 篇为主）、17 次正文列举回答、5 次真实零结果/澄清请求（忠实记录产品行为，非采集缺陷）
- **转录**：23 次 JSON 逐字转录 + 17 次正文严格解析 + 5 次零结果存 `{"papers": []}` + 原文说明；全量 45 文件忠实性校验通过（normalized 每字段可在 raw 中原样找到、顺序一致）
- **纪律**：提示词原文一字不改独立提交；未补 DOI、未改序、未删重；判不了字段填 null；zip 内无账号/API key/私人链接

## 备注

- FB-01-r1/r2 的 raw 为同一次有效运行的重新完整抓取（初抓因网页折叠残缺），run-log notes 已注明。
- 组长后续动作（导入、合并去重、统一裁决、重评分、宏平均汇总）按 kit 协议由负责人执行，本交付只含采集与忠实转录。
