# MCP 与 skills 接入说明（2026-10-24）

> **历史文档提示：**本页保留 2026-10-24 的旁路方案与历史证据，不能代表当前运行时代码或验收状态。当前 MCP 空 allowlist 默认拒绝，工具与 prompt 分别授权；远端错误正文默认不对外披露，schema 中 `$ref`、正则、`uniqueItems: true` 及部分重复验证关键字暂不兼容；远端发现的 schema 在限时独立进程中验证，但该进程不是安全沙箱，启动和清理可能超时；MCP 传输拒绝重定向。Lune 共享本地配额与来源配置见 `docs/API.md`。完整 MCP 生命周期和 Skills 仍在开发，不能依据本页声明兼容或交付完成。

这份文档写给两类人：想把**另一个 MCP server** 接进来的开发者，和想加**自定义 skill** 的队友。
照做不需要读 `source.py`，但需要知道本轮的硬边界：33 个运行时文件处于冻结状态。

---

## 当前治理边界（进行中）

- 工具和 prompt 都需要宿主独立 allowlist 以及本地审阅的输入 schema；远端发现的 schema 只能进一步收紧，不能单独扩大参数权限。非 Lune `ServerConfig` 应显式提供 `local_tool_schemas` 或 `local_prompt_schemas`。旧的“只发现远端 schema 即执行”方式不再可用。
- `list_tools(paginate=True)` / `list_prompts(paginate=True)` 是宿主显式选择的有界分页。默认不自动请求后续页；发现响应含 `nextCursor` 时，默认路径报错，不把首页当成完整发现。每页 Lune HTTP 和必要的认证尝试均由共享本地额度预留，不能推断账户实际余额。
- 刷新开始后旧发现快照不再授权调用，失败、取消、过期或被较新刷新取代时保持关闭；仅完整成功的发现按代数原子发布。原始单页发现与列表入口同样受大小和代数校验。分页过程中认证身份或目标变化会拒绝发送后续 cursor。请求已越过本地最终检查的在途操作不能撤销。
- 缺省发现过程使用一个 30 秒本地 `Budget`；调用方可显式传递更短预算或取消令牌。底层已经进入的系统调用和子进程启动/清理不具有严格硬墙钟保证。
- 默认仍为原有无状态 POST 路径；`McpClient(..., stateful=True)` 是仅宿主显式启用的有限实验接口。它在首个业务 POST 前串行执行 `initialize` 与 `notifications/initialized`，只接受本地支持的 `2025-03-26` / `2025-11-25` 协商版本，携带协商版本和会话 ID，检查认证身份绑定；Lune 每次初始化、通知和业务请求分别保守预留。对应验证目前仅为离线模拟，不代表已在真实 Lune 执行。
- 有状态模式允许一个有界 POST 响应中的进度通知，但不执行服务端主动请求；若同流已有可识别的工具响应，错误上保留经脱敏的收到结果，不能将其当成完成的推荐。任一会话失败后当前实例不自动刷新认证、重握手或重放；HTTP 404 后需要宿主检查此前操作结果并显式创建新客户端。这是保守策略，与规范要求的会话恢复行为并不等同。
- 独立 GET/SSE 流与重连、服务端请求响应、HTTP DELETE、全部规范兼容、实际 Lune 会话握手和分页仍未交付。`session/delete` 仅是配额方法名，不表示已发送 DELETE。服务器不分配会话 ID 的情形在实验模式拒绝；会话 ID 是敏感值，不写入事件。配置超时约束单次 HTTP，而不保证整个握手加业务调用的总时长；SQLite 等在途系统调用无法立即中断。
- 本地配额的预留和结算支持预算/取消检查：取消或结算异常不退款、不重发，已收到结果与账本状态应分别检查。真实账户额度和第三方客户端消耗仍不可由本地账本证明。

---

## 1. 为什么是「旁路注册」而不是改代码

`core_lock.json` 把 33 个运行时文件锁定在 `core_id = 692375d0…`（2026-10-27 因
provider 兼容性修复重新 lock；上一版是 `2bae49f4…`，`runs/mcp-20261024/` 那批证据
对应旧 id，旧批次各自仍记录自己运行时的 id，新旧批次不可混用）：

```
common.py prompts.py controller.py session.py workflow.py reuse.py state.py
state_schema.sql source.py retrieval.py research_*.py agent_runtime.py agent_tools.py
tools.py llm.py model_identity.py model_policy.py network.py costing.py diagnosis.py
recovery.py intent.py preflight.py providers.py report.py activity.py demo.py app.py webapp.py
```

