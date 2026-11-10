"""Step 4+6a+6c-part1: OpenAlex enrichment for all candidates.

Per paper: existence verification, canonical venue/year cross-check,
cited_by_count (the protocol's single citation source), and raw author
institutions (evidence for the institution rubric). Polite pool via mailto.
"""
import json, time, urllib.parse, urllib.request, os

# OpenAlex polite-pool email. Left blank: API still works (lower rate limit).
# Fill in a real email before any re-run (ask the owner which to use).
MAILTO = ""
BASE = "https://api.openalex.org"

def get(path, params=None):
    params = dict(params or {})
    if MAILTO:
        params["mailto"] = MAILTO
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "frontierlitbench-prep"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if attempt == 2:
                return {"_error": str(e)}
            time.sleep(2 ** attempt)

def extract(w):
    if not w or "id" not in w:
        return None
    authorships = []
    for a in w.get("authorships", []):
        insts = [i.get("display_name") for i in a.get("institutions", []) if i.get("display_name")]
        authorships.append({"author": (a.get("author") or {}).get("display_name"),
                            "institutions": insts,
                            "is_corresponding": bool(a.get("is_corresponding"))})
    loc = w.get("primary_location") or {}
    src = (loc.get("source") or {})
    return {
        "openalex_id": w["id"],
        "doi": w.get("doi"),
        "title": w.get("title"),
        "publication_year": w.get("publication_year"),
        "cited_by_count": w.get("cited_by_count"),
        "source_display_name": src.get("display_name"),
        "openalex_url": w.get("id"),
        "authorships": authorships,
        "corresponding_institutions_confirmed": any(a["is_corresponding"] for a in authorships),
    }

os.makedirs("labels", exist_ok=True)
done_ids = set()
if os.path.exists("labels/openalex_enrichment.jsonl"):
    for l in open("labels/openalex_enrichment.jsonl", encoding="utf-8"):
        try:
            done_ids.add(json.loads(l)["lune_id"])
        except Exception:
            pass
cands = [json.loads(l) for l in open("corpus/candidates_video.jsonl", encoding="utf-8")]
cands = [c for c in cands if c["lune_id"] not in done_ids]
print(f"resume: {len(done_ids)} done, {len(cands)} remaining")
out = open("labels/openalex_enrichment.jsonl", "a", encoding="utf-8")
n_ok = n_err = n_miss = 0
for i, c in enumerate(cands):
    rec = {"lune_id": c["lune_id"], "queried_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    doi = c.get("doi")
    if doi:
        w = get("/works/doi:" + urllib.parse.quote(doi, safe=""))
        if w and "id" in w:
            rec["match"] = "doi"; rec["data"] = extract(w)
        else:
            w = None
    else:
        w = None
    if w is None:
        # fallback: title search, pick first same-year candidate
        q = get("/works", {"filter": f"title.search:{c['title']},publication_year:{c['year']}", "per-page": 3})
        hits = (q or {}).get("results", []) if isinstance(q, dict) else []
        pick = None
        for h in hits:
            if (h.get("title") or "").strip().lower() == (c["title"] or "").strip().lower():
                pick = h; break
        if pick is None and hits:
            pick = hits[0]
            rec["match"] = "title_fuzzy"
        elif pick is not None:
            rec["match"] = "title_exact"
        if pick is not None:
            rec["data"] = extract(pick)
        else:
            rec["match"] = "none"; n_miss += 1
    if rec.get("data"):
        n_ok += 1
    elif rec.get("match") == "none":
        pass
    else:
        n_err += 1
    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if (i + 1) % 50 == 0:
        print(f"{i+1}/{len(cands)} ok={n_ok} miss={n_err + n_miss}", flush=True)
    time.sleep(0.12)
out.close()
print(f"DONE ok={n_ok} none={n_miss} err={n_err} / {len(cands)}")
