# ScholarGuide + gpt-6.1-terra: FB-02 single attempt (2026-11-17)

## Result

The one requested FB-02 attempt ended **`unsupported / unsupported_requirements`**, at the planning stage. No scholarly source was queried, no papers were returned, and no nDCG, WeightedHit or S-tier score is available. This is an unsuccessful attempt, **not** a completed search with zero-gain results.

Evidence: `runs/frontier-fb02-openai-oauth-gpt61terra-20261117-1/result.json` and its companion output files. Its trace records the model as `gpt-6.1-terra` and the two unresolved clauses as:

1. `Each paper should contain rank`
2. `Use null for unknown details.`

The run used one model call, reporting 1,659 input and 205 output tokens. Monetary cost was not available in the saved result.

## Conditions

- Official OpenAI Responses API and the authorized ChatGPT OAuth profile, with the user-approved loopback proxy.
- Model `gpt-6.1-terra` was overridden **only in the run process**; the saved `openai-oauth` profile remains `gpt-6-luna` and the active provider was not changed.
- Original FB-02 `--json-format` prompt; Method A; `multi` sources; JSON action protocol; research mode off; cache off; 300-second budget.
- The intended scoring reference was `frontierlitbench/corpus_v1.1/gold_world_models.jsonl` plus `frontierlitbench/review/final_adjudications_v1.1.json`, with out-of-reference positions assigned zero only for a successfully completed/scorable run. No score was calculated for this attempt.

The gpt-6-luna attempts and DeepSeek Flash reference runs are preserved separately. They do not establish a quantitative model ranking against this unsuccessful single attempt. In particular, the planner prompt and calibration were edited after the last luna FB-02 attempt, so these are not identical code versions.
