# FrontierLitBench — Track A (Video Generation) data release

This directory holds an exploratory FrontierLitBench FB-1.0 snapshot for **Track A: Video Generation and Editing** (text/image-to-video, video diffusion & DiT, controllable editing, temporal consistency). It follows the same layout and conventions as the Track C collection (`*_agents` files).

**Corpus: v1.2.1, 198 scored Gold papers (S=40 / A=79 / B=79) + 18 excluded-per-protocol.** Candidate pool: 574 unique papers discovered via Lune MCP (whitelist venues CVPR/ICCV/ICLR/ICML/NeurIPS/ACM MM/SIGGRAPH, years 2024–2026), verified against OpenAlex. 18 further papers carry `institution_status=unverified_excluded` and are **not** part of the scored corpus, per protocol §4.4 (unverified required dimension → candidate/review only). Their titles remain in `corpus/gold_video.jsonl` with null scores so no information is silently lost.

Scoring is exactly the frozen FB-1.0 rule: **Q = 0.55·C + 0.30·G + 0.15·A**; C and G are midrank percentiles over log(1+x) within Track×Year strata (G over verified-official-repo stars only); tiers are round(0.2N)/round(0.4N)/rest by Q with paper-ID tiebreak. GitHub G uses the four-state frozen policy (official / none_available=0.0 / not_applicable=0.5 / unverified excluded). Institution A uses CSRankings 2021–2025 (areas CV+ML) for universities, and for industrial labs a quartile mapping over 2021–2025 Track-A-venue publication counts observed in OpenAlex (see `config/institution_rubric_video.json` for per-institution evidence, ROR and reviewer notes). 53 papers without work-level OpenAlex affiliations were scored using author-level evidence (first author's `last_known_institutions`), flagged per record.

Known limitations, same honesty bar as the Track C README:

- This is a working research dataset, not a claim of exhaustive verification.
- The 18 excluded papers await institution re-verification (OpenAlex rate-limit interrupted the run; resume caches exist).
- Automatic repo and institution attribution can still contain errors; a 25% bibliographic sample and a 20-paper repo spot-check queue are in `review/` for human sign-off.
- `arXiv twin` records (OpenAlex holds only the preprint) use the preprint citation count and are flagged in `labels/openalex_master_video.json`.
- Lune index gaps (ICCV 2025 only; no NeurIPS/ACM MM 2026) are recorded in `config/venues_video.json` — gaps in the index are not evidence of absent papers.
- FB-13 venue-subset is provisionally interpreted as CVPR.

Task eligibility: FB-01/04/05/06/13 all eligible (min cell has 43 valid papers ≥ 5). See `tasks/task_eligibility_video.json`.

Reproduction scripts are historical and incomplete; see `scripts/PIPELINE.md`. Policies and manual review remain pending; do not present this as a final benchmark.

Integrity: SHA-256 of `corpus/gold_video.jsonl` is pinned in `manifests/corpus_sha256_video.json`. Do not hand-edit the corpus without bumping `corpus_version` and the hash.