新增一个检索来源不能改这些文件，于是只能在**运行时注册**。检索路径实际只有两处依赖：

| 依赖点 | 位置 | 作用 |
|---|---|---|
| `PROVIDERS` 字典 | `source.py` | `search_batch` 用它判断 provider 是否存在（`p in PROVIDERS`）；`agent_tools` 遍历它列工具 |
| `AcademicSource._search_one` | `source.py` | provider → 实现的分发链；未命中直接 `raise GuideError('Unknown source')` |

`mcp_plugin.register(name, spec, handler)` 做前者，`mcp_plugin.activate()` 做后者（包一层，
未注册的 provider 原样落回原方法）。**改动恰好两个符号**：

```
source.PROVIDERS['<name>']
source.AcademicSource._search_one
```

`tests/test_mcp_plugin.py` 的第一条断言就把这份清单钉死；`runs/mcp-20261024/spike-report.json`
是当时的证据（`mutations` 字段、`core_id` 未变、9 个旧批次仍 `same`）。

### 加载顺序

必须先 `import mcp_plugin`（或 `mcp_lune.install()`）再发第一次检索。没导入时的表现是
「provider 不存在」，不会出现半注册状态。`activate()` 幂等。

### 入口侧（CLI / Web）

`--source` 会被 `common.SOURCE_NAMES` 校验，而这个 tuple 被 `app.py` / `webapp.py` /
`controller.py` / `source.py` **按值 import**，无法从外部扩展。`app_mcp.py` 启动器在导入后
重绑这几个模块的 `SOURCE_NAMES`：

```bash
C:/Python314/python.exe app_mcp.py web --port 8765
```

评估/批次路径不需要启动器：`evaluation.batch` 用 `providers=[...]` 显式传参，不经过 `SOURCE_NAMES`。

---

## 2. MCP 客户端（`mcp_client.py`）

标准库实现，零第三方依赖；传输走仓库既有的 `network.urlopen`（进程边界 + 绝对超时 + 禁代理）。

- 一个 JSON-RPC 消息一个 `POST`；`Accept: application/json, text/event-stream`
- 同时接受纯 JSON 与单条 SSE（`event: message` + `data:` 行）
- 默认无会话状态：不发 `GET`/`DELETE`；显式实验性 `stateful=True` 的限制见上面的当前治理边界
- 方法：`tools/list`、`tools/call`、`prompts/list`、`prompts/get`

### 错误分类（分支只看 `kind`，不看文案）

| `kind` | 触发 |
|---|---|
| `auth` | HTTP 401/407 |
| `forbidden` | HTTP 403 |
| `rate_limited` | HTTP 429 |
| `client_error` | 其余 4xx |
| `server_error` | 5xx |
| `timeout` | 传输层超时 |
| `transport` | 连接失败（如拒绝连接） |
| `protocol` | 非 JSON / SSE 无 data 行 / 无 `result` 对象 / JSON-RPC `error` / `isError: true` |

每个 `McpError` 带 `status`、`code`、`server` 和安全的 `raw` 标记；远端错误正文不会对外披露。

### 配置文件 `mcp_servers.json`

```json
{"servers": {"lune": {
  "url": "https://mcp.luneresearch.com",
  "headers": {"Authorization": "Bearer ${LUNE_API_KEY}"},
  "timeout": 60,
  "protocol": "2025-11-25",
  "allow": ["search_papers", "search_papers_many", "search_related_papers",
            "get_paper_citations", "list_conferences"]
}}}
```

- `${VAR}` 从环境展开；没设就抛 `auth`，**不会**把值打进日志
- `allow` 是白名单：不在表里的工具在**发请求前**就被拒（`client_error`），避免误花额度
- `on_missing='drop'` 时展开失败的 header 会被丢弃（用于「凭据由别处提供」的场景）

### 凭据的两种方式

