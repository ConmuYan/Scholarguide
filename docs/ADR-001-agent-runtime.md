# ADR-001: LangGraph single-agent role runtime

- Status: accepted
- Date: 2026-10-19
- Context: ScholarGuide v2 is a single-LLM controller with SQLite task state, bounded
  SEARCH/ASSESS/ASK/FINISH actions, multi-source retrieval and a local web UI. The
  controller works, but planning, execution and verification are coupled in one
  `while` loop; there is no graph-level checkpoint or explicit critic stage.
- Decision: keep one decision Agent; orchestrate it with LangGraph StateGraph nodes
  `planner → decide → retrieve|assess → critic → decide` and
  `decide → verifier → finalize`. SQLite remains the system of record; LangGraph
  checkpoints only orchestration state in `agent-checkpoints.sqlite3`.

## Why

- Explicit stages make failures and costs attributable (product observability).
- Durable checkpoints/interrupt semantics are what make it a runtime, not a loop.
- Single Agent preserves the proposal's single-agent scope and the existing
  S/V2/V2-NR comparison.
- Roles are phases, not independent agents; no handoff/message-bus complexity.

## Consequences

- New dependencies: `langgraph`, `langgraph-checkpoint-sqlite`.
- `ChatSession` gains `prepare_turn`, `next_decision`, `apply_decision`; the legacy
  loop remains as fallback (`SG_AGENT_RUNTIME=legacy`).
- Graph node failures are recorded as `agent_node`/`error` events and become partial
  deliveries, never silent success.
- Checkpoint state is not business truth: task/candidates/assessments/events/budget
  remain in the existing SQLite schema. A checkpoint loss cannot fabricate papers.
- Max graph steps and existing action/LLM/time budgets both apply.

## Alternatives considered

- Keep the legacy while loop only: rejected; no graph-level recovery/observability.
- OpenAI Agents SDK: lighter, but handoffs are multi-agent-shaped and checkpoint/
  interrupt semantics are less explicit for this state machine.
- AutoGen multi-agent: rejected for default; proposal scope and cost.
- Custom state machine with no framework: viable, but duplicates durable execution
  and checkpoint work; LangGraph is the requested mature framework.

## Acceptance

- `SG_AGENT_RUNTIME=langgraph` runs live turns through the graph.
- Existing 321-test suite remains green; new graph/checkpoint tests pass.
- 12×2 comparison reports cost/latency/expect honestly; if it regresses, runtime is
  opt-in and the result is documented.
- Frozen proposal documents and arXiv main experiment are unchanged.
