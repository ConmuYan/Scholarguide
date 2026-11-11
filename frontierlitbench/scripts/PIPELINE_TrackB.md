# Track B World Model pipeline

| Phase | Input / operation | Frozen output |
|---|---|---|
| 0 snapshot | Lune conference inventory | `snapshots/lune_conferences_20261108_world_models.json` |
| 1 discover | Lune topic queries, 2024-2026 venue whitelist | `corpus/candidates_world_models.jsonl` |
| 2 direction review | normalized-title deduplication and World Model scope screening | `corpus/features_world_models.jsonl` |
| 3 features | OpenAlex citations, official GitHub repository evidence, institution rubric | `labels/*_world_models.*` |
| 4 score and tier | Q=0.55C+0.30G+0.15A; fixed tier quotas | `corpus/gold_world_models.jsonl` |
| 5 eligibility | five FB-1.0 task slices | `tasks/task_eligibility_world_models.json` |
| 6 review and freeze | audit log, 25% review sample, sensitivity, SHA-256 | `review/`, `reports/`, `manifests/` |

The historical collection depended on local credentials and cached source responses. The committed files preserve the frozen evidence and derived outputs; they are not represented as a credential-free end-to-end reproduction pipeline.
