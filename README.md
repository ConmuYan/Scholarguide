# ScholarGuide

ScholarGuide 是本地运行的论文发现与阅读助手：输入主题、数量和出版条件，查看带来源与摘要证据的推荐，并在 Chat 中继续修改需求。当前交付对象是 **Python API 与本地 Web 产品**。

- 推荐数量支持 **1–10 篇，默认 5 篇**；可表达数量区间。超过 10 篇会明确提示上限，候选不足时显示实际数量和缺口。
- Search 和 Chat 使用共享的会话执行路径；Chat 支持连续反馈、切换主题、取消和恢复。
- Sources 管理检索源配置与可用状态；Models 管理模型连接。来源凭据与模型凭据分别配置。
- Activity 展示实际执行事件、来源、耗时与错误；部分结果、来源限制和未知成本会明确显示。

## 一键安装（GitHub Release）

公开 GitHub 仓库尚未创建。仓库建立并发布版本后，macOS/Linux 用户可使用：

```bash
curl -fsSL https://github.com/ConmuYan/Scholarguide/releases/download/vX.Y.Z/install.sh | sh
```

Windows PowerShell：

```powershell
irm https://github.com/ConmuYan/Scholarguide/releases/download/vX.Y.Z/install.ps1 | iex
```

安装器会校验 Release 源码包 SHA-256，在用户目录创建独立 Python 环境，不要求管理员权限。首次打开本地网页后，到 Models 输入你自己的 API key。密钥保存在本机；实时搜索可能消耗你配置的模型与来源配额。安装细节和安全边界见 [docs/INSTALL.md](docs/INSTALL.md)。

## 开发者从源码运行

需要 Python 3.11 或更新版本。从项目根目录执行：


```bash
python -m pip install -r requirements.txt
```

复制配置模板：

```powershell
# Windows PowerShell
Copy-Item .env.example .env
```

```bash
# macOS / Linux
cp .env.example .env
```

在 `.env` 中填写自己的模型设置，例如：

```dotenv
SG_PROVIDER=openai
SG_BASE_URL=https://api.deepseek.com
SG_MODEL=deepseek-flash
SG_API_KEY=YOUR-KEY
SG_AGENT_RUNTIME=langgraph
```

也可在网页 Models 中配置其他支持结构化 JSON 的兼容服务。`@pi:provider` 是已配置 pi 的机器才可使用的可选密钥引用；普通安装不需要 pi、GPU 或模型训练。

```bash
python app.py web --open
```

Windows 下可无控制台后台运行，关闭启动终端后服务仍会继续：

```bash
python app.py web --background --open
```

后台日志保存在 `runs/service-8765.log`，进程信息在 `runs/service-8765.json`。Windows 的网络请求子进程使用同一 Python 安装中的 `pythonw.exe`，避免检索和模型调用时反复弹出控制台。

打开 `http://127.0.0.1:8765/`。服务默认仅监听本机回环地址。

| 页面 | 用途 |
| --- | --- |
| `/search` | 输入自然语言需求、查看解析后的数量与条件、检索并阅读结果 |
| `/chat` | 连续反馈、修改条件、恢复已保存会话 |
| `/sources` | 管理来源、优先级、能力与健康状态，按需测试连接 |
| `/settings` | 管理模型 profiles、连接测试和成本单价 |
| `/activity` | 查看运行状态、实际工具事件和错误详情 |

首次使用可先选择离线 Demo。Demo 使用固定样例，不访问模型或外部搜索；切换到 Live 后才执行真实检索与推理，可能消耗相应账号配额。打开 Sources 列表不会自动执行付费连接测试。

## Search 与 Chat

在 Search 中输入完整需求，例如：

> 推荐 8 篇关于 retrieval augmented generation 的论文，发表于 2024–2026 年，比较主要方法。

也可输入“推荐 3–6 篇”这样的区间。不指定数量时默认 5 篇。显式类别配额、年份、venue 和出版状态须一起满足；请先检查解析后的任务条件。来源不支持的筛选或无法核验的出版信息会反馈为限制或缺口。

从搜索结果进入 Chat 后，可以继续说“去掉第三篇”“增加一篇综述”或切换主题。每次操作有自己的执行预算；取消、失败与重启后的恢复保留已有结果和实际使用记录。自动复用仅使用仍然有效的证据与判断。

推荐标题、标识符和链接来自来源元数据。摘要原文、检索片段与模型归纳分别呈现；摘要引文匹配不代表全文核查，模型建议仍应结合原论文判断。无法满足请求时系统返回可用结果并解释原因。

## 模型与检索来源

在 Models 中配置 base URL、model、API key 和可选单价，再按需测试连接。Profiles 默认保存到服务端 `~/.scholarguide/providers.json`，可通过 `SG_PROVIDER_STORE` 修改路径。完整的已保存 key 不返回浏览器，缺失 usage 或单价时成本显示 unknown。

