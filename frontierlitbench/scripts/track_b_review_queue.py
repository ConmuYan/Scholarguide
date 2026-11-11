"""List Track B records requiring evidence or a recalculation before release."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    records = [
        json.loads(line)
        for line in (ROOT / "corpus/gold_world_models.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    affiliations = {
        paper["paper_id"]: paper
        for paper in read_json(ROOT / "review/trackb_affiliation_evidence_20261111.json")["papers"]
    }
    repositories = {
        paper["paper_id"]: paper
        for paper in read_json(ROOT / "review/trackb_repository_additional_evidence_20261111.json")["papers"]
    }
    queue = []
    for record in records:
        impact = record["impact"]
        paper_id = record["paper_id"]
        issues = []
        if "absent_counted_zero" in impact.get("citation_status", ""):
            issues.append("citation_count_unobserved")
        if impact.get("institution_reason") == "verified_unmapped_conservative_floor":
            issues.append("institution_score_unmapped_default")
        if paper_id in repositories:
            issues.append("official_repository_found_after_snapshot")
        if issues:
            queue.append({
                "paper_id": paper_id,
                "title": record["title"],
                "year": record["year"],
                "issues": issues,
                "identifiers": record["identifiers"],
                "affiliation_evidence": affiliations.get(paper_id),
                "repository_evidence": repositories.get(paper_id),
            })
    print(json.dumps({
        "status": "not_ready_for_final_scoring",
        "affected_papers": len(queue),
        "issue_counts": {
            issue: sum(issue in item["issues"] for item in queue)
            for issue in (
                "citation_count_unobserved",
                "institution_score_unmapped_default",
                "official_repository_found_after_snapshot",
            )
        },
        "papers": queue,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
