# 复用来源与实现边界

## GPT Researcher

- 仓库：https://github.com/assafelovic/gpt-researcher
- 固定参考 commit：`5cdad9cb434754188b78bd998df18dd8d502cf7e`
- 原文件：`gpt_researcher/retrievers/arxiv/arxiv.py`
- 原始链接：https://github.com/assafelovic/gpt-researcher/blob/5cdad9cb434754188b78bd998df18dd8d502cf7e/gpt_researcher/retrievers/arxiv/arxiv.py
- 原许可证：Apache License 2.0，副本位于 `vendor/LICENSE-gpt-researcher`。
- 本项目衍生文件：`vendor/gptr_arxiv.py`，文件头已说明修改。

保留了 `ArxivSearch` 的检索职责、`search(max_results)` 接口，以及 `title/href/body` 结果映射。为脱离完整包初始化，将 `arxiv` SDK 调用改为注入 HTTP 函数和标准库 Atom XML 解析；加入稳定 ID、年份、作者字段，并把 ID 查询独立成 `id_list` 请求。结果链接改为摘要页，排序仍支持相关性/提交时间。

**准确描述：当前是开源检索组件适配 + 独立精简 workflow。** 没有把复制一个小模块描述为完成整个 GPT Researcher 的架构重构。主 Agent、通用研究 conductor、memory/embedding、全文浏览和长报告组件不进入精简入口。

`evaluation/upstream.py` 提供原始 GPT Researcher 的独立进程入口，固定 commit 校验，直接调用原版 `conduct_research()` 和 `write_report()`。是否完成该基线运行见 `VERIFICATION.md`；缺依赖/配置时明确失败，不使用自写流程冒充原版。

## 用户现有 pi 工作流

参考本次用户授权使用的本地代码：

- `~/.pi/agent/skills/idea-spark/scripts/search_arxiv.py`：HTTP/XML、请求节流和有限重试的实现经验。
- `~/.pi/agent/skills/idea-spark/scripts/search_openalex.py`：OpenAlex 摘要倒排索引还原与元数据映射。
- `~/.pi/agent/skills/idea-spark/scripts/_merge.py` 与 `run.py`：查询来源、缓存和判断/执行分开的设计。
- `~/.pi/agent/extensions/academic-search.ts`：统一 PaperRecord 和 source status 的设计。

本实现重新组织成项目内的小模块，不依赖 pi 运行时或全局扩展。用户可显式配置 `SG_API_KEY=@pi:deepseek` 引用本机已有 API key；其他机器直接填写自己的 key 即可。检索模块不读取机构浏览器配置。个人既有代码的权属和公开许可未在本次审查中确认；本文件不擅自为其重新授权。未来公开发布前应确认团队的授权与归属写法。

相对于已检查的 pi 实现，本项目：

- arXiv 精确查找走 `id_list`，核对返回 ID，没有模糊查询首条回退。
- 通过 DOI/arXiv/稳定 ID 的交集关联记录，同时保留所有实际查询来源。
- 空/失败响应不缓存；有效缓存使用参数与格式版本键。
- 默认只启用一个来源，避免多源固定顺序截断问题。
- 不继承研究缺口搜索中的“纯综述排除”策略，survey 是有效推荐类别。
- 以最多两轮的显式 Python 流程取代 pi 主 Agent 临场编排。

## 方法参考

- FlowSearcher，ICLR 2026：https://iclr.cc/virtual/2026/poster/10011673 。参考其模块化执行与检查后继续的结构，没有实现动态 DAG、分层记忆或经验复用。
- Corrective Retrieval Augmented Generation：https://arxiv.org/abs/2401.15884 。参考评估检索结果后触发纠正搜索的思想，没有训练其 T5 evaluator，也未完整复现 CRAG。
- Adaptive Retrieval Without Self-Knowledge? Bringing Uncertainty Back Home，ACL 2025：https://aclanthology.org/2025.acl-long.319/ 。参考同时报告质量与效率的评测原则。

不能引用原论文效果作为本项目效果。项目贡献是有限范围内的整合、任务适配、可靠性处理和可复核评测。
