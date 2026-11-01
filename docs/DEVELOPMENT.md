# 开发指南与当前文档索引

当前开发范围是本地 Python API 与 Web 产品。产品数量范围为 1–10、默认 5，包含连续 Chat、Sources 管理与实际执行事件。工程交付以当前源码和当次验证结果为准；历史测试数量或历史部署记录不能代替本轮验证。

## 来源并发租约升级注意事项（进行中）

新版来源并发控制不再仅凭时间戳回收请求槽。部署前必须停止所有共享同一来源数据库的旧版进程并确认本地操作结束；旧版与新版混跑时，旧版仍可能删除新版正在使用的槽，无法保证并发上限。升级会增量添加 owner 字段，不删除设置或历史。无法验证归属的旧租约默认保留并占槽，可能影响可用性；仅在操作者确认所有旧版客户端及其本地操作已停止后，才能按 `docs/API.md` 的明确确认接口恢复 owner 为空的旧槽，不能直接清空数据库或删除历史。恢复不能证明远端工作已经停止。远端工作可能在本地进程退出后继续，进程退出不能证明远端已停止。不得在持有数据库上下文或受保护来源操作时调用 `fork()`，也不得让 fork 后的子进程继续执行父进程已准入的业务；子进程必须新建自己的 store 和请求。Windows 上的真实 POSIX fork 回归通过 WSL 默认发行版执行，需要该发行版的 Python 与 `jsonschema`；缺失时测试失败，不会伪装为通过。

DeepSeek 来源 key 的新存储使用现有来源数据库字段内的版本化绑定格式，不新增数据库列，也不删除历史配置。旧版程序可能把该封装当作原始 key，因此**不能与新版混用或直接回退后发送请求**；回退前停止所有相关进程，备份数据库，核对凭据和端点，在目标版本的 Sources 设置中由用户重新输入，而不是以旧版代码读取新版绑定。历史未绑定的自定义端点保留但默认不可发送，需用户显式重新确认；环境 key 不自动授权跨 origin。源码回退不等于凭据格式自动回退。

## Web 国际化（2026-10-31）

Web 页面支持简体中文、繁體中文和 English。语言按钮位于主题按钮旁，点击循环切换并写入独立的 `scholarguide-language` 偏好；切换不刷新页面、不调用模型或来源，也不改写输入、草稿、论文标题、摘要、引文和模型生成内容。界面字典由 `web/theme.js` 与各页内嵌字典提供，服务端自由文本在没有结构化翻译键时保留原文。首页文案只保留研究流程和证据边界所需说明。


## 当前入口

| 文档 | 作用 |
| --- | --- |
| [README](../README.md) | 安装、产品页面、模型和来源配置、CLI 快速上手 |
| [API](API.md) | v1 请求与响应、异步操作、事件、取消、重试与示例 |
| [2026-10-28 产品契约](PRODUCT_CONTRACT_20261028.md) | 本轮稳定接口、数量与连续会话要求、模块所有权与验收边界 |
| [2026-10-28 产品审计](PRODUCT_AUDIT_20261028.md) | 问题、责任归属与验收条件；列入审计不等于已修复 |
| [归档说明](../archive/README.md) | 研究文件迁移位置、历史文档解释与保留规则 |
| [上游说明](../UPSTREAM.md) | 已复用接口与实现来源 |

API 文档与产品契约优先解释当前公开行为；审计保留未决问题和验证要求。遇到旧文档与新契约冲突时，以新契约、当前实现和相应回归证据为准，不用旧 proposal 的文件哈希约束产品修改。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| `app.py`、`webapp.py`、`api_v1.py` | CLI、Web 服务、公开 API 与异步操作 |
| `session.py`、`agent_runtime.py`、`state.py` | 会话执行、LangGraph 编排、持久化与恢复 |
| `common.py`、`intent.py`、`workflow.py` | 请求与配置、条件验证、兼容执行入口 |
| `source.py`、`retrieval.py`、`source_store.py`、`source_scheduler.py` | 来源接入、路由融合、独立配置、健康与调度 |
| `mcp_client.py`、`mcp_lune.py`、`mcp_plugin.py` | MCP 传输、Lune 与扩展能力 |
| `providers.py`、`llm.py`、`costing.py` | 模型配置、请求和实际使用/成本统计 |
| `execution_events.py`、`activity.py` | 实际执行事件、重连和 Activity |
| `reuse.py`、`preflight.py`、`recovery.py` | 有效证据复用、出版条件与恢复边界 |
| `research_*.py`、`report.py` | 产品中的阅读综合、证据与输出；文件名含 research 不代表研究评测 |
| `web/` | 本地产品页面与浏览器交互 |
| `tests/` | 当前工程回归与离线浏览器产品流程 |
| `scripts/verify_delivery.py` | 本地工程交付校验和独立证据目录 |

