# ScholarGuide + OpenAI OAuth: FB-02 after the delivery-contract fix (2026-11-17)

## Outcome

The OpenAI OAuth path now completes a real FB-02 run: planning, multi-source search, abstract
assessment and delivery of five ranked papers. It is **scorable**, unlike attempts 1–5, and the
earlier failures remain preserved and unchanged.

- Evidence: `runs/frontier-fb02-openai-oauth-gpt6luna-20261117-6/result.json`,
  `score-standard.json` and `report.md`.
- Model/profile: `gpt-6-luna` through the official Responses endpoint and the `openai-oauth`
  profile (ChatGPT OAuth), Method A, multi-source, JSON actions, research mode off, cache off,
  300-second budget. `SG_MAX_OUTPUT_TOKENS=8000` was exported for the shell, but the saved
  profile overrides it: the effective `config.max_output_tokens` in the run is **3000**.
- Usage reported by the run: 8,388 input and 1,817 output tokens across the recorded stages;
  monetary cost is unknown (OAuth usage is not billed per token in the saved metrics).

## Score

Reference set: `frontierlitbench/corpus_v1.1/gold_world_models.jsonl` plus
`frontierlitbench/review/final_adjudications_v1.1.json`. Gains S/A/B = 5/3/1; IDCG from the
frozen Gold plus eligible adjudications.

| Rank | Recommendation | Match | Tier | Gain |
|---:|---|---|---|---:|
| 1 | Learning Latent Action World Models in the Wild | Gold | A | 3 |
| 2 | Simplifying Latent Dynamics with Softly State-Invariant World Models | Gold | B | 1 |
| 3 | Co-Evolving Latent Action World Models | Gold | B | 1 |
| 4 | DDP-WM: Disentangled Dynamics Prediction for Efficient World Models | Gold | B | 1 |
| 5 | AdaWorld: Learning Adaptable World Models with Latent Actions | Gold | B | 1 |

**nDCG@5 = 0.3357**, WeightedHit@5 = 28%, S-Tier Hit@5 = 0. All five positions matched Gold, so
the closed-world zero rule for out-of-set positions did not need to be applied.

This is one run, not a three-run mean, and the identical value to the isolated v0.1.6 DeepSeek run
reflects the same five gains, not a model comparison.

## What changed before this run

1. The delivered `papers` array is now one shared shape: an explicit 1-based integer `rank` and
   `null` (not an empty string) for metadata that was not retrieved. It is produced once in
   `report.delivered_papers()` and used by the CLI JSON, `report.md`, the product API and the web
   paper cards. This makes FB-02's "explicit rank / null for unknown details" an implemented
   product contract rather than a request the planner had to reject.
2. The planner prompt now states that this result shape is provided, and that a shortfall is
   reported in `gaps` with no benchmark labels or scores.
3. `quantities.clause_is_satisfied()` drops a planner-echoed clause only with positive evidence
   that the plan or the result already satisfies it (venue/year scope already applied; rank,
   null, gap or label instructions). Constraints that could change which papers qualify (code
   release, awards, authors, citation thresholds, custom formats) remain unsupported and still
   stop the run before any spend.

## Boundary

Five earlier luna attempts remain recorded and unscored: `...-1` (HTTP 400), `...-2` and `...-3`
(planner unsupported), `...-4` and `...-5` (planner unsupported on further output clauses before
the fixes above). The separate `gpt-6.1-terra` attempt also remains unscored. Corpus and adjudication
coverage is not a claim that outside papers are scientifically invalid, and one completed run is not
evidence about model ranking in general.
