# Offline engineering baseline

This is a small, repeatable product-regression baseline, not an expert-labelled literature benchmark. `cases.json` fixes ten existing test cases and a public offline paper fixture by SHA-256. Six cases are development checks; four are a transparent holdout, not used to tune the implementation. The holdout is visible and tiny, so it is not a blind or statistically representative evaluation.

Before and after changes, run with a new evidence path:

```sh
python evaluation/engineering_agent/run_baseline.py --split all --output runs/verification/NEW-DIRECTORY/engineering-baseline.json
```

For iterative development use `--split dev`; reserve the holdout for baseline and final verification. The runner refuses to run if either the saved source snapshot or the active demo fixture differs from the recorded digest. It refuses to overwrite existing evidence. Cases use offline fixtures or test doubles, not live sources or live model calls. Results record individual pass/fail/error/skip counts and elapsed time; token usage and cost are `null` because this is not a live billing measurement.

The fixed checks cover count and invalid-input constraints, explicit feedback, evidence attribution and fabrication, cancellation, recovery, and session rehydration. They do **not** yet establish correctness of native tool calling, unified governance, consented memory, MCP lifecycle, or conditional multi-agent orchestration. Add separate targeted tests and record their outcomes when those features exist, without silently changing this manifest or source snapshot.

For the later single-agent versus multi-agent comparison, predefine both variants and run the same tasks with the same source fixture, model or scripted model outputs, tool permissions, deadline and shared budget. Record each task's constraint and evidence checks, tool call count/validity, memory operations, failure/recovery, elapsed time, and observable token/cost data. Report missing usage as unknown. Do not turn a passing offline stub, model self-assessment, or a small sample into a claim of improved research quality; if the comparison is inconclusive or regresses, leave multi-agent use opt-in.

Historic `runs/verification/context-switch-final-20261029/summary.json` reports 872 tests and 120 Chrome tests for the earlier state. It is context, not evidence that the present or future source passes. The separate new `scripts/verify_delivery.py --require-browser --screenshots` run remains required at final acceptance.