运行 Web 时默认只绑定本机地址。不要将密钥写入公开 JSON、事件或日志。模型和来源分别配置；列表展示读取本地状态，显式连接测试才发出实际请求。开发时使用临时 provider/source store 与离线替身，避免读取开发者账号或消耗配额。

## 工程验证

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python scripts/verify_delivery.py --require-browser --screenshots
```

交付脚本只发现 `tests/`，不发现 `archive/research-tests/`。它检查当前 Python 文件、Web JavaScript、三个固定 CLI Demo；浏览器检查使用离线数据，截图覆盖首页、Search、Chat、Activity、Models 和 Sources，包含 light/dark 与 1440/1024/390 px。`--require-browser` 要求浏览器流程实际执行且无跳过，`--screenshots` 额外保留响应式截图。Sources 截图等待 `/sources` 的 `#sources-list`。

校验不运行历史 Cloudflare 实现，不调用付费服务，不执行研究指标、标签工作流或冻结 proposal 哈希门禁。保留 `test_reuse.py` 中的内容/语义失效、未验证证据拒绝与模型/提示词变化等产品安全不变量。来源融合回归在 `test_source_fusion.py`。

交付脚本为子进程重定向 provider/source、OAuth 和 quota 路径，跳过默认项目 `.env`，使用合成 MCP 配置，并通过测试专用 `sitecustomize` 拒绝非 loopback Python DNS/套接字连接。Chrome 使用临时 profile、不可达代理和 loopback 白名单；Selenium Manager 禁用下载与遥测，优先使用已安装驱动。上述措施是纵深防护，不是操作系统网络沙箱，也不保证第三方可执行程序不能访问网络；真实服务请求仍必须单独授权和计量。无兼容本地 ChromeDriver 时浏览器验收应失败，不应下载或复用用户浏览器资料。

`attempts.cost_cny` 的旧 SQLite 列为 `REAL NOT NULL`。新记录以 `-1` 表示费用未知，不是负费用或零费用；旧 Lune 记录中的 `0` 也不能证明免费。该编码不修改数据库结构；直接分析数据库时须显式排除未知值，不能直接 `SUM(cost_cny)` 当作账单。对外结果以 null 表示未知金额。

每次输出到新的 `runs/verification/<UTC时间>/`，或以 `--out` 指定新的空目录。`summary.json` 记录环境版本、源码 SHA-256、检查日志、退出码与证据路径；源码在校验过程中变化会明确使此次校验失败。先完成并行实现，再由主集成执行最终工程套件。真实外部服务验证单独记录供应商响应、限制与成本，不由离线通过推断成功。

## 本轮最终交付记录（2026-10-30）

完整验收记录 [`runs/verification/agent-upgrade-final-20261030-03`](../runs/verification/agent-upgrade-final-20261030-03) 的 `summary.json` 记录 **1721 个 Python 测试、0 跳过、149 个隔离 Chrome 测试**，Python/JavaScript 检查、离线 CLI、响应式 light/dark 截图和源码漂移检查均通过。其后仅增加 `2025-11-25` MCP 握手回归并更新交付文档；这些变更另由新的验收目录记录，不将旧 manifest 当作新文件的证明。该证据只证明本地工程与离线 fixture 的正确性，不证明研究质量或完整 MCP 生命周期。

