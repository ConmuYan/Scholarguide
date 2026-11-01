# Product implementation contract — 2026-10-28

Goal `mvqxbxra-3g12as` confirmed. Main integrates and independently verifies. All workers must follow bounded delegated assignments without creating/focusing goals. Research/annotation/ablation gates are superseded; historical artifacts remain unchanged.

## Ownership

- SOURCE worker: `source.py`, `retrieval.py`, `mcp_client.py`, `mcp_lune.py`, `mcp_plugin.py`, `mcp_servers.json`, new `source_store.py`, `source_scheduler.py`, source/MCP tests. Do not edit common, webapp, app, UI or unrelated tests.
- CORE worker: `common.py`, `prompts.py`, `intent.py`, `workflow.py`, `session.py`, `state.py`, `reuse.py`, `recovery.py`, `diagnosis.py`, `preflight.py`, `research_*.py`, `controller.py`, `agent_runtime.py`, `agent_tools.py`, `tools.py`, core tests. No webapp/UI/source edits.
- UI worker: `web/*` and new browser/UI tests. No Python service/runtime edits.
- MAIN/API: `webapp.py`, `activity.py`, `app.py`, `app_mcp.py`, `llm.py`, new `execution_events.py`, `api_v1.py`, API/schema/docs and integration tests. No overlapping writes with workers.
- Documentation/decoupling worker later: README, active docs entry, engineering verification scripts, archive research tests; coordinate source test split with source worker.

## Quantity and task

Single constants: MIN_PAPERS=1, MAX_PAPERS=10, DEFAULT_PAPERS=5. Add optional `minimum_count` / `maximum_count` to Request for explicit intervals; `target_count` is selected goal (default5 within interval), exact requests min=max=target. Never mistake years for requested counts. Explicit >10 yields a clear upper-bound notice/error rather than silently choosing5. Coverage requires min for interval and target for exact. category quota <= allowed count; conflicts validated before state mutation. Publication hard filters remain.

CORE adds optional `Config.source_store` (default None, repr=False, compare=False) so replace() preserves the service's injected SourceStore. Config.public excludes it. Source worker consumes it. No change of framework.

CORE provides `ChatSession.start(request_data=None,message=None,constraints=None,goal_hint=None)` with count parsing/validation shared by workflow; constraints are explicit overrides after parsing, validated atomically before execution. Exposes optional `progress` callback constructor attribute, invoked by `_event` after actual persistence with event `{action,at,revision,payload}`. No model internals or keys in events. `_event` does not call execution_events.emit (avoids recursion).

Continuous Chat uses per-turn budgets; history & compatible assessments persist. New messages can continue indefinitely with bounded work per operation, including old sessions written with exhausted feedback counters. Limits remain configurable/protective. Keep cumulative usage honest. Search API and live Web Search should use same ChatSession/LangGraph application path; legacy workflow remains compatibility/offline adapter. No fake reset of actual monetary usage.

## Execution event interface (MAIN supplies)

`execution_events.bind_events(sink)` contextmanager where sink signature is `(action: str, payload: dict)`. `emit(action, **payload)` publishes only if bound. `operation(tool, *, source='', category='tool', **public_fields)` contextmanager creates `span_id`, emits `tool_start`, and `tool_end` or `tool_error` with duration_ms, actual status/error. Returned dict can be enriched with count/usage by caller. Never pass credentials, prompts or request headers.

CORE binds events to `self._event` for a whole start/send/recovery execution; source and LLM wrappers emit actual nested spans through that scope. Legacy workflow bridges emitted events into its normal progress function. MAIN sends Chat progress to Jobs when Search uses ChatSession.

Legacy events remain readable. Public event normalization adds `seq`, `event_id`, `operation_id`, `session_id`, `turn`, `at`, `action`, `payload`. Per-stream IDs are stable append-only across retries/reconnects. SSE event name `trace`, `id: <seq>`, JSON data same normalized object; terminal event `done`. GET accepts after/Last-Event-ID, JSON fallback. Heartbeat never invents progress.

## Source service (SOURCE supplies)

