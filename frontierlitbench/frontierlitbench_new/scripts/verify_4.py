"""Step 4 refinement:
1) Proper venue cross-check (whitelist short name -> OpenAlex source keywords).
2) For Lune-vs-OpenAlex year mismatches, re-query by title without year filter
   and prefer the record whose source is the conference version (keeps
   cited_by_count on the formal publication where OpenAlex has it).
Outputs labels/verification_report.json.
"""
import json, time, urllib.parse, urllib.request

BASE = "https://api.openalex.org"

def get(path, params=None):
    url = BASE + path + "?" + urllib.parse.urlencode(params or {})
    req = urllib.request.Request(url, headers={"User-Agent": "frontierlitbench-prep"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception:
            if attempt == 2:
                return {}
            time.sleep(2 ** attempt)

VENUE_KEYWORDS = {
    "CVPR": ["computer vision and pattern recognition"],
    "ICCV": ["international conference on computer vision"],
    "ICLR": ["learning representations"],
    "ICML": ["international conference on machine learning"],
    "NeurIPS": ["neural information processing"],
    "ACM MM": ["multimedia"],
    "SIGGRAPH": ["siggraph", "computer graphics", "transactions on graphics"],
}

cands = {json.loads(l)["lune_id"]: json.loads(l) for l in open("corpus/candidates_video.jsonl", encoding="utf-8")}
enr = [json.loads(l) for l in open("labels/openalex_enrichment.jsonl", encoding="utf-8")]

report = {"total": len(enr), "matched": 0, "unmatched": [], "venue_mismatch": [],
          "year_mismatch_resolved": 0, "year_mismatch_unresolved": []}
fixed = {}  # lune_id -> replacement openalex data (conference-version record)

for e in enr:
    lid = e["lune_id"]
    c = cands[lid]
    d = e.get("data")
    if not d:
        report["unmatched"].append({"lune_id": lid, "title": c["title"], "venue": c["venue"], "year": c["year"]})
        continue
    report["matched"] += 1
    oa_src = (d.get("source_display_name") or "").lower()
    kws = VENUE_KEYWORDS.get(c["venue"], [])
    venue_ok = any(k in oa_src for k in kws)
    year_ok = d.get("publication_year") == c["year"]
    if not venue_ok:
        report["venue_mismatch"].append({"lune_id": lid, "title": c["title"][:80],
                                         "lune_venue": c["venue"], "oa_source": d.get("source_display_name"),
                                         "match": e.get("match")})
    if not year_ok and e.get("match") != "doi":
        # re-query without year filter; prefer conference-source record with matching year
        q = get("/works", {"filter": f'title.search:{c["title"]}', "per-page": 5})
        best = None
        for h in q.get("results", []):
            src = ((h.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
            src_l = src.lower()
            if h.get("publication_year") == c["year"] and any(k in src_l for k in kws):
                best = h; break
        if best:
            fixed[lid] = best
            report["year_mismatch_resolved"] += 1
        else:
            report["year_mismatch_unresolved"].append({"lune_id": lid, "title": c["title"][:80],
                                                       "lune": f'{c["venue"]} {c["year"]}',
                                                       "oa": f'{d.get("source_display_name")} {d.get("publication_year")}'})
    time.sleep(0.12)

json.dump(report, open("labels/verification_report.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
json.dump(fixed, open("labels/openalex_fixed_records.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"matched={report['matched']} unmatched={len(report['unmatched'])}")
print(f"venue_mismatch={len(report['venue_mismatch'])}")
for v in report["venue_mismatch"][:15]:
    print("  VM:", v["lune_venue"], "| OA:", (v["oa_source"] or "")[:60], "|", v["title"][:40])
print(f"year_mismatch resolved={report['year_mismatch_resolved']} unresolved={len(report['year_mismatch_unresolved'])}")
for v in report["year_mismatch_unresolved"][:15]:
    print("  YM:", v["lune"], "vs", v["oa"], "|", v["title"][:40])
