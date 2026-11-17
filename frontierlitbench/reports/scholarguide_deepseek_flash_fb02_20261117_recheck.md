# ScholarGuide + DeepSeek Flash: FB-02 recheck (2026-11-17)

## Result

The single requested recheck stopped at planning with **`unsupported / unsupported_requirements`**. The planner identified `Use null for unknown details` as unsupported. No scholarly source was queried and zero papers were returned. This is an unsuccessful run, **not a scored search and not an nDCG of zero**.

Evidence: `runs/frontier-fb02-deepseek-flash-20261117-1/result.json` and companion files. The saved configuration records `deepseek-flash`, environment profile, multi-source retrieval, Method A, JSON actions, research mode off and cache off. The active environment endpoint host was independently verified as `api.deepseek.com`; the saved `provider=openai` denotes the compatible provider adapter, not the company hosting this run. The model call reported 2,219 input and 5,159 output tokens; no monetary cost was available. The timeout budget was 300 seconds and the elapsed time was 28.066 seconds.

## Interpretation

The historical FB-02 DeepSeek Flash matrix has three completed runs with a mean nDCG@5 of 0.3357. Those runs predate changes to the planner instructions. In particular, before this recheck the instructions were updated to explicitly preserve `exact null representation` as unsupported when the current output contract cannot guarantee it. Thus the earlier successes and this blocked attempt **do not isolate model behavior**, and this recheck must not be used to negate the historical scores. The current output places recommendations in ranked array order but does not guarantee per-paper `rank` or `null` for all unknown fields.

Prior OpenAI `gpt-6-luna` and `gpt-6.1-terra` attempts also stopped at planning with output-format requirements listed among unsupported constraints, but were run under different exact planner revisions. All attempt evidence remains separate and no automatic retries were performed.

If a future authorized run reaches a scorable result, the intended closed-set reference is `frontierlitbench/corpus_v1.1/gold_world_models.jsonl` plus `frontierlitbench/review/final_adjudications_v1.1.json`; out-of-set positions receive zero under the user's specified convention, while unresolved conflicts are not silently assigned zero.
