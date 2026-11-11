# FrontierLitBench 1.1 delivery

Status: **delivered on 2026-11-11**. The A/B/C reference corpora, unified review, scoring workflow, ScholarGuide matrix and reusable baseline instructions are present. Historical FB-1.0 files remain for reproducibility.

## Delivered artifacts

1. **Versioned data**
   - Track A: `corpus_v1.1/gold_video.jsonl`, 163 scored papers, S/A/B = 33/65/65.
   - Track B: `corpus_v1.1/gold_world_models.jsonl`, 128 scored papers, S/A/B = 26/51/51. Thirteen missing citation counts and 13 paper affiliations were reviewed; two official repositories were corrected.
   - Track C: `corpus_v1.1/gold_agents.jsonl`, 134 scored papers, S/A/B = 27/54/53.
2. **Shared review**
   - `review/final_adjudications_v1.1.json` contains 76 reviewed decisions.
   - All 62 distinct papers discovered outside Gold during the matrix were reviewed. Publication identity, year and venue were checked; impact used the frozen reference thresholds.
   - The review report records the conservative policies and provenance in `review/final_adjudication_report_v1.1.json`.
3. **Unified evaluation**
   - Common prompts, chat import, scoring, per-task repetition summaries and overall aggregation are exposed through `evaluation/frontierlitbench_cli.py`.
   - URL evidence can supplement DOI/arXiv identity without treating unrelated project and paper URLs as conflicting when DOI/arXiv already agree.
   - Usage and baseline instructions are in `EVALUATION_GUIDE.md`.
4. **ScholarGuide evaluation**
   - 15 tasks × 3 repetitions = 45 scored runs with DeepSeek Flash, method A, multiple sources and no cache.
   - Track macro nDCG@5: A `0.7316`, B `0.4735`, C `0.7113`; Overall `0.6388`.
   - Full per-task results are in `reports/scholarguide_matrix_20261111.md`.

## Final checks

- All 45 result files reached a final score with no positions pending adjudication.
- 44 runs completed normally; one FB-14 run exhausted its search budget after returning five papers and is scored under the documented bounded-search rule.
- The complete unified adjudication file is used for every system. Frozen Gold is never silently mutated.
- Historical `corpus/` and review files are retained. They are superseded for new comparisons by `corpus_v1.1/`.

## Limits

FB-1.1 is a reviewed research benchmark, not an exhaustive or objective census. Unmapped institutions use the documented conservative floor, absent attributable repositories receive zero repository evidence, and labels are single-primary-reviewer with retained evidence. Cross-system results must use the same corpus, adjudications, prompt and tool conditions. Missing papers in an incomplete search are not counted as evidence that those papers do not exist.
