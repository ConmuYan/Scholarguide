# Provider profiles and model cost contract

ScholarGuide can use one of two HTTP protocols and a local, server-side profile
store. The active profile controls the **next** Search job and Chat turn; it does
not rewrite historical result files.

## Profile fields

| Field | Meaning |
|---|---|
| `id` | short lowercase slug; `env` is reserved for the `SG_*` fallback |
| `name` | human label shown in `/settings` |
| `protocol` | `chat_completions` or `responses` |
| `base_url` | HTTP(S) base URL; query/fragment/userinfo are rejected |
| `model` | provider model id |
| `api_key` | write-only in the settings API; masked in every response |
| `input_price` / `output_price` | optional estimate price per 1M tokens |
| `max_output_tokens` | per-call output cap, default 3000; MiniMax M3.1 uses 8000 |
| `currency` | 3-letter code, default `CNY` |

`SG_PROVIDER_STORE` selects the local profile file; otherwise it is
`~/.scholarguide/providers.json`. The file is written with `0600` where the OS supports it; on Windows the service also attempts a user-only `icacls` ACL. The key is never stored in `result.json`, `meta.json`, SQLite event
payloads, trace files, logs or browser storage.

## Protocol semantics

- `chat_completions` posts to `<base_url>/chat/completions` (unless the supplied
  URL already ends there), uses `response_format: {"type":"json_object"}` and
  reads `choices[0].message.content`. Usage is
  `usage.prompt_tokens` / `usage.completion_tokens`.
- `responses` posts to `<base_url>/responses` (unless already ending there),
  sends `text.format` with the JSON schema and reads `output[*].content[*].text`
  where the part type is `output_text` (or `text`). Usage is
  `usage.input_tokens` / `usage.output_tokens`.

Both adapters feed the same `JsonLLM.generate(stage, system, data, schema,
validate)` contract used by planning, task updates, assessment and refinement.
The provider never bypasses `ChatSession`, controller legal-action checks,
tool registry, budget ledger or verifier.

## Connection test

`POST /api/providers/test` validates the submitted profile and makes a bounded
structured probe. It returns:

- `ok`, `status`, `http_status`, `latency_ms`, `model`, `protocol`
- `usage` and probe `cost` if the provider returned usage
- a sanitized `error`/`message` if failed

It does not create a session, a Search run, or a history record. Status classes
are `auth` (401/403), `model_or_endpoint` (404), `timeout`, `protocol`
(invalid/empty/unexpected JSON), and `error`. Mock tests are not presented as
real-provider success.

## Structured-output compatibility

`responses` requests carry the stage schema in `text.format`. Until 2026-10-27
they also carried `strict: true` for every provider, which failed on any gateway
that actually enforces OpenAI strict validation: our schemas leave optional
fields out of `required` and use `maxLength` / `minimum` / `maxItems`, so the
first call answered `HTTP 400` (`'required' ... including every key in
properties`, `invalid_json_schema`). MiniMax tolerated it, so the failure only
appeared after switching profiles.

The request shape is now chosen from the schema itself
(`llm.strict_schema_compatible`), not from the vendor name:

| Schema | Request |
|---|---|
| every property in `required`, `additionalProperties: false`, no unsupported keywords | `strict: true` |
| anything else, MiniMax M3.1 Flash family | `strict: true` (live-verified acceptance, `model_policy.tolerates_partial_strict`) |
| anything else, every other backend | `json_schema` **without** `strict` |

Non-strict requests still send the whole schema and the prompt still states every
constraint; the bounded repair attempt in `JsonLLM.generate` is unchanged.

Provider error bodies are no longer discarded. `HTTP` failures now read
`Model API HTTP 400 — <provider message> (param: ...); check model, endpoint and
credentials.` with the API key redacted, and `/settings` classifies schema or
parameter rejections as `protocol` instead of a generic error. If a 400/422 body
names a parameter the client can drop (`response_format`, `text`, `temperature`,
`reasoning`), `JsonLLM` retries **once** with that field removed; the retry is
charged against the phase request budget like any other request. Anything else
is reported as-is.

Live checks on 2026-10-27 (single `plan` call each, no search): DeepSeek
`deepseek-flash` went from HTTP 400 to 200 with `strict` omitted (1584 in / 612
out tokens); `MiniMax-M3.1-Flash` still receives `strict: true`. The `FastAI`
profile reproduced the same schema error and now passes the same check. This
change re-locked the core: `core_id` moved from `2bae49f4…` to `692375d0…`
(33 files). Archived batches keep their original `core_id`; old and new batches
are not interchangeable.

## Cost formula

```
model_cost = input_tokens / 1M * input_price
           + output_tokens / 1M * output_price
tool_cost  = sum(provider run cost_cny recorded in retrieval trace)
total      = model_cost + tool_cost
```

Every Search result and Chat turn carries `metrics.cost` and top-level `cost`:

```json
{
  "currency": "CNY",
  "unit": "per_1m_tokens",
  "input_tokens": 1234,
  "output_tokens": 567,
  "input_price": 2.0,
  "output_price": 8.0,
  "model": 0.007,
  "tools": 0.005,
  "total": 0.012,
  "estimate": true,
  "known": true
}
```

Missing usage or missing prices produce `model: null` and `total: null`; the UI
shows `unknown`/`cost unknown`, never a fabricated zero. This is an estimate,
not a provider invoice.

## Compatibility

- With no profile file, active is `env`, built from the existing `SG_*`
  variables. The env profile is read-only but **can be activated**; it is the DeepSeek /
  deepseek-flash fallback unless a custom profile is active. Offline demo remains `DemoSource` and does not call a provider.
- `SG_PROTOCOL`, `SG_INPUT_PRICE`, `SG_OUTPUT_PRICE`, `SG_MAX_OUTPUT_TOKENS`, `SG_CURRENCY` are optional
  fallbacks for the env profile.
- Changing the active profile affects new work; old result files retain their
  recorded model/profile metadata.
- The settings API is loopback-only and same-origin guarded like the rest of the
  local service.
