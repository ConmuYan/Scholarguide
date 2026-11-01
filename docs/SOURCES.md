# 多信源配置与边界

ScholarGuide 的**主实验仍然固定 arXiv**（`SG_SOURCE=arxiv`）。多信源是后续扩展，不能在论文中写成
LitSearch 等价评测。所有网络访问都走 `source.py` 的 `AcademicSource`，失败按源隔离，不静默换源或伪造结果。

## 1. 环境变量

| 变量 | 默认 | 用途 |
|---|---|---|
| `SG_SOURCE` | `arxiv` | 单源名、逗号列表或 `multi` |
| `SG_SOURCE_TIMEOUT` | `15` | 单次学术源 HTTP 请求上限（秒） |
| `OPENALEX_API_KEY` | 空 | OpenAlex polite pool（可选） |
| `S2_API_KEY` | 空 | Semantic Scholar Graph API key（可选；无 key 也可但易 429） |
| `DEEPSEEK_SEARCH_KEY` | 空 | DeepSeek Search；为空时若 `SG_BASE_URL` 是 DeepSeek 则复用模型 key |
| `SG_REQUEST_TIMEOUT` | `90` | 模型/任务请求上限；源请求还会被 `SG_SOURCE_TIMEOUT` 截断 |

密钥只放在 `.env`（被 `.gitignore` 忽略）或 shell 环境；报告、日志、SQLite 和前端不保存 key。

## 2. 支持的信源

| 名称 | 端点 / 鉴权 | 限流 | 摘要 | OA / 费用 | 备注 |
|---|---|---|---|---|---|
| `arxiv` | `export.arxiv.org/api/query`，无 key | 客户端 4s | 有 | PDF；免费 | 主实验唯一来源 |
| `openalex` | `api.openalex.org/works` + `sources`，`OPENALEX_API_KEY` 可选 | 0.2s | 有（倒排索引重建） | `open_access.oa_url`；免费 | 覆盖广；venue 先解析 `source.id`，再用 `primary_location.source.id` + `publication_year` 过滤 |
| `semantic_scholar` | `api.semanticscholar.org/graph/v1/paper/search`，`x-api-key` 可选 | key 约 1 RPS；无 key 共享池 429 | 有 | `openAccessPdf`；免费 | 无 key 时最多 3 次退避，仍失败则 45s 冷却 |
| `dblp` | `dblp.org/search/publ/api?format=json`，无 key | 1s | **无** | 只有 `ee` 链接；免费 | 本机网络曾触发 Anubis 202 HTML 挑战；适配器会报 HTML challenge，不伪造摘要 |
| `openreview` | `api2.openreview.net/notes/search?source=forum`，无 key | 0.6s | 有 | PDF；免费 | 过滤 `submitted/rejected/withdrawn`，避免把投稿当录用 |
| `acl_anthology` | 发现：`verbatim.krlabs.eu/api/v1/papers/search`；详情：`aclanthology.org/<id>/` | 0.6s | 有（详情页） | PDF；免费 | ACL Anthology 没有官方 JSON 搜索；Verbatim 是第三方发现层，失败则降级，不冒充官方 API |
| `deepseek` | `POST api.deepseek.com/v1/tools/search`，Bearer | 5 QPS；本项目每轮最多 1 次 | **无**（summary/snippet） | 5 元/千次成功请求 | 只作覆盖不足的补缺；只保留学术域名/DOI/arXiv 结果；数据在 PRC |

### ACL Anthology 的特别说明

ACL Anthology 官方只提供 GitHub 元数据/PDF，没有公开 JSON 搜索端点。实现采用「Verbatim 公共索引发现
→ ACL 官方详情页解析 `citation_*` 与 `acl-abstract`」的两步路径。Verbatim 不是 ACL 官方服务，若其不可用，
ACL 源会记录错误并降级，不影响 arXiv/OpenAlex/S2。不要把这条路径描述成官方 API。

## 3. 路由规则

`SG_SOURCE=multi` 时：

