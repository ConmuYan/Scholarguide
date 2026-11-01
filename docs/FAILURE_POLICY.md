# Failure and recovery policy

Status: implemented diagnosis layer; automatic constraint relaxation remains a future policy increment.

## Why

A no-results turn is not one failure. The CVPR 2026 incident combined:

1. a venue label (`CVPR 2026`) that the task updater put into `venue` instead of
   splitting the year;
2. OpenAlex failing to resolve that label, DBLP returning an HTML challenge and
   Semantic Scholar being skipped;
3. expensive assessment of stale candidates before hard-constraint feasibility
   was known;
4. a generic no-results message with no recovery interface.

The agent should optimize **calibrated delivery**, not force a target count.

## Contract

- **Hard constraints**: venue, year range, evidence requirements, user exclusions.
- **Soft targets**: target count and reading-goal mix. A partial list may be
  explicitly offered, never padded with unrelated papers.
- **Derived constraints**: category quota and source selection. They must be
  visible in the result and never silently changed.
- **Provenance**: every recommendation still needs a verified abstract quote and
  source/venue metadata. A relaxed search may not relax evidence verification.

## Failure taxonomy

`diagnosis.py` returns one stable code plus structured evidence:

| code | meaning |
|---|---|
| `constraints_unmet` | candidates exist, but venue/year constraints have too few hits |
| `quota_unmet` | category or reading-goal mix cannot be met from the selected candidates |
| `insufficient_candidates` | no relevant candidate was found |
| `source_degraded` | source errors prevented candidate formation |
| `budget_exhausted` | turn/search/assessment budget ended |
| `requirements_unmet` | a generic unmet requirement remains |
| `ok` | no recovery needed |

Each diagnosis includes `summary`, `source_errors`, `near_misses`, counts and
explicit `actions` (relax year, relax venue, lower target, check source,
clarify). Actions that would change the request are marked `retry: true` and
require a user click; the Agent does not silently apply them.

## Recovery ladder

1. **Normalise** the request (`CVPR 2026` → venue `CVPR`, year 2026).
2. **Preflight** source capability/constraint upper bound before expensive
   assessment.
3. **Source fallback** for resolve failures, rate limits, bot challenges and
   timeouts.
4. **Explicit relaxation** of one constraint at a time via deterministic
   `/recover` patches, recorded as a new revision/event.
5. **Source fallback** via a new server-side `/fork` session that preserves the
   current request and selects the user-confirmed source.
6. **Partial delivery** with gaps and evidence when some usable papers exist.
7. **Ask/stop** with a diagnosis when no usable evidence exists.

The current iteration implements request normalisation, metadata-only preflight,
per-provider source health/error codes, diagnosis, user-confirmed recovery
actions, archive/restore, and explicit timeout observations. Remaining work is
automatic relaxation, a token-budget assessment gate, and hard cancellation.

## Evaluation

Add failure-path fixtures to the regular suite and track:

- recovery success rate and false no-results;
- constraint violations (must remain zero);
- wasted tokens/cost before diagnosis;
- time-to-diagnosis and number of source fallbacks;
- partial-delivery evidence completeness.

Happy-path precision/recall alone cannot validate an Agent that must decide when
to stop.