1. **Access key**：`.env` 里 `LUNE_API_KEY=lune_…`（或环境变量）。注意 `.env` 已在仓库里，别提交。
2. **OAuth（推荐）**：`scripts/lune_oauth.py` 走授权码 + PKCE，浏览器点一次 Authorize，
   token 落到 `~/.scholarguide/lune-oauth.json`（**仓库外**，含 `refresh_token`，access token 有效期 1 小时；仓库里只有脱敏的 `runs/mcp-20261024/oauth-record.json`）。脚本会 `chmod 0600`，但 **Windows 上 chmod 只切只读位**，真正的保护来自「文件在用户主目录、不在仓库里、不进任何产物」；要更严就用 NTFS ACL 收紧该目录。
   `mcp_lune.access_token()` 在临近过期（<120s）时自动用 refresh token 换新——Lune 会轮换 refresh token，
   新值会写回同一文件。

> 踩过的坑：`api.luneresearch.com` 前面有 Cloudflare，默认 `Python-urllib` 的 UA 会被判
> `Error 1010 browser_signature_banned`。OAuth 脚本与 token 刷新都带浏览器 UA；
> **不要**为了省事把 UA 写成一个明显的机器人字符串。

---

## 3. Lune 适配（`mcp_lune.py`）

### 真实字段（2026-10-24 用 OAuth 探测确认，`lune-probe.json`）

`search_papers` 返回 `{results: [...], has_more, best_score, low_confidence}`，单条记录：

```
paper_id, title, authors[], year, doi, abstract, citation_count, conference, score, rerank_score, contexts[]
```

适配器也接受更早猜的名称（`id`/`name`/`summary`/`venue`/`published_year` 等），
外层键接受 `papers|results|items|data|hits|documents` 以及 `results.papers`。

最终 `Paper.id = "lune:<paper_id>"`，所以每条命中都能追回 Lune 的原始记录。

### 一个必须知道的坑：身份字段

`source.search_batch` 合并时执行：

```python
papers = merge_papers([p for p in collected if p.title and (p.url or p.doi or p.arxiv_id)])
```

只有标题、没有 `url`/`doi`/`arxiv_id` 的记录会被**静默丢弃**。Lune 的 `url` 常为空，但 `doi` 通常有，
所以正常情况不丢；账本里记 `no_identity` 计数，`returned` 与最终命中数对不上时先看这个数。

### 预算与账本（历史记录，非当前共享配额）

以下是 2026-10-24 的旧版 JSON 账本说明，不是实时余额，也不能保障当前跨进程共享上限；新的 SQLite 快照、认证尝试与保守计量以 `docs/API.md` 为准。账户剩余额度或重置规则不明时不得发请求。

- 历史版本的 `--max-lune-requests N`（当时默认 120）：**发请求前**检查，超了就抛
  `lune_request_budget_exhausted`，不会先花再报错
- 账本 `runs/mcp-20261024/lune-ledger.json`：`requests_used` / `max_requests` / `by_tool`，
  每条 entry 含 `at`、`tool`、`query`、`limit`、`ok`、`returned`、`visible_items`、`skipped`、
  `no_identity`，失败时还有 `error`(kind) 与 `status`
- 失败的调用**也计入额度**（Lune 侧可能已经计费），账本照记 `ok:false`

### 错误翻译

`classify_source_error` 是冻结的、按文案匹配，所以适配器把 kind/status 写进错误文本：

| MCP | 文本前缀 | 归类 |
|---|---|---|
| `rate_limited` 429 | `lune rate_limited 429 …` | `rate_limited` |
| `auth` 401 / `forbidden` 403 | `lune auth 401 …` | `auth` |
| `timeout` | `lune timeout …` | `timeout` |
| `protocol`（含 JSON 失败） | `lune protocol … json …` | `invalid_response` |
| 其余 | — | `source_error` |

### 历史批次的操作说明

本节原有 120 篇批处理和旧探测命令是历史方案，不应直接重跑：它们可能覆盖历史证据或消耗共享额度。认证页面本身的浏览器请求不受本地账本完全控制；重新认证后应先核实账户级余额。手动授权模式会显示一次性敏感 URL，不应将其复制到日志或 AI 对话；`--open` 模式不打印该 URL。需要新的有限探测时，先由用户确认共享账户余额、重置时间及其他客户端不并发使用，建立有效本地快照，再使用 `scripts/probe_lune_governance.py` 并指定全新证据路径。

`mcp_run_batch.py` 先 `install()` 再调用**未修改**的 `evaluation.batch.run_batch`，
所以 manifest、`core_id`、`batch verify` 与既有批次同构；额外字段 `mcp.lune` 记录请求数与配额余量。

---

## 4. skills（`mcp_skills.py`）