1. 学术核心：`arxiv + openalex + semantic_scholar`。
2. 查询含 venue 词（ACL/EMNLP/ICLR/NeurIPS/ICML/…）：追加 `dblp + openreview + acl_anthology`。
3. 类别为 evaluation/survey：追加 `openreview + acl_anthology`（不抓 DBLP 的纯元数据）。
4. 主题查询：不抓 venue 站点，避免无谓请求。
5. 查询变体：category=evaluation/survey 或 `prefer_recent` 时最多一个确定性变体；首轮覆盖不足才使用。
6. 覆盖仍不足且 DeepSeek key 可用：最多一次 `deepseek` 补缺；不参与首轮并列检索。
7. 单源 `SG_SOURCE=arxiv` 完全绕过 router，保持冻结主实验口径。

排序是确定性的：词面相关性 0.52、权威度 0.18、venue 0.12、时效 0.10、OA 0.05、摘要可用 0.03。
DBLP-only 记录 `metadata_only=true`，没有摘要时不会被当作可推荐证据。

## 4. 失败与降级

- 单源失败：`GuideError` 进入 run report 的 warnings；不返回伪造论文。
- 多源失败：某个源 error/skipped 只影响该源，其他源结果继续合并；UI 的 Activity 显示每源状态与耗时。
- 429/5xx：按源退避 + 冷却（S2/OpenAlex），冷却期内跳过。
- DBLP bot challenge / ACL 第三方索引失败：记录 HTML challenge 或 HTTP 错误，不把 HTML 当 JSON。
- 缺 key：`deepseek` 在 multi 中标记 skipped；单源 deepseek 返回空并提示。
- DeepSeek 每轮最多一次：`begin_turn()` 重置，router 只在覆盖不足时调用；Session 的 cost ledger 记录。

## 5. 计费与数据

- arXiv/OpenAlex/DBLP/OpenReview/ACL：当前无直接费用；仍受服务条款和限流约束。
- Semantic Scholar：免费但无 key 时共享池不稳定；建议申请 key。
- DeepSeek Search：5 元/千次**成功**请求；空结果/失败不计费；结果默认不带正文（`need_content=false`）。
- 不要把 DeepSeek Search 的网页 summary 当论文摘要；它只用于发现，摘要需由学术源补充。
- 任何外部源结果都要保留 `sources`、`abstract_source`、`open_access_url`、`retrieved_at`；缺摘要标 `metadata_only`。

## 6. 快速检查

```bash
# 单源，不改变主实验
python app.py run --request-file examples/request.json --source arxiv

# 多源（router 会按 query 选子集）
python evaluation/run_multiturn.py --methods V2MS --source multi \
  --out evaluation/results/multisource-check

# 查看 provider 注册表
python -c "from source import PROVIDERS; print(sorted(PROVIDERS))"
```

## 7. 本机 live smoke（2026-10-18）

`python scripts/source_smoke.py` 的机器可读结果在 `docs/SOURCE_SMOKE.json`。这不是评测，只证明端点和解析在当前网络可达；模型、query 和结果数都固定为 2。

| 源 | 状态 | 返回 | 说明 |
|---|---|---|---|
| arXiv | ok | 2 | 摘要/PDF |
| OpenAlex | ok | 2 | 摘要/venue/OA |
| Semantic Scholar | ok | 2 | 无 key；relevance 429 后 bulk fallback 成功，约 10s |
| DBLP | error | 0 | 本机仍被 Anubis 202 bot challenge 拦截；解析器由离线 fixture 测试 |
| OpenReview | ok | 2 | 摘要/PDF |
| ACL Anthology | ok | 2 | Verbatim 发现 + ACL 详情摘要 |
| DeepSeek Search | ok | 2 | 官方 Search API，2 条学术结果；DOI/arXiv 抽取已去 Cloudflare query |

DBLP 的失败是网络/机器人防护，不是 JSON 解析失败；适配器会抛 `HTML challenge` 而不是把 HTML 当 JSON。
Semantic Scholar 的 bulk fallback 只用于发现，结果仍按 `fields` 截断到请求条数。
