"""Step 3: merge raw collections, dedupe by Lune paper_id, normalize into
corpus/candidates_video.jsonl. Provenance (which queries hit each paper,
which sub-direction angles) is preserved for auditability.
"""
import json, glob, datetime

WHITELIST = {"CVPR", "ICCV", "ICLR", "ICML", "NeurIPS", "ACM MM", "SIGGRAPH"}
SUBDIR_FILES = {
    "t2v_i2v": "1. text/image-to-video",
    "video_diffusion_dit": "2. video diffusion & DiT",
    "controllable_editing": "3. controllable editing",
    "temporal_consistency": "4. temporal consistency",
}

papers = {}  # lune paper_id -> record

def add(p, source, subdirection=None, query=None):
    pid = p.get("paper_id")
    if not pid:
        return
    rec = papers.setdefault(pid, {
        "lune_id": pid,
        "title": p.get("title"),
        "authors": p.get("authors", []),
        "year": p.get("year"),
        "venue": p.get("conference"),
        "doi": p.get("doi"),
        "abstract": p.get("abstract"),
        "pdf_cdn_url": p.get("pdf_cdn_url"),
        "lune_citation_count": p.get("citation_count"),
        "provenance": {"sources": set(), "subdirections": set(), "queries": set()},
        "screening": {"whitelist_venue_ok": p.get("conference") in WHITELIST,
                      "year_ok": p.get("year") in (2024, 2025, 2026)},
    })
    rec["provenance"]["sources"].add(source)
    if subdirection:
        rec["provenance"]["subdirections"].add(subdirection)
    if query:
        rec["provenance"]["queries"].add(query)

# step 2a
for fn in glob.glob("corpus/raw/many_*.json"):
    sub = SUBDIR_FILES[fn.split("many_")[1].split(".json")[0]]
    r = json.load(open(fn, encoding="utf-8"))
    for p in r.get("results", []):
        add(p, "search_papers_many", subdirection=sub)

# step 2b
for fn in glob.glob("corpus/raw/supp_*.json"):
    d = json.load(open(fn, encoding="utf-8"))
    for p in d["response"].get("results", []):
        add(p, "search_papers_supplement", query=d["query"])

# serialize
out = []
for rec in papers.values():
    rec["provenance"]["sources"] = sorted(rec["provenance"]["sources"])
    rec["provenance"]["subdirections"] = sorted(rec["provenance"]["subdirections"])
    rec["provenance"]["queries"] = sorted(rec["provenance"]["queries"])
    out.append(rec)

out.sort(key=lambda r: (r["venue"] or "", r["year"] or 0, r["title"] or ""))
with open("corpus/candidates_video.jsonl", "w", encoding="utf-8") as f:
    for r in out:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")

# stats
cells = {}
for r in out:
    cells[(r["venue"], r["year"])] = cells.get((r["venue"], r["year"]), 0) + 1
print("unique candidates:", len(out))
print(f'{"venue":10} 2024 2025 2026')
for cv in ["CVPR", "ICCV", "ICLR", "ICML", "NeurIPS", "ACM MM", "SIGGRAPH"]:
    row = [cells.get((cv, y), 0) for y in (2024, 2025, 2026)]
    print(f"{cv:10} {row[0]:4} {row[1]:4} {row[2]:4}")
nonwl = [r for r in out if not r["screening"]["whitelist_venue_ok"]]
print("non-whitelist venue records:", len(nonwl))
