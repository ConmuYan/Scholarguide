# Track A pilot hit-rate run — 2026-11-10

**Scope**: retrieval pilot. The 5 Track A tasks (FB-01/04/05/06/13) were run through a Lune-retrieval
pipeline (mechanical proxy for ScholarGuide's retrieval stage; the full agent was not executed).
Top-5 matched against frozen Gold v1.2.1 (paper_id → DOI → normalized title+year, protocol §7.2),
scored with frozen gains (S=5/A=3/B=1). One run per task.

Two probes, same retrieved pool:
- **Probe A** — Top-5 in Lune's own relevance order (raw retrieval, no influence reranking).
- **Probe B** — Top-5 re-ranked by Lune `citation_count` (metadata any system can see; approximates an "influential papers" intent).

**Macro-average nDCG@5: Probe A = 0.1988 / Probe B = 0.1546**

| Task | Valid pool | A: Hits@5 | A: nDCG@5 | A: WH@5 | B: Hits@5 | B: nDCG@5 | B: WH@5 |
|---|---|---|---|---|---|---|---|
| FB-01 | 198 | 1/5 | 0.1461 | 20.0% | 0/5 | 0.0000 | 0.0% |
| FB-04 | 68 | 1/5 | 0.1696 | 20.0% | 0/5 | 0.0000 | 0.0% |
| FB-05 | 87 | 2/5 | 0.2374 | 24.0% | 4/5 | 0.4810 | 40.0% |
| FB-06 | 43 | 1/5 | 0.0678 | 4.0% | 1/5 | 0.1461 | 20.0% |
| FB-13 | 57 | 2/5 | 0.3731 | 24.0% | 1/5 | 0.1461 | 20.0% |

## Caveats
- Optimistic bound: Gold was itself discovered via Lune (same index), so retrieval overlap is inflated vs. an independent source.
- A real ScholarGuide run (LLM + tools, 3 repeats per protocol §7.1) may score differently in either direction.
- Unmatched Top-5 papers (pending_adjudication per §7.3) were counted as gain 0 here; they are listed in the JSON, not adjudicated. Several are plausibly valid papers sitting in the 322-paper review backup pool.
