# FrontierLitBench - Track B World Model data release

> Historical FB-1.0 data record. New evaluations use [`corpus_v1.1/gold_world_models.jsonl`](corpus_v1.1/gold_world_models.jsonl), which contains the reviewed citation, affiliation, repository and recomputed tier corrections (128 papers, S/A/B = 26/51/51).

This directory contains the FB-1.0 snapshot for **Track B: World Model**: latent dynamics, action-conditioned prediction, environment simulation, video world models, autonomous driving, and embodied or robot planning.

**Historical corpus: 128 papers (S=26 / A=51 / B=51). Candidate pool: 150 papers from the 2024-2026 venue whitelist.** The five Track B tasks FB-02/07/08/09/14 can be run against this snapshot for exploratory evaluation, but the labels are provisional and should not be reported as final benchmark results until the issues below are resolved. The smallest year cell contains 34 papers.

Scoring follows **Q = 0.55*C + 0.30*G + 0.15*A**. C and G use Track x Year midrank percentiles; tiers use S=round(0.20N), A=round(0.40N), B=rest, with paper-ID ascending as the stable tiebreaker.

## Snapshot summary

- Years: 2024=34, 2025=47, 2026=47.
- Venues: ICML=35, NeurIPS=31, ICLR=30, CVPR=20, ICCV=6, AAAI=6.
- Repository states: official=65, none_available=63.
- Official repositories by year: 2024=17, 2025=24, 2026=24.
- Citation snapshot: 2026-11-08. GitHub stars snapshot: 2026-11-11.
- Corpus version: `FB-1.0-world-2026-11-11-repo-corrected-single-reviewer`.

## Review and limitations

This is a working research dataset. Thirteen citation counts were not observed, 54 institution scores used an unmapped default, and two additional official repositories were found after the historical re-audit. These affect the computed tiers, including potentially other papers' tiers. Details and the remaining work are in `review/open_issues_world_models.md`. Generate the paper-level review queue with `python frontierlitbench/scripts/track_b_review_queue.py`.

ScholarGuide search runs and retrieval metrics are downstream evaluation artifacts. Preserve the historical snapshot when producing corrected labels, so earlier exploratory runs remain interpretable.
