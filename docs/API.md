# ScholarGuide API

当前版本：`v1`。本地服务默认 `http://127.0.0.1:8765`。Web 与 API 使用同一业务服务；在线 Search 和 Chat 均通过 `ChatSession` / LangGraph 执行。API 无需数据集、研究评分器或人工标注。

机器可读契约：`GET /openapi.json`（同 `/api/v1/openapi.json`）。网页地址为 `/search`、`/chat`、`/sources`、`/settings`、`/activity`，它们不是 POST API。

## 本地偏好

初版偏好管理 API 已可用；设置页暂**不展示**该面板（UI 延后，功能与数据保留，`/memory.js` 仍加载以便测试），因此日常界面看不到本地偏好入口。偏好默认不进入新会话、任务或模型；只有用户在当前 Chat 结果中主动点击“按近期偏好查看”时，`prefer_recent` 才可能对**已提交、已展示的当前轮次**做一次本地显示排序。该排序不改变检索、任务硬条件、模型输入、checkpoint、activity 或原始历史结果；撤回不能撤销已看到的内容。`reading_goal` 目前仅可管理，未接入任务或模型。

- `GET /api/v1/memory/preferences` 返回 `{"api_version":"v1","preferences":{},"active":{},"store_id":"…"}`。`preferences` 含每个已记录键的 `revision`、`state`、`value`、`consent`、`expires_at`；`active` 仅含有效值。GET 会把观察到的过期项持久标记为 `expired`；库不存在时会安全创建**不含偏好值和授权的空库**，用于建立稳定身份。读取本身不等于同意使用偏好。
- `POST /api/v1/memory/preferences/{key}` 的 JSON 必须恰含 `value`、`consent`、`expires_at`、`expected_revision`、`store_id`。`store_id` 必须来自最新 GET；`consent` 必须是 `true`，过期时间必须是未来的有限 Unix 秒数。`reading_goal` 的值为 `orientation`、`comparison`、`frontier`、`general` 之一；`prefer_recent` 为布尔值。
- `POST /api/v1/memory/preferences/{key}/revoke` 和 `/delete` 的 JSON 必须恰含 `expected_revision` 和 `store_id`。成功返回 `{"api_version":"v1","revision":1}`，再 GET 可查看状态；删除和撤回均清除当前本地值并保留版本墓碑。

每键初始版本为 0，每次变更递增。库身份或版本不符返回 409；旧客户端缺少 `store_id` 也返回 409，须更新后重新读取并由用户重新决定。本地故障或提交结果不明返回固定安全错误，`retryable: false`；不得自动重试。`store_id` 是防止意外写入其他数据库的版本标识，**不是认证凭据，也不能防止数据库被恶意篡改**。默认独立数据库为 `~/.scholarguide/memory.sqlite3`，可用 `SG_MEMORY_STORE` 指定。合法旧偏好库首次打开时在事务中增加身份元数据，旧值不变；未知 schema 会拒绝访问，不清理或覆盖原文件。降级到不识别新元数据的旧版本前，应先停止所有访问该库的进程并备份数据库及其可能的 WAL/SHM；旧版可能拒绝新 schema，不能通过删除元数据或覆盖库来盲目回退。

### Chat 展示偏好

- `POST /api/chat/sessions/{session_id}/turns/{turn}/presentation` 只接受 `{"selected":true}`，由可信宿主从该 session/turn 已持久化且已交付的论文读取输入；它不接受客户端论文、路径、用户身份或模型参数。
- 成功只返回有界的 `paper_ids` 顺序，状态为 `applied`；撤回、过期、库身份/修订变化、来源状态异常、旧任务缺少显式用户字段或结果文件不一致均返回 `not_applied`，不自动重试。响应 `Cache-Control: no-store`。
- 该接口是展示层选择，不是记忆注入、推荐重排事实或语义证据；偏好验证与响应发送之间仍存在不可原子窗口。

