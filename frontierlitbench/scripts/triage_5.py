"""Step 5: first-pass relevance triage + boundary flags for Track A.

Rule-based scoring on title+abstract. Buckets:
  likely_in_scope    - clearly video generation/editing within a frozen sub-direction
  needs_review       - borderline (restoration, 4D/3D-dynamic, world-model-ish, etc.)
  likely_out_of_scope- understanding/analysis of video, non-video topics
This is a TRIAGE AID ONLY; protocol requires human judgment for final inclusion.
"""
import json, re

cands = [json.loads(l) for l in open("corpus/candidates_video.jsonl", encoding="utf-8")]
master = json.load(open("labels/openalex_master.json", encoding="utf-8"))

OUT_SIGNALS = [
    r"video (action |anomaly )?(recognition|classification)", r"\bobject detection\b",
    r"video (object )?detection", r"segmentation", r"\btracking\b", r"video caption",
    r"video question answer", r"video(-| )?text retrieval", r"video retrieval",
    r"video summarization", r"compress", r"codec", r"streaming", r"conference",
    r"quality assessment", r"forgery detection", r"deepfake detection", r"video understanding",
    r"highlight detection", r"temporal action localization", r" violence", r"surveillance",
    r"person re-identification", r"gait recognition", r"emotion recognition", r"pose estimation",
    r"3d (human )?(pose|mesh|reconstruction from (image|single))",
]
REVIEW_SIGNALS = [
    r"super-resolution", r"frame interpolation", r"deblur", r"denois", r"enhancement",
    r"restoration", r"inpainting(?!.*(remov|insert))", r"4d", r"3d gaussian.*(animat|dynamic|scene)",
    r"world model", r"driving (scene|video|simulat)", r"autonomous", r"robot",
    r"image generation", r"text-to-image", r"talking", r"lip ?sync", r"portrait",
    r"neural (radiance|rendering|field)", r"avatar", r"video prediction",
]
IN_SIGNALS = [
    r"video generation", r"text-to-video", r"image-to-video", r"video diffusion", r"video edit",
    r"temporal consistency", r"video synthesis", r"video model", r"di(t|ffusion transformer)",
    r"flow matching.*video", r"consistency model.*video", r"video tokeniz", r"long video",
    r"frame consistency", r"flicker",
]

def classify(text):
    t = text.lower()
    out_hits = [p for p in OUT_SIGNALS if re.search(p, t)]
    in_hits = [p for p in IN_SIGNALS if re.search(p, t)]
    rev_hits = [p for p in REVIEW_SIGNALS if re.search(p, t)]
    if out_hits and not in_hits:
        return "likely_out_of_scope", out_hits, in_hits
    if in_hits and not (rev_hits or out_hits):
        return "likely_in_scope", out_hits, in_hits
    return "needs_review", out_hits, in_hits

rows = []
for c in cands:
    text = (c["title"] or "") + " || " + (c["abstract"] or "")[:600]
    bucket, oh, ih = classify(text)
    oa = master.get(c["lune_id"], {})
    data = oa.get("data") or {}
    rows.append({
        "lune_id": c["lune_id"], "title": c["title"], "venue": c["venue"], "year": c["year"],
        "bucket": bucket,
        "subdirection_hints": sorted(c["provenance"]["subdirections"]),
        "has_doi": bool(c.get("doi")),
        "openalex_status": oa.get("status"),
        "oa_cited_by": data.get("cited_by_count"),
        "first_author_insts": (data.get("authorships") or [{}])[0].get("institutions") if data.get("authorships") else None,
    })

from collections import Counter
print(Counter(r["bucket"] for r in rows))
# cross: bucket x venue-year pulls
src = Counter()
for c, r in zip(cands, rows):
    src[(("supp" if any(s.startswith("search_papers_supp") for s in c["provenance"]["sources"]) else "many"), r["bucket"])] += 1
print(src)
with open("labels/triage_video.jsonl", "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
print("written labels/triage_video.jsonl")
# sample of needs_review
for r in [x for x in rows if x["bucket"] == "needs_review"][:12]:
    print("  REVIEW:", r["venue"], r["year"], (r["title"] or "")[:65])
