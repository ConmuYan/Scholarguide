"""Build CSRankings-based institution tiers for Track A (simplified per plan v1).

Areas = vision + mlmining (Track A: CV + ML), window 2021-2025, sum adjustedcount
per dept, rank. Tiers: 1-25 -> 1.00, 26-50 -> 0.80, 51-100 -> 0.60, rest -> 0.40.
Also saves the distinct dept list for matching OpenAlex institution names.
"""
import csv, json
from collections import defaultdict

ADJ = defaultdict(float)
depts = set()
with open("generated-author-info.csv", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        try:
            year = int(row["year"])
        except ValueError:
            continue
        if not (2021 <= year <= 2025):
            continue
        if row["area"] not in ("cvpr", "iccv", "eccv", "icml", "nips", "iclr"):
            continue
        d = row["dept"].strip()
        depts.add(d)
        try:
            ADJ[d] += float(row["adjustedcount"])
        except ValueError:
            pass

ranking = sorted(ADJ.items(), key=lambda kv: -kv[1])
out = [{"rank": i + 1, "dept": d, "adjustedcount_cv_ml_2021_2025": round(v, 2),
        "tier_score": 1.00 if i < 25 else (0.80 if i < 50 else (0.60 if i < 100 else 0.40))}
       for i, (d, v) in enumerate(ranking)]
json.dump(out, open("labels/institution_tiers_csrankings.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
json.dump(sorted(depts), open("labels/csrankings_depts.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("institutions ranked:", len(out))
for r in out[:10]:
    print(f'  #{r["rank"]} {r["dept"]}: {r["adjustedcount_cv_ml_2021_2025"]} -> {r["tier_score"]}')
print("  ...")
for r in out[100:103]:
    print(f'  #{r["rank"]} {r["dept"]}: {r["adjustedcount_cv_ml_2021_2025"]} -> {r["tier_score"]}')
