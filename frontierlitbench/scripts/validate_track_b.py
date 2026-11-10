from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


candidates = read_jsonl(ROOT / "corpus" / "candidates_world_models.jsonl")
gold = read_jsonl(ROOT / "corpus" / "gold_world_models.jsonl")

assert len(candidates) == 150
assert len(gold) == 128
assert len({row["paper_id"] for row in candidates}) == len(candidates)
assert len({row["paper_id"] for row in gold}) == len(gold)
assert {row["paper_id"] for row in gold} <= {row["paper_id"] for row in candidates}

assert Counter(row["impact"]["tier"] for row in gold) == {"S": 26, "A": 51, "B": 51}
assert Counter(int(row["year"]) for row in gold) == {2024: 34, 2025: 47, 2026: 47}
assert all(row["venue"] in {"AAAI", "CVPR", "ICCV", "ICLR", "ICML", "NeurIPS"} for row in gold)
assert sorted(row["impact"]["track_rank"] for row in gold) == list(range(1, 129))

for row in gold:
    impact = row["impact"]
    expected = 0.55 * impact["citation_percentile"] + 0.30 * impact["github_percentile"] + 0.15 * impact["institution_score"]
    assert math.isclose(expected, impact["quality_score"], abs_tol=1e-8), (row["paper_id"], expected, impact["quality_score"])

tasks = json.loads((ROOT / "tasks" / "task_eligibility_world_models.json").read_text(encoding="utf-8"))
assert tasks["gold_total"] == 128
assert [task["valid_gold"] for task in tasks["tasks"]] == [128, 34, 47, 47, 65]
assert all(task["eligible"] for task in tasks["tasks"])

with (ROOT / "review" / "sample_for_human_review_world_models.csv").open("r", encoding="utf-8-sig", newline="") as handle:
    sample = list(csv.DictReader(handle))
assert len(sample) == 32
assert len({row["paper_id"] for row in sample}) == 32
assert Counter(row["tier"] for row in sample) == {"S": 6, "A": 13, "B": 13}

manifest = json.loads((ROOT / "manifests" / "corpus_sha256_world_models.json").read_text(encoding="utf-8"))
assert manifest["n_candidates"] == 150
assert manifest["n_gold"] == 128
for relative, expected in manifest["files"].items():
    actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
    assert actual == expected, relative

print("Track B validation passed: 150 candidates, 128 Gold, 5/5 eligible tasks, hashes verified.")