有限真实 MiniMax 产品证据为 [`minimax-product-diagnostic-20261030-02.json`](../runs/verification/agent-upgrade-final-20261030-03/minimax-product-native-diagnostic-20261030-02.json)：实际 ChatSession native proposal/apply 2请求、1合成 source、1候选、call ID匹配、controller授权通过，2439/642 tokens，USD 0.00075105 本地估算而非账单。它覆盖单一模型/profile、两步Responses和合成source，不证明广泛兼容或远端 exactly-once。

有限真实 Lune 证据为 [`lune-discover-search-20261030.json`](../runs/verification/agent-upgrade-final-20261030-02/lune-discover-search-20261030.json)：2 个逻辑请求、3 个含 OAuth 的计量 HTTP 尝试、6 个允许工具、返回 1 篇论文；本地快照从 254 减至 251，0 个未完成预留。供应商账户余额和实际账单仍未知，不能把本地余额或估算费用当作账户事实。

当前交付边界：三级 Subagent 层级仅供本 Goal 执行时的协作调度，ScholarGuide 不采用产品内主从式多智能体架构；此前误加的产品多智能体要求已由用户通过 Goal tweak 移除。MCP `stateful=True` 是只覆盖有限 POST 握手的实验接口，GET/SSE 重连、DELETE、服务端主动请求和完整恢复未实现；内置 Skills 默认关闭，领域运行时只做经审核的 DBLP 候选/样本统计且不自动执行；历史专家配对是未接入产品的合成工程实验，不是产品交付要求，也不是科研效果证明；记忆只在用户主动选择时对当前已交付结果做展示排序，不进入模型或任务提示。上述边界及未决项见 `docs/API.md`、`docs/MCP_AND_SKILLS.md` 和 `runs/verification/agent-upgrade-baseline-20261029/CONTINUATION.md`。

回退原则：目录不是 Git 仓库；先停止共享 SQLite 的旧进程，备份数据库及 WAL/SHM 和配置，再按文件级恢复 [`source-baseline.tar.gz`](../runs/verification/agent-upgrade-baseline-20261029/source-baseline.tar.gz) 中的非敏感源码。不要整体覆盖真实会话、凭据、研究资料或来源数据库；MemoryStore 新元数据和旧版不兼容时，不能删除元数据盲目降级。

## 历史文档如何使用

以下文件保持原样以保留上下文；其中已被本轮契约取代的范围、数量、一次补搜/有限反馈、来源配置、UI 控件、发布门禁和云端能力描述均为历史记录：

- `PROPOSAL_*.md`、`PROJECT_PLAN.md`、`EVALUATION_*.md`、`LABELING.md`、`LLM_PROXY.md`：历史研究计划、协议和结果，不是当前产品待办或发布前置条件。
- `TEAM_RUNBOOK_20261023.md`、`CORE_UPGRADE_*.md`、`QUALITY_UPGRADE_*.md`、`DEEP_UPGRADE_*.md`、`DELIVERY_*.md` 以及保留的旧 smoke/verification 记录：历史实施和验证背景，不宣称当前源码已经通过相同结果。已完成且不再需要的旧 UI 计划及部分工程截图、测试输出已清理。
- `SOURCES.md`、`PROVIDER_PROFILES.md`、`MCP_AND_SKILLS.md`、`AGENT_TRACE.md`、`UI.md`、`SEARCH_RECOVERY.md` 等专题说明：可作历史设计背景；新的 Sources 能力、独立凭据、事件和恢复契约以当前 API 与产品契约为准。
- `IMPLEMENTATION.md`、`DESIGN_INVARIANTS.md`、`RUNTIME_GOVERNANCE.md`、`INTENT_POLICY.md` 等技术说明：保留设计依据；被新数量范围与按操作预算取代的限制不再适用，证据与硬约束保护仍需由当前工程回归验证。
- `cloudflare-worker/README.md` 与历史 Pages 地址：独立的旧 JavaScript 云端产品，不是当前 Python 服务的部署说明。

研究评测记录及用户会话保留；部分已完成的旧工程测试产物按清理要求删除，历史文档可能仍描述当时的路径，不保证已删除工程产物可用。原 `evaluation/` 内容现位于 `archive/research/evaluation/`，查阅时按迁移位置定位。请不要把历史记录中的空白标签、实验任务或消融要求重新引入产品开发流程。
