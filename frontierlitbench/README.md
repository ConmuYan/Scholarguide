# FrontierLitBench track index

For task prompts, ScholarGuide runs, chat-product baselines and shared scoring, see the [evaluation guide](EVALUATION_GUIDE.md). New comparisons should use the reviewed [FB-1.1 corpora](corpus_v1.1), the [shared adjudications](review/final_adjudications_v1.1.json) and the [delivery report](reports/scholarguide_matrix_20261111.md).

| Track | Scope | Corpus | Status and review |
|---|---|---|---|
| A | Video generation and editing | [v1.1 video corpus](corpus_v1.1/gold_video.jsonl) | Reviewed; 163 papers, S/A/B = 33/65/65 |
| B | World models | [v1.1 world-model corpus](corpus_v1.1/gold_world_models.jsonl) | Reviewed; 128 papers, S/A/B = 26/51/51 |
| C | LLM agents and tools | [v1.1 agents corpus](corpus_v1.1/gold_agents.jsonl) | Reviewed; 134 papers, S/A/B = 27/54/53 |

Tracks cover different retrieval problems and have different corpus sizes. Track scores are reported separately; the equal-weight Overall score is a convenience aggregate, not evidence that the tracks are equally difficult. Historical [`corpus/`](corpus) remains for reproducibility and is not the v1.1 comparison baseline.

The matrix review found 62 distinct papers outside Gold. Publication identity, year, venue and tier are recorded for all of them in the shared adjudication table, so no offered recommendation from the evaluated matrix remains pending. Absence from Gold still does not prove that an unlisted paper is invalid; future systems need the same merge-and-review process before scoring.

# Track C review and pilot status

The reviewed v1.1 snapshot contains LLM agents, tool use, planning, memory, self-correction and multi-agent work, including relevant benchmarks and adjacent reasoning research. The original discovery and scoring evidence is retained in `corpus/features_agents.jsonl`.

Four clear scope mismatches were excluded: TraceRL (diffusion language model training), GauDP (embodied robot manipulation), HyperMARL (non-LLM multi-agent reinforcement learning), and Probabilistic Hierarchical Goal Network Planning (classical planning). `track_review` and `track_review_reason` preserve those decisions. Gold admission also excludes repositories flagged as shared monorepos: their total stars cannot be attributed to an individual paper. The remaining data were rescored using the existing protocol. This is a targeted review, not a complete independent verification of each paper, venue, repository, author affiliation, or ranking. Automatic repository and institution attribution can still contain errors. OpenAlex year disagreement may reflect a preprint; it is not itself grounds for exclusion.

This collection is suitable for versioned evaluation with the documented evidence policy. For the completed FB-1.1 matrix, every observed Gold-outside paper was reviewed and merged into the shared adjudication pool. Absence from the corpus is not proof that a recommendation is invalid; a new recommendation still needs review. The source's institution rubric is deliberately conservative and industrial institutions may be mapped less precisely than universities.

The five tasks and leaderboard are generated using `phase4_score_tier.py`, `phase5_eligibility.py`, and `export_leaderboard.py`. Do not rerun data collection over the reviewed features without preserving the review decisions.
