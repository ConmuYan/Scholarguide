# ScholarGuide + OpenAI OAuth: FB-02 trial (2026-11-17)

## Status

The first and initially authorized FB-02 attempt **failed** at the first planning request with HTTP 400. It returned **zero papers** and performed **zero search HTTP calls**. It is an operational failure, not an nDCG score of zero, and it must not be silently excluded or presented as a successful benchmark result.

- Attempt evidence: `runs/frontier-fb02-openai-oauth-gpt6luna-20261117-1/result.json` and the other files in that directory.
- Configuration: OpenAI Sign in with ChatGPT; `gpt-6-luna`; official Responses endpoint; ScholarGuide Method A, multi-source, JSON action protocol, research mode off, cache off, 300-second budget.
- Target reference, should a successful attempt later be authorized: `frontierlitbench/corpus_v1.1/gold_world_models.jsonl` plus `frontierlitbench/review/final_adjudications_v1.1.json`; the user-specified closed-set convention assigns zero gain to out-of-set positions. A failed run is not scored under that convention.

## Diagnosis and subsequent synthetic checks

The target account's official model catalog listed `gpt-6-luna` with `visibility=list`. A short request following OpenAI's official input-array format completed successfully. An earlier short probe using a string input produced HTTP 400; that was a probe-format error, not evidence the model was unavailable.

After the failed FB-02 attempt, an unrelated synthetic planning request reproduced HTTP 400 with `invalid_json_schema` at `text.format.schema`: ScholarGuide's plan schema has optional properties, whereas the OAuth route required a compatible schema. OAuth requests now use JSON-object mode for incompatible schemas, retain the original schema in the prompt, and retain local validation and bounded repair. A **synthetic, non-FB-02** planning request through ScholarGuide then completed with `gpt-6-luna`. No FB-02 retry is implied by this synthetic check.

After the user explicitly approved one additional FB-02 attempt, the second attempt reached the model and returned a plan, but stopped with `unsupported / unsupported_requirements`: the planner classified the benchmark's required output/verification formatting as unsupported. It made zero search HTTP calls and returned zero papers, so it too is **not scoreable**. It used one model call with 1,499 input and 270 output tokens reported. Its evidence is `runs/frontier-fb02-openai-oauth-gpt6luna-20261117-2/result.json`.

After the user requested a minimal fix and exactly one retry, planner instructions were updated to distinguish standard presentation from unsupported constraints. The third attempt (`runs/frontier-fb02-openai-oauth-gpt6luna-20261117-3/result.json`) still stopped with `unsupported / unsupported_requirements`, before any source search and with zero papers. The recorded unsupported clauses included both the paired influence/significance request and the requested JSON representation. This attempt used two model calls including one bounded repair, reporting 3,643 input and 941 output tokens. It is also **not scoreable**. Subsequently, the existing citation-proxy calibration was extended to recognize the entire paired influence/significance phrase, but exact per-paper `rank` and `null` output semantics remain unsupported; merely suppressing that requirement would silently relax the task. No fourth FB-02 attempt was made. These are product/model planning outcomes, not OAuth or model-availability failures.

The new authentication uses a selected local HTTP loopback proxy only after explicit user approval; other model and source traffic is unchanged. Browser authorization succeeded and the OAuth profile was added without changing the active provider. Request and refresh tokens are stored in the operating-system keyring. Token-refresh crash durability across every supported keyring backend has not been proven.

## Comparison boundary

The historical ScholarGuide DeepSeek FB-02 result, `0.3357`, and PaperSeek closed-set FB-02 result, `0.2990`, are means of three completed runs. They cannot be compared numerically with these unsuccessful attempts. Original evidence and both existing reports were not overwritten.
