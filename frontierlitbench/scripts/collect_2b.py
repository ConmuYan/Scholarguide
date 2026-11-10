"""Step 2b: supplementary collection for thin venue x year strata.

One search_papers call per (venue, year, angle). Documents strata that Lune
does not index (e.g. NeurIPS/ACM MM 2026) as evidence rather than assuming
papers do not exist.
"""
import json, os, time, datetime
from lune_client import LuneClient

VENUES = ["CVPR", "ICCV", "ICLR", "ICML", "NeurIPS", "ACM MM", "SIGGRAPH"]

# (conference, year, query angle) - thin cells from step-2a stratum table
CALLS = [
    ("ACM MM", 2025, "video generation and video editing with diffusion models"),
    ("ACM MM", 2024, "video generation and video editing with diffusion models"),
    ("ACM MM", 2026, "video generation and video editing"),
    ("NeurIPS", 2025, "text-to-video and image-to-video generation models"),
    ("NeurIPS", 2025, "controllable video editing and temporal consistency"),
    ("NeurIPS", 2026, "video generation and editing"),
    ("NeurIPS", 2024, "video diffusion and generation models"),
    ("NeurIPS", 2024, "video editing and temporal consistency methods"),
    ("ICML", 2024, "video generation with diffusion models"),
    ("SIGGRAPH", 2024, "video generation and neural video synthesis"),
    ("SIGGRAPH", 2026, "video generation and neural video synthesis"),
]

os.makedirs("corpus/raw", exist_ok=True)
c = LuneClient()
meta = {"retrieved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "calls": []}

for conf, year, q in CALLS:
    r = c.call("search_papers", query=q, conference=conf, year=year, limit=50, detail=True)
    fn = f"corpus/raw/supp_{conf.replace(' ','')}_{year}.json"
    # keep multiple angles for same stratum separate
    if os.path.exists(fn):
        fn = f"corpus/raw/supp_{conf.replace(' ','')}_{year}_b.json"
    json.dump({"query": q, "conference": conf, "year": year, "response": r},
              open(fn, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    n = len(r.get("results", [])) if isinstance(r, dict) else 0
    meta["calls"].append({"conference": conf, "year": year, "query": q, "n_results": n, "raw_file": fn})
    print(f"{conf} {year}: {n} results | {q[:60]}")
    time.sleep(2)

json.dump(meta, open("corpus/raw/collection_meta_2b.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print("done")
