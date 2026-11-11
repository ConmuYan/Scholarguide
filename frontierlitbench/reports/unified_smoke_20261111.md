# Unified evaluation smoke test — 2026-11-11

These are single-run engineering checks, not benchmark leaderboard results. All use ScholarGuide method A, DeepSeek `deepseek-flash`, `multi` configured sources, disabled cache, and the common task prompt. The snapshots and supplemental decisions remain provisional.

| Task | Run outcome | Scoring status |
|---|---|---|
| FB-13 / A | completed, five recommendations | one Gold match, one adjudicated outside Track A scope, three pending adjudication; metrics unavailable |
| FB-02 / B | search budget exhausted, four recommendations | two Gold/supplement matches, two pending; one missing position; metrics unavailable |
| FB-14 / B | completed, five recommendations | four Gold matches and one provisional supplemental match; nDCG@5 0.4213, WeightedHit@5 36%, S-Tier Hit@5 0 |
| FB-15 / C | completed, five recommendations | one Gold match; four pending adjudication; metrics unavailable |

For FB-14, the provisional gain sequence is A, A, B, B, B. The single added B-tier paper is documented in `review/trackb_supplemental_adjudications.json`; its source features were observed after the initial Track B freeze. The present Track B reference also contains citation and affiliation uncertainties, so the numeric result must not be presented as a final ranking.

Before the OpenReview verification fix, an FB-14 run returned only one recommendation although Lune had returned relevant candidates. After the fix, a separate run returned five; this shows the observed acceptance-evidence bottleneck was addressed for that run, but the two runs are not a controlled statistical comparison. Lune returned ten results in each search round of the earlier run and ten in the successful run. DBLP encountered a bot challenge. Raw results and score files are saved locally under `runs/frontier-fb14-unified-3`, `runs/frontier-fb14-unified-4`, and the three `runs/frontier-fb*-unified-smoke-1` directories.
