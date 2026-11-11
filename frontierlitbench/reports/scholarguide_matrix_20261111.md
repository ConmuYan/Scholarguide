# ScholarGuide FrontierLitBench matrix (FB-1.1)

Status: **complete, reviewed v1.1 reference matrix**. All 45 planned runs have a final score against the same versioned corpora and the shared 76-record adjudication table. This report measures ScholarGuide's ability to retrieve, rank and return high-tier papers; it is not a leaderboard across unrelated systems.

## Run conditions

- ScholarGuide revision: `7b41ecce485563b3a39651d1fd4883f2b322f479`.
- Provider/model: DeepSeek Flash through the configured DeepSeek provider.
- Method: A, multiple retrieval sources, agent/research workflow, cache disabled.
- Promotion: exactly five ranked papers requested for each of 15 tasks; each task repeated three times.
- Run status: 44 `complete`, 1 `partial/search_budget_exhausted` (FB-14 repeat 3, which still returned five papers). The scorer applies the existing bounded-search rule and scores the five returned positions.
- Lune source audit: all 45 runs contain successful Lune search states; 50 Lune invocations across the saved traces returned `ok`, with no recorded Lune error. Lune is a literature database/tool source, like Semantic Scholar, DBLP and OpenAlex. This evidence does not establish an independent or causal Lune advantage.

## Reference data

| Track | Scope | v1.1 Gold | Tiers S/A/B | Years 2024/2025/2026 |
| --- | --- | ---: | ---: | ---: |
| A | Video generation and editing | 163 | 33/65/65 | 47/79/37 |
| B | World models | 128 | 26/51/51 | 34/47/47 |
| C | LLM agents and tool use | 134 | 27/54/53 | 21/43/70 |

The matrix initially had 112 unresolved recommendation positions representing 62 distinct title/year papers (A 57 positions, B 3, C 52). The unified review adjudicated all 62 papers and retained 14 earlier decisions: 76 records in total. All are retained as a separate adjudication pool; frozen Gold records are not edited.

Publication metadata and abstracts were checked for eligibility. Track B also received 13 Lune citation observations, 13 paper-affiliation reviews and two official-repository corrections. Across the 62 newly adjudicated papers, 45 have an attributable repository. Unmapped institutions use the documented conservative value `0.4`; a missing attributable repository receives `0` rather than an invented star count. No reviewed supplemental paper remains missing a citation count.

## Results

Gains are `S/A/B = 5/3/1`. Each task value is the mean of three runs. nDCG@5 uses the frozen Gold plus the shared eligible adjudication pool as IDCG. Missing positions after a successful bounded search receive zero.

| Task | Scored runs | nDCG@5 | WeightedHit@5 | S-Tier Hit@5 |
| --- | ---: | ---: | ---: | ---: |
| A / FB-01 | 3/3 | 0.7629 | 68.00% | 2.67 |
| A / FB-04 | 3/3 | 0.9650 | 94.67% | 4.67 |
| A / FB-05 | 3/3 | 0.5063 | 41.33% | 1.00 |
| A / FB-06 | 3/3 | 0.7803 | 70.67% | 2.33 |
| A / FB-13 | 3/3 | 0.6436 | 57.33% | 2.33 |
| B / FB-02 | 3/3 | 0.3357 | 28.00% | 0.00 |
| B / FB-07 | 3/3 | 0.5262 | 52.00% | 1.00 |
| B / FB-08 | 3/3 | 0.6555 | 60.00% | 1.33 |
| B / FB-09 | 3/3 | 0.4289 | 38.67% | 0.00 |
| B / FB-14 | 3/3 | 0.4213 | 36.00% | 0.00 |
| C / FB-03 | 3/3 | 0.6757 | 65.33% | 1.00 |
| C / FB-10 | 3/3 | 0.6368 | 60.00% | 0.67 |
| C / FB-11 | 3/3 | 0.7375 | 65.33% | 2.00 |
| C / FB-12 | 3/3 | 0.8315 | 76.00% | 2.67 |
| C / FB-15 | 3/3 | 0.6752 | 65.33% | 2.00 |

| Track | Macro nDCG@5 | Macro WeightedHit@5 | Macro S-Tier Hit@5 |
| --- | ---: | ---: | ---: |
| A | **0.7316** | **66.40%** | **2.60** |
| B | **0.4735** | **42.93%** | **0.47** |
| C | **0.7113** | **66.40%** | **1.67** |
| Overall | **0.6388** | - | - |

Track scores are equally weighted task macros; Overall is the equal mean of the three track macros. Track B is the clear weakness in this matrix. FB-02 and FB-14 returned no S-tier hits on average, while FB-09 also had no S-tier hit and weaker nDCG. This result is evidence about the combined effect of planning, query construction, source retrieval, metadata normalization and final ranking; it is not explained by which source supplies the underlying paper database.

## Interpretation limits

FB-1.1 is reviewed and evaluable, but it is not an exhaustive census. Absence from the corpus is not proof that a recommendation is invalid, and the relevance and impact labels use a single-primary-reviewer protocol with deterministic evidence. Institutions without a rubric mapping retain the conservative `0.4` floor. The impact labels are research-reference labels, not objective measures of scientific quality. Comparisons must use the same corpus version, adjudication file, prompt and run conditions.

Machine-readable outputs used for this report are in `runs/frontier-matrix-20261111-final-v1.1-scores/`. The corpora are in `frontierlitbench/corpus_v1.1/`, the unified decisions are in `frontierlitbench/review/final_adjudications_v1.1.json`, and the review summary is in `frontierlitbench/review/final_adjudication_report_v1.1.json`.
