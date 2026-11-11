# Track A retrieval pilot (2026-11-10)

This exploratory pilot retrieved papers through Lune for five Track A tasks,
using relevance order and citation order. It did not run the ScholarGuide
agent. The original recommendations and their matches are preserved in
`pilot_results_trackA.json`.

The previously reported metrics are withdrawn: unmatched recommendations were
assigned zero without adjudication. All affected metrics are now null until the
papers are reviewed against the same reference pool used for other systems.
Some historical recommendations also fall outside the task year constraints;
the pilot script has been corrected for future runs. These historical results
do not establish an advantage or disadvantage from any particular data source.