服务的 Host/Origin 检查是本机浏览器访问限制，**不是用户认证**；外部页面与本机其他进程不应被视为已认证用户。本地删除不承诺清除旧 sessions、SQLite WAL、备份或远端数据；撤回也不能取消已经发出的请求。

`DELETE /api/chat/sessions/{id}` 与 `DELETE /api/jobs/{id}` 当前统一返回 503 `deletion_unavailable`，不删除数据。原因是现有目录内 execution lease 不足以在跨进程执行、恢复和旧对象引用之间安全实现递归删除；必须先补齐稳定目录外锁、删除墓碑与安全路径操作。Provider 自定义配置的 DELETE 不受此限制；环境配置不可通过该接口删除。

## 创建 Search

```bash
curl http://127.0.0.1:8765/api/v1/search \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: my-search-001' \
  -d '{"query":"推荐 5 篇 RAG 论文，帮助设计评测实验。","source":"lune"}'
```

返回 HTTP 202：

```json
{
  "api_version": "v1",
  "operation_id": "…",
  "session_id": "…",
  "turn": 1,
  "state": "running",
  "links": {
    "self": "/api/v1/operations/…",
    "events": "/api/v1/operations/…/events",
    "cancel": "/api/v1/operations/…/cancel",
    "result": "/api/v1/operations/…/result"
  },
  "result": null,
  "error": null
}
```

`session_id` 可能在首个回执时尚未建立，后续快照与结果会提供。`state=completed` 表示执行结束；实际是否满足需求，看 `result.status`、`gaps` 和 `stop_reason`。不足、来源失败或用户停止都不会被伪装成完整推荐。

输入支持：

| 字段 | 说明 |
|---|---|
| `query` / `message` / `request` | 自然语言需求，1–2000 字符，选一种 |
| `count` | 可选显式数量，1–10；未指定从需求解析，无数量时默认 5 |
| `source` | 来源名称、逗号来源列表或 `multi`；`multi` 按来源配置与能力规划 |
| `providers` | 可选的来源名称数组，形成显式 allow-list |
| `filters` | 明确筛选条件，覆盖对应解析字段；冲突会拒绝或报告缺口 |
| `request_data` | 可选结构化创建任务，包含 `topic` 及筛选字段；绕过意图解析 |
| `client_request_id` | 可选幂等键，与 `Idempotency-Key` 二选一即可 |
| `mode` | `live`（默认）或固定离线 `demo` |
| `cache` | 是否使用来源缓存，默认 true |
| `research_mode` | 是否执行研究路径，默认 true |

同一幂等键与同一请求返回原 operation；同键不同请求返回 409。断开浏览器或 SSE 不会重新提交任务，也不等于取消。若提交结果在进程崩溃窗口中无法确认，API 返回明确的不确定状态，不自动再次调用外部服务。

数量可在文本中表达为“3–7 篇”，结构化区间使用 `minimum_count` / `maximum_count`。明确超过 10 篇返回数量提示。不会因为缺少匹配结果而编造论文。

筛选字段包括 `category`（none/evaluation/survey/research）、`minimum_category_count`、`venue`、`year_from`、`year_to`、`purpose`、`reading_goal`、`publication_type`、`venue_scope`、`accepted_only`、`presentation_types`、`prefer_recent` 和 `excluded_paper_ids`。类别最低篇数必须兼容总数；会议、年份与录用条件须由同一可验证出版记录满足。某来源不提供相关证据时，系统明确说明缺口。

## Chat 与 Search 接续

```bash
curl http://127.0.0.1:8765/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"SEARCH返回的session_id","message":"排除第三篇，保留研究主题，改成 4 篇。","client_request_id":"my-followup-001"}'
```

不传 `session_id` 时创建会话；传入时继续该工作区。每条消息有独立 operation 和本轮预算，已有论文、有效判断和累计用量保留。继续消息中的 `request_data` 不被静默接受，应使用 `message` 和 `filters`。源选择如果明确传入，则用于后续执行；未传时保留会话来源。

