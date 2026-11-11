# FrontierLitBench track index

For task prompts, ScholarGuide runs, chat-product baselines and shared scoring, see the [evaluation guide](EVALUATION_GUIDE.md). Current reference datasets and results remain under review.

| Track | Scope | Corpus | Status and review |
|---|---|---|---|
| A | Video generation and editing | [Video corpus](corpus/gold_video.jsonl) | [Track A notes](README_TrackA.md), [open issues](review/open_issues_video.md) |
| B | World models | [World Model corpus](corpus/gold_world_models.jsonl) | [Track B notes](README_TrackB.md), [open issues](review/open_issues_world_models.md) |
| C | LLM agents and tools | [Agents corpus](corpus/gold_agents.jsonl) | [Track C review](#track-c-review-and-pilot-status), [open issues](review/open_issues.md) |

Tracks are exploratory snapshots with separate evidence, scores, eligibility and pending reviews. Cross-track scores are not directly comparable.

# Track C review and pilot status

This is a working research dataset, not a claim of exhaustive verification or a completed benchmark release. It contains LLM agents, tool use, planning, memory, self-correction and multi-agent work, including relevant benchmarks and adjacent reasoning research. The original discovery and scoring evidence is retained in `corpus/features_agents.jsonl`.

Four clear scope mismatches were excluded: TraceRL (diffusion language model training), GauDP (embodied robot manipulation), HyperMARL (non-LLM multi-agent reinforcement learning), and Probabilistic Hierarchical Goal Network Planning (classical planning). `track_review` and `track_review_reason` preserve those decisions. Gold admission also excludes repositories flagged as shared monorepos: their total stars cannot be attributed to an individual paper. The remaining data were rescored using the existing protocol. This is a targeted review, not a complete independent verification of each paper, venue, repository, author affiliation, or ranking. Automatic repository and institution attribution can still contain errors. OpenAlex year disagreement may reflect a preprint; it is not itself grounds for exclusion.

This collection is suitable for exploratory evaluation with these limitations disclosed. Gold-outside results must be reviewed before assigning final scores; absence from this corpus is not proof that a recommendation is invalid. The source's institution rubric is provisional and industrial institutions may be misclassified. Avoid presenting pilot results as a final leaderboard.

The five tasks and leaderboard are generated using `phase4_score_tier.py`, `phase5_eligibility.py`, and `export_leaderboard.py`. Do not rerun data collection over the reviewed features without preserving the review decisions.
