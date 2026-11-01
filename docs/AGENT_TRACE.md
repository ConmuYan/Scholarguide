# Agent trace: a trace tree, not a story card

The Activity view now follows the interaction model used by production agent-observability
products rather than a marketing “pipeline” diagram:

- **LangSmith** exposes a trace as a tree of runs (LLM, tool, chain), with a timeline/waterfall,
  input/output and metadata.
- **LangGraph Studio** exposes the executable graph and the state/checkpoint at each node.
- **AgentOps** uses a session → agent → workflow/task → LLM/tool span hierarchy.
- **Arize Phoenix** renders nested spans with timing, token usage and errors.

The useful abstraction is therefore **trace → span tree → span detail**, plus a graph for the
runtime topology. A four-card summary is readable but it is not how an engineer debugs an agent:
it hides order, nesting, retries, rejected actions and tool boundaries.

## What Activity renders now

```
Execution graph        Input → Plan/Understand → Intent → Runtime → Tools → Critic → Verify → Deliver
Trace tree             root turn
                         ├─ 01 Understand        PROPOSAL  TaskState patch / plan
                         ├─ 02 Intent policy     CONTRACT  goal / mix / deterministic quota
                         ├─ 03 Runtime loop      NODE      planner / decide / retrieve / ...
                         │    ├─ decision        CONTRACT  chosen action / reason
                         │    ├─ search          TOOL      query / returned
                         │    ├─ assess          TOOL      papers / verified
                         │    └─ guardrail       ERROR     rejected action / fallback
                         ├─ 04 Verify            GATE      coverage / delivery check
                         └─ 05 Deliver           GATE      papers / stop reason
Span inspector         input fields, output fields, policy context, raw JSON
Raw event stream       collapsed by default; full audit payload
```

### Span types make the boundary visible

| Type | Meaning | Examples |
|---|---|---|
| `PROPOSAL` | model output that was parsed and schema-validated | `task_update` patch, `plan` query |
| `CONTRACT` | deterministic policy decision | `intent` quota, `allowed_actions`, `decision`, `reuse` |
| `NODE` | runtime state transition | `planner`, `retrieve`, `assess`, `critic`, `verifier` |
| `TOOL` | external or retrieval operation | `search`, `assess batch` |
| `GATE` | evidence / delivery boundary | `check`, `delivery_check`, `delivered`, `stop` |
| `IMPORT` | imported Search audit evidence | original Search `plan/search/assess/check` |
| `ERROR` | a rejected proposal or failure | `invalid_action`, tool error |

Clicking any row opens the inspector. For a `task_update` it shows the message and patch; for a
`decision` it shows the chosen action, reason, legal set and budget; for a tool it shows the query
and observation; for a gate it shows the evidence count and stop reason. The raw JSON is one
click away.

## Two real examples

1. **GAD live Search** (`57f79127…`, `docs/ui/activity-gad-1440.png`)
   - graph: `Input → Plan → Intent → Search → Assess → Check → Deliver`
   - `intent=frontier` → `1 survey + 4 research`
   - 10 candidates, two assess batches, 5 selected, 0 gaps, `requirements_met`
   - `SINGLE-PASS WORKFLOW` is stated explicitly; no fake per-action LLM controller.
2. **Demo follow-up** (`Demo · RAG evaluation walkthrough`, `docs/ui/activity-trace-demo-1440.png`)
   - model proposes `category=survey`
   - harness offers `ASSESS/SEARCH`
   - after assessment it offers `SEARCH/FINISH`
   - a proposed `SEARCH` is rejected as `repeated_query`; fallback is `FINISH`
   - delivery passes; 5 papers, 4/5 evidence-verified; the unverified paper is not counted as
     evidence. This is the trace we want: **the model is not allowed to keep looping**.

## Why this is the right direction

The visual hierarchy reveals the system's contract:

1. **LLM proposes** structured intent and one action.
2. **Harness owns legality**: `allowed_actions`, quotas, budgets, retries and evidence gates.
3. **Runtime owns state**: revision, candidates, checkpoints, reuse and stop conditions.

That is more informative than a prose narrative and more honest than a chat transcript. It also
makes failures legible: a rejected action and its fallback are first-class spans, not an
afterthought.

## Chat follow-up trace (inline)

Chat no longer discards the event feed after updating the running label. Each turn now has a
collapsible **Agent 轨迹** block directly below the user message and above the paper list:

- the latest turn opens by default; older turns stay collapsed but are fetched on first expand;
- the list is filtered by the turn revision, so a follow-up does not replay the previous turn's
  events;
- full audit summaries include `provider`, `agent_node`, `decision`, `invalid_action`, `search`,
  `assess`, `delivery_check`, `delivered`, `ask` and errors; raw payloads stay in Activity;
- Activity remains the complete trace explorer via `在 Activity 查看完整审计 →`.

While a turn is running, the running strip also shows a bounded live event list fed by
`/api/chat/sessions/<id>/events?after=...`; the list is cleared for each new turn.

## Failure diagnosis beside the trace

The trace answers “what happened”; `diagnosis.py` answers “what should happen next”.
For a no-results turn it extracts provider-level search errors, counts, constraints and
near misses, then returns a stable code and explicit recovery actions. Chat/Search render
the summary; retryable actions (relax year/venue, lower target) are user-triggered and are
never applied silently. See `docs/FAILURE_POLICY.md` and
`docs/answers/2026-10-19-agent-failure-recovery.html`.

## Current limits / next increments

- Per-span latency and token usage are not yet persisted for every Chat event, so the tree shows
  order/time and run-level cost rather than a fully proportional waterfall. The next step is to
  attach `duration_ms`, `tokens_in/out` and `cost` to each span (the router already records some
  latency/cost for search).
- The graph view is a compact observed path; a future increment is step-through replay over
  checkpoints, like LangGraph Studio.
- Raw prompts and hidden chain-of-thought are intentionally not stored or displayed. The inspector
  shows validated structured outputs, state and tool observations.

See also `docs/UI.md` and `docs/RUNTIME_GOVERNANCE.md`.
