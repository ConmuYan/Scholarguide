# FrontierLitBench - Track B World Model data release

This directory contains the FB-1.0 snapshot for **Track B: World Model**: latent dynamics, action-conditioned prediction, environment simulation, video world models, autonomous driving, and embodied or robot planning.

**Corpus: 128 frozen Gold papers (S=26 / A=51 / B=51). Candidate pool: 150 papers from the 2024-2026 venue whitelist.** The five Track B tasks FB-02/07/08/09/14 are eligible; the smallest year cell contains 34 Gold papers.

Scoring follows the frozen FB-1.0 rule **Q = 0.55*C + 0.30*G + 0.15*A**. C and G use Track x Year midrank percentiles; tiers use S=round(0.20N), A=round(0.40N), B=rest, with paper-ID ascending as the stable tiebreaker.

## Snapshot summary

- Years: 2024=34, 2025=47, 2026=47.
- Venues: ICML=35, NeurIPS=31, ICLR=30, CVPR=20, ICCV=6, AAAI=6.
- Repository states: official=9, none_available=119.
- Citation snapshot: 2026-11-08. GitHub stars snapshot: 2026-11-08.
- Corpus version: `FB-1.0-world-2026-11-09-single-reviewer`.

## Review and limitations

This is a working research dataset, not a claim of exhaustive verification. The corpus was frozen under a primary single-reviewer policy and independent cross-review was waived. Negative repository outcomes, unresolved OpenAlex citation counts, and conservative institution floors are disclosed in `review/open_issues_world_models.md`; a deterministic 25% sample is retained in `review/sample_for_human_review_world_models.csv`.

ScholarGuide search runs and retrieval metrics are downstream evaluation artifacts and are not included in this data-construction PR. Integrity hashes are pinned in `manifests/corpus_sha256_world_models.json`; do not hand-edit frozen files without bumping the corpus version and hashes.