## 状态、事件与取消

```bash
curl http://127.0.0.1:8765/api/v1/operations/OPERATION_ID
curl -N http://127.0.0.1:8765/api/v1/operations/OPERATION_ID/events \
  -H 'Accept: text/event-stream'
curl http://127.0.0.1:8765/api/v1/operations/OPERATION_ID/events?after=12
curl -X POST http://127.0.0.1:8765/api/v1/operations/OPERATION_ID/cancel \
  -H 'Content-Type: application/json' -d '{}'
```

SSE 的 `trace` 事件携带稳定的 `seq/event_id`、operation、session、turn、时间、action 和 payload；用 `Last-Event-ID` 或 `after` 续接。`tool_start/tool_end/tool_error` 是实际调用的生命周期，含来源、工具、span、耗时和可公开的结果摘要。`done` 表示该操作终态已可读取；心跳仅说明连接存活。无需解析私有模型推理。

显式停止会阻止新的工作，尽可能终止本地在途 HTTP，保留已核验结果；远端请求可能已经产生用量。对完成状态未知的历史外部调用，不宣称 exactly-once，也不会自动重放。

## 推荐 JSON

结果中的每篇论文包括 `rank`、`paper_id`、标题、链接、作者、年份、DOI、来源、摘要和出版元数据，以及推荐理由与证据。`returned_count` 与 `papers` 数组长度一致，不截断为五篇。`candidates` 是单独的发现候选（最多 20 项），可能尚未经过摘要、语义或出版条件核验，不计入推荐数量；中断后可保留已取得的部分候选。`usage` 和 `cost` 是可观测记录，缺失值保留 unknown/null。工具费用以 `cost.tools_currency` 标识（当前来源估算为 CNY）；Lune 实际费用未知时，`cost.tools` 和 `cost.total` 为 null，即使模型费用已知也不会显示完整总额。非零 CNY 工具费用不会未经汇率转换直接加到其他币种的模型费用；估算不是供应商账单。

`evidence.quote_verified=true` 只说明引文存在于对应摘要；不证明语义蕴含或全文实验成立。模型归纳与待验证假设在 `research_brief` 中分别标记。

错误为 JSON：

```json
{"api_version":"v1","error":{"code":"invalid_request","message":"…","field":"count","retryable":false}}
```

参数错误为 400，找不到资源为 404，忙碌会话或幂等冲突为 409。来源的 429、认证、超时、空响应和部分失败在来源健康、事件及结果诊断中保留。错误可能额外提供受限的 `remote_outcome`：`not_started`（该受控尝试未派发）、`unknown`（结果不明）、`response_received`（已收到响应，但本地未能交付）、`error`（远端明确报告错误）或 `completed`（远端完成但本地后续处理受阻）。也可能提供 `transaction_outcome` 和 `retry_safe: false`；有这些分类时 `retryable` 为 false，客户端不能盲目重新派发。`not_started` 不证明同一操作中其他页或认证请求未发生；远端明确报错也不保证没有副作用。


## 模型供应商（本地 Web）

- `GET /api/providers` / `POST /api/providers`：读取与保存模型档案；密钥只写不读，响应仅含是否已存储。
- `GET /api/providers/oauth/openai`：读取 ChatGPT 登录状态 `{"state":"idle|running|succeeded|failed","message":"…","profile":{…}|null,"has_profile":bool,"running":bool}`。不返回令牌、验证码或回调地址。
- `POST /api/providers/oauth/openai/login`：`{"model":"<该账户可用的模型>","use_system_proxy":false}`，在本机浏览器拉起 OpenAI 授权；`202` 表示已开始，`409` 表示已有登录在进行，`400` 表示参数无效或保留档案冲突。授权完成后自动登记 `openai-oauth` 档案但**不切换**当前模型。成功后浏览器轮询上述 GET 获取终态。

