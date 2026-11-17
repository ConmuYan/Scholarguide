# PaperSeek FB-1.1: closed-world results

This report preserves the comparison requested by the user. Reference set: `frontierlitbench/corpus_v1.1/` plus `frontierlitbench/review/final_adjudications_v1.1.json`. For this report only, all positions labeled `not_in_gold` by the existing scorer receive zero gain. This is a closed-world evaluation convention, not the scorer's default pending-adjudication behavior. Gold and adjudicated matches, task eligibility, ideal gains and IDCG are unchanged. The 154 pending positions were all `not_in_gold`; they were not identifier conflicts.

Source: `C:/Users/ASUS/Desktop/hku-study/COMP7608-LLM/baseline/PaperSeek-Community-1.0.0b5-CLI-paperhub-medium-FB1.1/normalized/`, with configuration and status in the same directory's `config/registration.json` and `run-log.json`. PaperSeek Community 1.0.0-beta5 CLI, `gpt-6-luna` (model version unknown), OpenAI Responses, paperhub, medium search budget. All 45 runs are recorded complete. Each returned 14–384 papers; only the first five in original order are scored. Positions: 44 Gold matches, 15 adjudicated matches, 12 matched out-of-scope, 154 outside the current reference set. The 154 positions correspond to 109 distinct track/title pairs, not necessarily 109 distinct canonical identities.

Each task is the mean of three runs. S/A/B gains = 5/3/1. Track scores are equally weighted task means; overall nDCG is the equal mean of three track scores.

| Task | Runs | nDCG@5 | WeightedHit@5 | S-Tier Hit@5 |
|---|---:|---:|---:|---:|
| A / FB-01 | 3 | 0.0437 | 6.67% | 0.33 |
| A / FB-04 | 3 | 0.2439 | 17.33% | 0.33 |
| A / FB-05 | 3 | 0.2441 | 28.00% | 1.00 |
| A / FB-06 | 3 | 0.0677 | 9.33% | 0.33 |
| A / FB-13 | 3 | 0.0339 | 4.00% | 0.00 |
| B / FB-02 | 3 | 0.2990 | 29.33% | 0.00 |
| B / FB-07 | 3 | 0.0678 | 4.00% | 0.00 |
| B / FB-08 | 3 | 0.1780 | 18.67% | 0.00 |
| B / FB-09 | 3 | 0.2899 | 21.33% | 0.00 |
| B / FB-14 | 3 | 0.3859 | 37.33% | 0.00 |
| C / FB-03 | 3 | 0.1844 | 13.33% | 0.67 |
| C / FB-10 | 3 | 0.0000 | 0.00% | 0.00 |
| C / FB-11 | 3 | 0.3708 | 33.33% | 1.67 |
| C / FB-12 | 3 | 0.0525 | 5.33% | 0.00 |
| C / FB-15 | 3 | 0.0000 | 0.00% | 0.00 |

| Track | Macro nDCG@5 | Macro WeightedHit@5 | Macro S-Tier Hit@5 |
|---|---:|---:|---:|
| A | 0.1267 | 13.07% | 0.40 |
| B | 0.2441 | 22.13% | 0.00 |
| C | 0.1215 | 10.40% | 0.47 |
| Overall | 0.1641 | — | — |

The existing ScholarGuide reference matrix remains at `frontierlitbench/reports/scholarguide_matrix_20261111.md`. Its overall nDCG is 0.6388; all its 45 runs were scored without pending positions, so the zero-for-unmatched convention does not change its reported score. The systems used different models, search sources, configurations and numbers of returned recommendations, so this is a product-configuration comparison, not an isolated model comparison. Reference corpus coverage is not proof of real-world scientific invalidity.