当前额外有默认关闭的 `evidence_reading` 和 `research_trends` 领域计划，以及仅供可信宿主显式启用的 `domain_skill_runtime.py`。现有协调器只接受单一 DBLP 来源，返回候选和引用的精确匹配或样本年份统计，始终标为 `partial`；它不执行第三方脚本、不判定语义支持，也尚未接入产品入口。以下是旧 MCP prompt 注册接口，不能把远端 prompt 或 Skill 文本当成授权。

一条 skill = **名称 + 说明 + 参数 + 步骤**，是声明而不是代码。

```python
Skill(name='literature_review', description='…',
      params=(Param('topic', required=True), Param('venues'), Param('since_year')),
      steps=({'kind': 'prompt', 'server': 'lune', 'prompt': 'literature_review'},),
      source='lune', server='lune')
```

- **来源一**：`mcp_skills_lune.json` 内置 Lune 的 6 个 prompt（`literature_review`、
  `find_related_work`、`compare_papers`、`verify_draft`、`trace_citations`、`research_methodology`），
  离线可列、可校验
- **来源二**：`prompts/list` 刷新——服务器多出来的 prompt 会以 `source='mcp-prompt'` 注册；
  已在 bundle 里的不覆盖（保留人工整理的说明与参数）
- **来源三**：任何同格式 JSON 文件，`SkillRegistry.register()` 挂进来

CLI：

```bash
python -m mcp_skills list                 # 6 条 + 来源
python -m mcp_skills show compare_papers  # 参数与步骤
python -m mcp_skills check                # 校验每条都有参数与步骤
python -m mcp_skills invoke verify_draft --arg draft="…"        # 只出计划，不联网
python -m mcp_skills invoke verify_draft --arg draft="…" --execute   # 真调 MCP
```

参数校验：缺必填 → 报出缺哪些；多传 → 列出允许的名字；未支持的步骤类型（如 `shell`）直接拒绝。

### 加一个自定义 skill

1. 写进任意 `*.json`：`{"server": "lune", "prompts": [{"name": "my_flow", "description": "…",
   "arguments": [{"name": "topic", "required": true}], "steps": [{"kind": "tool",
   "tool": "search_papers", "arguments": {"query": "$topic", "limit": 10}}]}]}`
2. `python -m mcp_skills --help` 对应的 `invoke --file <你的.json> my_flow --arg topic=…`
3. 步骤里的 `$topic` 会被调用参数替换；`kind` 只支持 `tool` 与 `prompt`

---

## 5. 接第二个 MCP server 的清单

1. 在 `mcp_servers.json` 加一节：`url`（Streamable HTTP）、`headers`、`timeout`、`allow`、`protocol`
2. 若工具返回的不是论文：写一个 `xxx_adapter.py`，把结果映射成 `Paper`（至少 `title` +
   `url|doi|arxiv_id`，否则会被合并过滤器丢掉），并像 `mcp_lune.py` 那样提供
   `handler(query, limit, filters) -> list[Paper]`
3. `mcp_plugin.register('<name>', ProviderSpec('<name>', '<Label>', 'scholarly'), handler)` + `activate()`
4. 加单测：注册、映射、预算、错误分类各一条（照抄 `tests/test_mcp_lune.py` 的结构）
5. 需要出现在 Web 下拉框时：**先确认 `retrieval.py` 的 `preference` 表**（见下）——否则做了也是假 affordance

---

## 6. 已知限制（本轮实测，别当成待办）

1. **`retrieval.py` 的偏好表是硬编码六源**。`research_mode=True` 且没有显式 `providers` 提示时，
   `ordered = [n for n in preference if n in allowed]` 会把任何新来源丢掉，任务直接以
   「No configured and available source can execute this search plan」结束。
   所以 Web 下拉框**有意不提供** `lune`；它只走 ① 批次（`mcp_run_batch.py`）② agent 显式
   hint `providers=['lune']`（hinted 分支会保留）。
2. **activity trace 的 per-source chip**（`Lune · MCP search_papers · ok · 812ms`）只在 chat session
   的事件流里有 `router.batches[].runs[]`；`/search` 任务没有。渲染代码已接好，但没有一张
   真实厂商 trace 截图。
3. `search_papers` 的 `limit` 语义未逐一核对（探测时 `limit=3` 返回 3 条），批量按 20 请求，
   ≥20 条时以 `has_more` 为准。
4. 配额按团队计：Pro 300/天。跑 120 例约花 120 请求；重跑前先看账本 `requests_used`。
