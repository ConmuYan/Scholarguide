# ScholarGuide FrontierLitBench exploratory matrix, 2026-11-11

This is an exploratory evaluation, not a final leaderboard. The reference corpora and impact tiers are under review; unmatched recommendations require shared adjudication rather than automatic zero scores. Track A uses the rebuild candidate produced from the reviewed scope decisions; Tracks B and C use the historical corpora and their respective supplemental decisions. Different reference versions must not be compared as though they were the same dataset.

The 15 tasks were each run three times with ScholarGuide revision `7b41ecce485563b3a39651d1fd4883f2b322f479`, DeepSeek Flash, method A, multiple sources and cache disabled. Forty-four runs reported `complete`; FB-14 run 3 reported `partial`, `search_budget_exhausted`, with five papers. Every run recorded at least one successful Lune search in its source trace; no Lune error, partial, skipped, or missing state was observed there. Successful retrieval alone does not establish relevance or a correct impact tier.

| Task | Scored runs | nDCG@5 | WeightedHit@5 | S-Tier Hit@5 |
| --- | ---: | ---: | ---: | ---: |
| FB-02 | 3/3 | 0.4035 | 36.0% | 0.00 |
| FB-07 | 3/3 | 0.4239 | 44.0% | 1.00 |
| FB-08 | 3/3 | 0.6154 | 54.67% | 1.67 |

These are three-run means using provisional Track B labels. FB-09 has one scored run and FB-14 has two; neither task has a complete mean. Every Track A and C task has at least one unresolved recommendation in each of its runs. In total the unresolved positions are A: 57 (31 distinct titles), B: 3 (3 titles), C: 52 (28 titles). Per-track and overall macro scores remain unavailable until all runs can be assessed against a consistent, reviewed reference. These counts describe this matrix, not the full universe of missing papers.

Track B's institutional defaults, missing citation counts, and repository evidence affect its historical tiers. Track A and C also need scope, publication and impact evidence for unlisted recommendations. Evidence gathered for some of these cases is in `frontierlitbench/review/`; identity or an abstract by itself does not determine an impact tier. Lune is a literature database source, and its use alone does not establish an advantage over other systems.

The individual results, scores, summaries, and source audit are retained in the local `runs/frontier-matrix-20261111-final/` directory. The common prompt, chat import and scoring procedures are documented in `frontierlitbench/EVALUATION_GUIDE.md`. All systems should use the same reference version and shared adjudications.