在 Sources 中查看当前服务提供的来源与能力。支持的集成包括 arXiv、OpenAlex、Semantic Scholar、DBLP、OpenReview、ACL Anthology、Lune 和 DeepSeek；实际可用性取决于配置、授权与供应商限制，以该页返回的信息为准。来源状态默认保存于 `~/.scholarguide/`，可用 `SG_SOURCE_STORE` 指定存储文件。

arXiv 官方 API **没有 API key，也没有个人付费额度**；官方限制是同一出口 IP 每 3 秒最多一次、单连接。需要更高请求率只能通过 arXiv support 咨询，且不保证批准；大批量元数据/全文应使用官方 Kaggle 数据集、S3 requester-pays 或 OAI-PMH，而不是持续请求实时 API。本项目默认走实时 API，因此 arXiv 不是“无限量”来源；日常使用请保留 `multi` 和缓存，让 Lune / OpenAlex / Semantic Scholar 互为补位。

来源 HTTP 默认直连；如果本机需要学术代理，可在 `.env` 设置 `SG_SOURCE_PROXY=http://127.0.0.1:7890`，它只影响来源请求，不影响模型调用。arXiv 返回 429 / `Rate exceeded` 时会暂停 15 分钟，不再立即重试；这是 arXiv 的出口 IP 限流，不是密钥或配置错误。

`multi` 按启用状态、优先级与任务条件选择来源；显式单源请求保留其来源范围。Lune 与 DeepSeek 的来源授权独立于当前推理模型；更换模型不会提供另一来源的访问权限。Semantic Scholar 可匿名访问，但建议在 Sources 中配置 `S2_API_KEY` 以降低 429；OpenAlex 同样支持可选 key。DeepSeek 只使用 `DEEPSEEK_SEARCH_KEY`，不会回退到模型 key。连接测试会发出真实的有界请求，可能计费或扣除来源配额；429、冷却、认证失败和未知配额均按实际状态显示。DBLP 被反机器人页拦截后会进入 6 小时冷却；显式“测试连接”会清除冷却并发起一次真实探测。

## 命令行与 API

离线演示：

```bash
python app.py demo --scenario followup
```

其他固定场景为 `satisfied` 和 `insufficient`。Live 命令行示例：

```bash
python app.py doctor
python app.py run --source multi --request "推荐 8 篇关于视觉检索的论文，比较主要方法。"
python app.py chat --source multi --request "推荐 3 篇关于 RAG 的近期论文。"
```

CLI 支持 `--request-file examples/request.json`。结构化请求的 `target_count` 支持 1–10；`minimum_count` / `maximum_count` 表达区间。实际缺少论文时保留部分结果。CLI 输出包含 `result.json` 与 `report.md`；`--out` 指向新的输出目录以避免覆盖已有记录。

版本化 API 的主入口为 `/api/v1/search`、`/api/v1/chat` 和 `/api/v1/operations/{id}`。异步响应提供操作 ID、会话 ID 和查询、事件、取消、结果链接；客户端可使用 `client_request_id` 或 `Idempotency-Key` 避免重试时重复提交。字段、错误结构、SSE 恢复和可运行示例见 **[API 文档](docs/API.md)**。旧的 jobs 与 chat sessions 路由保留作兼容入口。

## 开发与验证

当前文档索引、模块边界与离线工程校验见 **[开发指南](docs/DEVELOPMENT.md)**。基础回归：

```bash
python -m unittest discover -s tests -v
```

完整本地交付校验：

```bash
python -m pip install -r requirements-dev.txt
python scripts/verify_delivery.py --require-browser --screenshots
```

交付校验运行工程测试、Python/JavaScript 语法检查、三个 CLI Demo 和真实浏览器的离线产品流程；截图包含 Sources 等六个页面的明暗主题与不同宽度。需要 Node、Chrome 和可用的 WebDriver。每次写入新的 `runs/verification/<UTC时间>/`，保留日志、源码哈希和 `summary.json`；此命令不调用付费模型，也不执行部署。测试结果以当次实际日志为准。

## 历史资料与 Cloudflare

`cloudflare-worker/` 与 `cloudflare-pages/` 保留的是历史云端实现，其 JavaScript 后端的来源、数量限制和 Chat 能力与当前 Python 产品不同。历史 Pages/Workers 地址不代表本轮已经部署，也不能用于验收当前本地 API 或 Web 功能。历史部署说明仍保存在 [Worker README](cloudflare-worker/README.md)。

研究代码、历史结果和相关测试的存放位置见 [归档说明](archive/README.md)。研究记录与用户会话保留；部分过期工程截图、旧测试输出和已完成的 UI 文档已清理。历史结论不作为当前发布条件。上游来源与复用说明见 [UPSTREAM.md](UPSTREAM.md)。
