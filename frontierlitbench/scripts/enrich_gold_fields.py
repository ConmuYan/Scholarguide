"""QC optimization: enrich gold_video.jsonl with authors, abstract and
formatted citations (APA / IEEE / BibTeX) per the protocol JSON contract and
the course's per-paper record requirement. Re-freezes the corpus afterwards.
"""
import json, re, hashlib, datetime

gold = [json.loads(l) for l in open("corpus/gold_video.jsonl", encoding="utf-8")]
cands = {json.loads(l)["lune_id"]: json.loads(l) for l in open("corpus/candidates_video.jsonl", encoding="utf-8")}
oa = json.load(open("labels/openalex_master.json", encoding="utf-8"))

def fmt_authors(auths):
    names = [a for a in auths if a]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + ", & " + names[-1] if len(names) > 1 else names[0]

def apa(title, authors, year, venue):
    a = fmt_authors(authors)
    return f"{a} ({year}). {title}. In {venue}."

def ieee(title, authors, year, venue):
    return f'{", ".join(authors)}, "{title}," in {venue}, {year}.'

def bibtex(title, authors, year, venue, doi):
    key = re.sub(r"[^A-Za-z]", "", (authors[0].split()[-1] if authors else "anon")) + str(year) + re.sub(r"[^A-Za-z]", "", title)[:12]
    a = " and ".join(authors)
    b = f"@inproceedings{{{key},\n  title={{{title}}},\n  author={{{a}}},\n  year={{{year}}},\n  booktitle={{{venue}}}"
    if doi:
        b += f",\n  doi={{{doi}}}"
    return b + "\n}"

changed = 0
for p in gold:
    lid = p["lune_id"]
    cand = cands.get(lid, {})
    oa_data = (oa.get(lid) or {}).get("data") or {}
    authors = [a.get("author") for a in (oa_data.get("authorships") or []) if a.get("author")]
    if not authors:
        authors = cand.get("authors") or []
    p["authors"] = authors
    if not p.get("abstract") and cand.get("abstract"):
        p["abstract"] = cand["abstract"]
    p["citations_fmt"] = {
        "apa": apa(p["title"], authors, p["year"], p["venue"]),
        "ieee": ieee(p["title"], authors, p["year"], p["venue"]),
        "bibtex": bibtex(p["title"], authors, p["year"], p["venue"], p.get("doi")),
    }
    p.setdefault("abstract", None)
    changed += 1

with open("corpus/gold_video.jsonl", "w", encoding="utf-8") as f:
    for p in gold:
        f.write(json.dumps(p, ensure_ascii=False) + "\n")

sha = hashlib.sha256(open("corpus/gold_video.jsonl", "rb").read()).hexdigest()
manifest = json.load(open("manifests/corpus_sha256.json", encoding="utf-8"))
manifest["sha256"] = sha
manifest["corpus_version"] = "trackA-video-gold-v1.1-2026-11-05"
manifest["enriched"] = "authors + abstract + APA/IEEE/BibTeX added (QC pass)"
manifest["frozen_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
json.dump(manifest, open("manifests/corpus_sha256.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"enriched {changed} papers; new sha={sha[:16]}...; version=v1.1")