`SourceStore(path=None)` default under ~/.scholarguide (or SG_SOURCE_STORE), server-side durable settings/health; opening lists must not issue paid probes.

Methods: `public_state() -> {'sources': [...], 'default_source':'multi', ...}`; `upsert(data:dict)->public source`; `configure(config)->Config` applies independent source credentials/settings and attaches config.source_store; `probe(name,config,query='retrieval augmented generation')->dict` does real bounded request and returns observed health; `catalog(name,config)->dict` optional actual Lune conference coverage; `skills(name,config)->dict` capability descriptors; `invoke(name,skill,args,config)->dict` if supported grounded workflow. Unknown/unsupported operations produce GuideError with clear code.

Public source fields: `name,label,enabled,priority,configured,auth_configured,capabilities,health,quota` (unknowns null), no full secrets. Capabilities include filter modes native/client/enrich/unsupported, abstract/publication/presentation/fulltext/related/citations/skills. SourceStore secret config never goes into model context, output or events. Config.source='multi' respects enabled priority and Lune when configured; explicit source doesn't silently expand allow-list. Register Lune via normal lazy registry rather than per-module tuple rebinding. Source list canonical in common, SOURCE requests any changes there from CORE.

Fix merge empty keys, Lune source-native identity, true abstract vs summary, year/paging, token refresh, request budget, safe quota ledger, HTTP/SSE classification, honest cached per-source runs, durable retry/cooldown. Provide failures for unsupported filters; publication metadata is not invented. DeepSeek Search and Fetch adhere to https://api-docs.deepseek.com/zh-cn/guides/search/ ; source credentials independent from active LLM.

MAIN endpoints consumed by UI: GET `/api/sources`; POST `/api/sources` (upsert with name), POST `/api/sources/{name}/test`, GET `/api/sources/{name}/catalog`, GET `/api/sources/{name}/skills`, POST `/api/sources/{name}/skills/{skill}`. Equivalent v1 routes provided. Sources page `/sources`.

## Public API (MAIN supplies)

Version `v1`. POST `/api/v1/search` accepts query or request string, optional count1..10, source/providers, filters, request_data, mode live/demo, stream default true, client_request_id. POST `/api/v1/chat` accepts message, optional session_id plus explicit supported request/config fields. API validates before execution. No current dataset or scoring dependency.

Response async: `{api_version:'v1',operation_id,session_id,turn,state:'running',links:{self,events,cancel,result},result:null,error:null}` HTTP202. Search operation tracks its durable Jobs ID; Chat each turn has independent op ID. Existing `/api/jobs` and `/api/chat/sessions` kept as compatibility adapters. GET `/api/v1/operations/{id}` returns snapshot + ranked complete JSON result; POST cancel; GET events (SSE by Accept or stream query, otherwise JSON); GET result. Idempotency uses client_request_id or Idempotency-Key without duplicate dispatch on retry.

Unified result includes status, stop_reason, request, papers (all1..10), per-paper rank/identity/title/url/DOI/abstract/publication/provenance/reason/evidence, gaps, warnings, question, usage/cost. Structured errors `{code,message,field,retryable}`. OpenAPI/schema and runnable Python/curl examples.

## UI

Remove required quantity dropdown: natural-language count with parsed task preview; guided input still accepts count in topic/request. Keep hard filters supported and visible, source choices from capabilities; remove ablation/reuse checkbox (automatic valid reuse stays). Source-aware errors and honest partial results. Chat supports repeated turns, reload/restore trace and stable result upsert, retryable details failures, asynchronous recovery. Real events drive modern compact timeline/spans, expanded ongoing tool, source/status/duration, restrained animation, reduced motion, light/dark, mobile. Preserve existing readable information design rather than fake timers/percentages. All pages have Sources navigation. API retains raw details for expansion.

## Verification

Tests must verify behavior and diagnosed regressions. Update assertions that encode superseded product limits; do not weaken safety or evidence checks. Archive standalone research metric/frozen-proposal tests separately, retaining pure product regressions when mixed. Main performs final current-source full engineering suite, true Chrome, MiniMax and Lune calls, and service version check. No real API test fabricated by mock, no benchmark/annotation work.