- `POST /api/providers/oauth/openai/test`：接受 `{}`（测试已保存模型）或 `{"model":"<slug>"}`（仅测试，不保存）。从官方 `/v1/models` 返回 `visibility=list` 的模型目录，并向所选模型发送小型 JSON 请求；输出 `catalog` 和 `probe` 的独立状态、HTTP 状态、响应耗时及本次测试的输入／输出 token。只有点击测试才发请求；并发测试返回 `409`。失败时用量可能为 `null`，不代表免费。账户套餐总用量不由此接口查询，`account_usage.available=false`，应到 [ChatGPT 用量设置](https://chatgpt.com/settings/usage) 查看。

这些端点与其余本地 UI 一样只接受 `127.0.0.1`/`localhost` 且校验 Origin；`use_system_proxy` 仅影响 OpenAI OAuth 认证与模型请求，取值为无凭据的本机 HTTP 回环代理，不改变论文来源的代理设置。

### 结构化停止条件（`blocking_requirements`）

规划器与任务更新器不再用自由文本申报“不支持”。二者的 JSON 契约里是同一个必填数组 `blocking_requirements`，元素为 `{kind, quote}`：`kind` 取自封闭枚举 `code_release`、`dataset`、`award`、`author_or_affiliation`、`citation_threshold`、`length_or_pages`、`language`、`venue_subset`、`year_subset`、`publication_type`、`presentation_type`、`version_or_status`、`topic_filter`、`output_format`、`other`；`quote` 必须是用户输入的大小写不敏感精确子串，且不超过 300 字符，否则该次计划或更新被拒绝（不进入检索，也不保存任务修订）。

程序只按结构判定：数组非空即在检索前停止（`status=unsupported`、`stop_reason=unsupported_requirements`），并把这些原文片段写入 `request.unsupported` 与 `warnings`；数组为空则进入检索，程序不再对任何句子做“其实已满足”的文本推断。一次性 `workflow.run` 与 `ChatSession.update_task` 共用同一校验函数，因此同一输入给出一致结论。

`topic`、`purpose`、`reading_goal`、`ranking`、类别及其最低篇数、目标/区间数量、`venue`、年份范围、`publication_type`、`venue_scope`、`accepted_only`、`presentation_types`，以及产品自身保证的展示契约（逐篇 `rank`、未知值 `null`、`gaps`、不添加 benchmark 标签或分数）都必须写入对应字段，不得放进 `blocking_requirements`。只有产品无法检索或无法核验的限制（代码、数据集、奖项、作者/机构、引用或页数阈值、语言、版本/评审状态、更窄的会议或年份子集、自定义输出格式、额外主题筛选等）才列入，并必须给出 `kind`。字段级的会议/年份范围仍由可验证出版记录统一判定；请求更宽的年份窗口不会被回溯检索，交付结果也不会越界。

不再存在“某句话已被视作满足”的白名单：即使模型把已由产品保证的展示要求放进 `blocking_requirements`，程序也只按结构停止，不做文本豁免。

唯一的结构性例外是**重复申报已应用范围**：`venue_subset` 未点名任何会议、或点名的会议集合恰好等于已应用的 `venue`；`year_subset` 未点名年份、或年份恰好等于已应用的（连续）区间——这类条目会被视为冗余，记一条警告后继续；若这两个字段为空而引文里点名了已知会议/连续年份，则先写入字段再视为冗余。**范围更窄的要求（如已应用六会却要求只 CVPR 和 ICCV、或只 2025）仍会停止**。该判断只比较显式值，不解析句子。

两个 schema（`PLAN_SCHEMA` 与 `TASK_UPDATE_SCHEMA`）都把 `blocking_requirements` 列为必填；当供应商只能走 `json_object` 兼容路径（不发 `json_schema`）时，同一校验函数在本地执行，结构与原文子串要求不因此放松。旧的结构化 `request_data` 仍可携带 `unsupported` 字符串数组，它不是新的解析通道：非空即停止，空则照常执行，与 `blocking_requirements=[]` 一致。

交付的每篇论文含 `rank`（从 1 开始，等于交付顺序）以及 `title`、`authors`、`year`、`venue`、`doi`、`url`、`arxiv_id`、`citation_count`、`citation_source`；未取得的元数据为 `null` 而非空字符串。CLI JSON、`report.md`、产品 API 与 Web 论文卡片共用这一形状，卡片序号取该 `rank`，筛选或仅展示性排序不会重新编号。

## Sources

- `GET /api/v1/sources`：读取配置、能力、最近实际健康观测和已知额度；不自动探测。响应中的 `routing` 只列出当前可执行的来源顺序，`cooling_down` 列出启用但仍在冷却的来源。
- `POST /api/v1/sources`：保存 `{name, enabled, priority, api_key?, endpoint?}`，密钥留空保留原值，`clear_key=true` 显式清除。
- `POST /api/v1/sources/{name}/test`：`{"query":"retrieval augmented generation"}`，执行有界真实探测。
- `GET /api/v1/sources/lune/catalog`：读取实际会议目录。
- `GET /api/v1/sources/lune/skills`：列出有边界的实际 MCP 操作。
- `POST /api/v1/sources/lune/skills/{skill}`：`{"arguments":{…}}`，执行检索、相关论文、全文、引用等白名单操作。

- `GET /api/v1/sources/lune/quota`：只读取本机共享额度快照与未完成预留，不发 Lune 请求；`external_usage_unknown=true` 表示其他客户端的用量无法由本机账本核实。Lune MCP 每次 HTTP 尝试、自动 OAuth token 刷新以及 `scripts/lune_oauth.py` 的注册和 token POST 均须事先预留；失败和未知结果不退款，上述 OAuth/MCP 请求拒绝重定向。浏览器授权页面及其他客户端的请求并不受本地账本完全控制；本地剩余量不等于供应商账户实际余额。
- `POST /api/v1/sources/lune/quota`：显式确认 `{"remaining": 300, "expires_at": 1890000000, "acknowledged": true}`；`expires_at` 是快照最晚有效时间的 Unix 秒数。示例时间仅用于说明，实际值必须由用户核对。未确认、过期或耗尽时在发送请求前拒绝，绝不自动假设重置。
- `POST /api/v1/sources/lune/quota/reconcile`：对已确认不再运行的未完成预留发送 `{"id":"...", "acknowledged":true}`，将其标为未知且不返还额度。不要对仍在运行的请求执行此操作。
- `GET /api/v1/sources/{name}/inflight/legacy`：只统计该来源无 owner 的旧版并发槽，不发网络请求。仅在**确认所有旧版客户端和本地操作已停止**后，才可通过 `POST` 同一路径并发送 `{"acknowledged":true,"legacy_clients_stopped":true}` 显式恢复这些旧槽；只处理完全无 owner 的槽，记录恢复次数，不删除历史、不处理有 owner 的槽。调用者须自行核实确认条件，远端工作结果仍未知。

`scripts/lune_oauth.py --open` 不打印一次性授权 URL；手动模式会显示该敏感 URL，应避免复制到日志或 AI 对话。自动 token 刷新在同一 token 文件路径上使用进程间协作锁，并以临时文件替换；这不能限制不使用该锁的其他客户端。注册/刷新响应体有大小上限，但慢速网络响应仍无严格的端到端硬墙钟保证。凭据保存失败或刷新结果未知时不得假定旧 token 仍有效或自动重复请求。

### 每日额度窗口自动化（`lune_quota_schedule.py`）

Lune 没有公开的余额查询接口。本模块只自动化**窗口**，不声称知道账户余额：

- `python lune_quota_schedule.py --enable --mode assume_full_allowance_at_reset --reset-utc-hour 0`：在配置的 UTC 重置时刻开启新窗口，按文档日额度填写，到期取下一个重置时刻；Web 服务运行期间每 5 分钟检查一次。
- 每次自动写入都带 `assumption` 标记；账本仍返回 `external_usage_unknown: true` 与 `account_remaining_known: false`，其他客户端当天已消耗的部分不可见。
- `--mode manual`（默认）不自动确认，只报告窗口过期，等待人工核对后确认。
- 存在未完成预留时刷新 fail closed，不会覆盖旧窗口。
- 查看 `--status`、立即执行 `--run-now`、关闭 `--disable`。该模块不访问网络，也不读取密钥。

Lune 的认证请求要求 HTTPS；既有 OAuth 或环境变量来源 key 不会自动发送到不同于已配置 MCP 服务器的 origin。使用已保存来源 key 时，变更端点须在同一次设置操作中重新输入或清除该 key；自定义端点和所输入 key 的归属由用户确认。DeepSeek Search 也只使用独立来源 key，不回退到模型 key；含凭据的调用要求 HTTPS，环境 key 只适用于官方 origin，向自定义 origin 发送保存的 key 需要在来源设置中对该目标重新输入并绑定。历史自定义端点与未绑定的旧 key 原样保留，但在重新确认前不发送；自定义 Search 端点的 key 不自动用于官方 Fetch。旧配置保留，不代表已验证其历史授权。MCP 不自动跟随重定向；其他来源与模型传输也拒绝跨 origin 重定向，可能需要将端点配置为最终地址。以上限制不改变或删除已保存配置。

同样支持兼容的 `/api/sources` 路径。MiniMax 等模型配置独立于来源配置；切换生成模型不会将其 API key 发送给其他学术来源。Lune OAuth 使用用户目录中的既有凭据，token 不返回浏览器。来源也各自使用独立凭据：`S2_API_KEY`（Semantic Scholar，建议配置以降低匿名 429）、`OPENALEX_API_KEY`（可选）和 `DEEPSEEK_SEARCH_KEY`（必需，不再回退到模型 key）。这些值可以在 Sources 页面保存，也可以由环境变量或 `.env` 提供；API 只返回 `auth_configured`，不回传密钥。

语义来源被限流后会按失败次数指数冷却；DBLP 的 `bot_challenge` 冷却 6 小时，避免每个会议/期刊查询重复等待。较早发起的请求即使后来成功，也不会自动清除其他请求留下的活动冷却；自动清理仅处理已到期的冷却，较短的新冷却也不会缩短既有冷却。显式 `POST /api/v1/sources/{name}/test` 仍会先清除该来源的冷却并立即发一次真实探测，这会暂时影响同一来源的其他请求，适合确认恢复条件后由用户主动操作。单来源因冷却未派发时返回不可用错误；为兼容旧接口，单来源缺凭据仍可能产生 `skipped` / 空列表，应查看来源诊断而非当作已执行的零结果。

来源健康是带时间的观测，缓存命中不代表当前在线；免费来源的限流和 Lune 的供应商额度仍按实际服务响应处理，未知限额不会显示成无限或零。当前旧缓存键不包含 Lune 账户或端点身份，因此含 Lune 的新查询不会读取或写入该共享缓存；旧缓存文件保留但不复用。用户显式重新搜索可能产生新的 Lune 请求，页面加载或上下文切换不会因此自动搜索。

arXiv 官方 API 没有 API key 或个人付费档，限制为同一出口 IP 每 3 秒最多一次、单连接；更高请求率需通过 arXiv support 咨询，不保证批准。大批量场景应使用官方 Kaggle 数据集、S3 requester-pays 或 OAI-PMH。

arXiv 的 429 / `Rate exceeded` 会暂停该来源 15 分钟，不再在同一次请求内立即重试；这是 arXiv 的 IP 限流，不是配置错误。若本机需要学术代理，可设置 `SG_SOURCE_PROXY`（例如 `http://127.0.0.1:7890`），它只影响来源 HTTP，不影响模型调用。

Lune 操作成功但本地审计写入失败时，结果可携带 `audit_status: "failed"`；槽释放待恢复时可携带 `release_status: "deferred"`，并可能发出固定的本地失败事件。若可信来源操作在收到并验证远端响应后因本地配额收尾而中断，Sources 错误响应可另含经服务端脱敏的 `partial_result`，并标记 `remote_outcome: "completed"`、`retry_safe: false`；这不表示本地审计已完成。客户端应保留该结果，不应据此自动重复调用。这些标记不能证明远端费用或外部审计完整。Lune 搜索遇到无效或不前进的分页 offset、页数上限、提前空页或本地账本失败时，已取得论文仅作为 `partial` 候选保留，并记录停止原因，不冒充完整搜索。Lune `tools/call` 遇到 401 不会在同一调用中自动刷新后重发，因为不能证明首次调用未执行；部分明确只读的协议发现请求仍可能刷新并再次请求，每次均受配额预留。配额预留后的最终冷却检查可能因取消或超时阻止发送，此时预留仍保守计数。最终冷却观察与网络发送无法跨进程原子化，也不能撤回在途请求。

来源请求同时受速率与在途并发限制。共享 SQLite 并发槽跨 Search、Chat、探测及配合使用的进程生效：arXiv、Semantic Scholar、DBLP、Lune 上限为 1，OpenAlex、OpenReview、ACL Anthology、DeepSeek 上限为 2。`concurrency.limit/in_flight` 返回本地记录的限制与在途数；排队受取消和任务时限约束。**过期时间本身不证明操作已停止**：有 owner 的槽仅在本地操作结束或确认 owner 进程已退出时回收，进程状态未知、PID 复用或旧版无 owner 的槽保守占用。即使 owner 已退出，另一个子进程或远端工作仍可能继续。升级来源数据库前必须停止共享该数据库的所有旧版进程并确认本地操作结束；新旧版本混跑不能保证限流，旧槽的人工恢复条件见上文。取消尚未派发的速率等待时，使用独立版本比较，仅撤销仍是自身最后一次的预约；无法确认时保守保留。

传输故障保留不同错误码：`dns_error`（DNS 解析）、`connection_error`（连接拒绝/重置）、`timeout`（真实请求超时）、`transport`（其他网络传输故障）。来源健康、执行事件及诊断使用相同分类，不将连接失败改写为超时。

## Python 客户端

```python
import requests
import time

base = "http://127.0.0.1:8765"
receipt = requests.post(base + "/api/v1/search", json={
    "query": "推荐 5 篇 RAG 论文，用于比较评测方法",
    "source": "lune",
    "client_request_id": "example-search-001",
}, timeout=10)
receipt.raise_for_status()
operation = receipt.json()
while operation["state"] in {"queued", "running"}:
    time.sleep(1)
    response = requests.get(base + operation["links"]["self"], timeout=10)
    response.raise_for_status()
    operation = response.json()
if operation.get("error"):
    raise RuntimeError(operation["error"])
result = operation["result"]
for paper in result["papers"]:
    print(paper["rank"], paper["paper_id"], paper["title"])
print(result["status"], result["gaps"])
```

此例仅消费 API，可在后续独立工程中接入用户自己的数据集与评分逻辑；当前产品不内置该评测流程。

## 本地与历史部署

本轮交付本地 Python 服务和 API。`cloudflare-*` 是旧的独立 JavaScript 演示实现，不代表当前 API 契约，未在本轮发布或同步。旧 `/api/jobs`、`/api/chat/sessions` 页面接口继续作为兼容层，详细生命周期以 v1 operation 契约为准。
