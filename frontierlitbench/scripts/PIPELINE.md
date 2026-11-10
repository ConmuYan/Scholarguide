# Track A pipeline scripts

Mirrors the phase structure used for Track C, executed 2026-11-05 ~ 2026-11-08.

| Phase | Scripts | Output |
|---|---|---|
| 0 snapshot | `lune_client.py` (`list_conferences`) | `snapshots/lune_conferences_20261110.json`, `config/venues_video.json` |
| 1 discover | `collect_2a.py` / `collect_2b.py` | `corpus/raw/*.json` (staged under repo root `corpus/raw` locally; raw Lune responses retained in workspace), candidates |
| 2 dedup + verify | `merge_3.py`, `verify_4.py`, `enrich_openalex.py`, `fix_round2.py` | `corpus/candidates_video.jsonl`, `labels/openalex_master_video.json` |
| 3 features | `triage_5.py`, `github_discovery.py`, `repo_verify.py`, `institution_tiers.py`, `institution_rescore_v2*.py` | `labels/triage_video.jsonl`, `labels/repo_verification_video.jsonl`, `config/institution_rubric_video.json` |
| 4 score + tier | `score_and_freeze.py`, `apply_rescore_v2.py` | `corpus/gold_video.jsonl` (v1.2), `labels/tier_thresholds_video.json` |
| 5 eligibility | eligibility check (inline in scoring pipeline) | `tasks/task_eligibility_video.json` |
| 6 review | review sampling + workbook | `review/*` |

Some scripts use local caches and credentials from `keys.local.env` (never committed). CSRankings raw input is
`generated-author-info.csv` from csrankings.org (redistribution-restricted — not committed).

## Reproducibility limitations

These are historical research scripts, not a verified end-to-end reproduction pipeline. Some expect untracked raw responses, caches, local credentials, or intermediate files under their original names. Several write relative to the current working directory and can overwrite outputs; do not run them over the frozen corpus. Resume support has not been independently verified. Reconstruct and validate in an isolated directory before use.
