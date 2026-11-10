"""Step 4 round 2:
- For venue-mismatch records pointing at arXiv twins, try to relocate the
  formal-publication OpenAlex record (same year + venue keyword in source).
- Re-try the 34 unmatched with a shorter title query.
Emits labels/openalex_master.json (lune_id -> best openalex record + status).
"""
import json, os, time, urllib.parse, urllib.request

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
    "ICML": ["international conference on machine learning", "machine learning research"],
    "NeurIPS": ["neural information processing"],
    "ACM MM": ["multimedia"],
    "SIGGRAPH": ["siggraph", "computer graphics", "transactions on graphics"],
}

def extract(w):
    authorships = []
    for a in w.get("authorships", []):
        insts = [i.get("display_name") for i in a.get("institutions", []) if i.get("display_name")]
        authorships.append({"author": (a.get("author") or {}).get("display_name"),
                            "institutions": insts,
                            "is_corresponding": bool(a.get("is_corresponding"))})
    src = ((w.get("primary_location") or {}).get("source") or {})
    return {"openalex_id": w["id"], "doi": w.get("doi"), "title": w.get("title"),
            "publication_year": w.get("publication_year"), "cited_by_count": w.get("cited_by_count"),
            "source_display_name": src.get("display_name"), "openalex_url": w.get("id"),
            "authorships": authorships}

cands = {json.loads(l)["lune_id"]: json.loads(l) for l in open("corpus/candidates_video.jsonl", encoding="utf-8")}
enr = {json.loads(l)["lune_id"]: json.loads(l) for l in open("labels/openalex_enrichment.jsonl", encoding="utf-8")}
rep = json.load(open("labels/verification_report.json", encoding="utf-8"))

PROG = "labels/fix_round2_progress.json"
master = json.load(open(PROG, encoding="utf-8")) if os.path.exists(PROG) else {}
print(f"resume: {len(master)} already done")

def save():
    json.dump(master, open(PROG, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

n_reloc = n_rematch = 0

# 1) relocate arXiv-twin venue mismatches
for v in rep["venue_mismatch"]:
    lid = v["lune_id"]
    if lid in master:
        continue
    c = cands[lid]
    kws = VENUE_KEYWORDS.get(c["venue"], [])
    q = get("/works", {"filter": f'title.search:{c["title"]}', "per-page": 8})
    best = None
    for h in q.get("results", []):
        src = (((h.get("primary_location") or {}).get("source") or {}).get("display_name") or "").lower()
        if h.get("publication_year") == c["year"] and any(k in src for k in kws):
            best = h; break
    if best:
        master[lid] = {"status": "openalex_verified_formal", "match": "relocated", "data": extract(best)}
        n_reloc += 1
    else:
        master[lid] = {"status": "openalex_arxiv_twin", "match": (enr[lid].get("match")), "data": enr[lid]["data"]}
    time.sleep(0.12)
    save()

# 2) retry unmatched with short title
for u in rep["unmatched"]:
    lid = u["lune_id"]
    if lid in master:
        continue
    c = cands[lid]
    short = " ".join(c["title"].split()[:5])
    q = get("/works", {"filter": f'title.search:{short}', "per-page": 5})
    best = None
    kws = VENUE_KEYWORDS.get(c["venue"], [])
    for h in q.get("results", []):
        src = (((h.get("primary_location") or {}).get("source") or {}).get("display_name") or "").lower()
        t = (h.get("title") or "").strip().lower()
        if t == c["title"].strip().lower() or (h.get("publication_year") == c["year"] and any(k in src for k in kws)):
            best = h; break
    if best:
        src_l = (((best.get("primary_location") or {}).get("source") or {}).get("display_name") or "").lower()
        st = "openalex_verified_formal" if any(k in src_l for k in kws) else "openalex_matched_other_source"
        master[lid] = {"status": st, "match": "retried", "data": extract(best)}
        n_rematch += 1
    else:
        master[lid] = {"status": "openalex_not_found", "match": None, "data": None}
    time.sleep(0.12)
    save()

# 3) everything else keeps original record
from collections import Counter
orig_status = Counter()
for lid, e in enr.items():
    if lid in master:
        continue
    d = e.get("data")
    if not d:
        master[lid] = {"status": "openalex_not_found", "match": e.get("match"), "data": None}
        continue
    c = cands[lid]
    kws = VENUE_KEYWORDS.get(c["venue"], [])
    src = (d.get("source_display_name") or "").lower()
    if any(k in src for k in kws) and d.get("publication_year") == c["year"]:
        master[lid] = {"status": "openalex_verified_formal", "match": e.get("match"), "data": d}
    else:
        master[lid] = {"status": "openalex_arxiv_twin", "match": e.get("match"), "data": d}
    orig_status[master[lid]["status"]] += 1

json.dump(master, open("labels/openalex_master.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"relocated={n_reloc} retried_matched={n_rematch}")
print("final statuses:", Counter(m["status"] for m in master.values()))
