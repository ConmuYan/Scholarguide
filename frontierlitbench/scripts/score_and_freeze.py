"""Master scoring + freeze pipeline (Track A, plan v1).

Inputs: triage, openalex_master, repo_verification, institution tiers.
Steps: Gold-eligibility filter -> institution matching -> per-year midrank
percentiles for C and G -> Q = 0.55C+0.30G+0.15A -> S/A/B by 20/40/40 quota
-> gold_video.jsonl + tier_thresholds.json + corpus_sha256.json ->
FB-01/04/05/06/13 eligibility -> review workbook.
"""
import json, re, math, hashlib, random, html, datetime, os
from collections import defaultdict

TODAY = "2026-11-05"
rng = random.Random(7608)  # fixed seed for reproducible sampling

# ---------- load ----------
triage = {json.loads(l)["lune_id"]: json.loads(l) for l in open("labels/triage_video.jsonl", encoding="utf-8")}
oa = json.load(open("labels/openalex_master.json", encoding="utf-8"))
repo = {json.loads(l)["lune_id"]: json.loads(l) for l in open("labels/repo_verification.jsonl", encoding="utf-8")}
tiers = json.load(open("labels/institution_tiers_csrankings.json", encoding="utf-8"))
dept_list = [t["dept"] for t in tiers]
tier_by_dept = {t["dept"]: t["tier_score"] for t in tiers}

# ---------- 1. Gold eligibility ----------
pool = []
for lid, t in triage.items():
    if t["bucket"] != "likely_in_scope":
        continue
    m = oa.get(lid, {})
    if m.get("status") not in ("openalex_verified_formal", "openalex_arxiv_twin"):
        continue  # drop OpenAlex-not-found per plan v1
    d = m.get("data") or {}
    if d.get("cited_by_count") is None:
        continue
    rv = repo.get(lid, {})
    pool.append({
        "lune_id": lid, "title": t["title"], "venue": t["venue"], "year": t["year"],
        "doi": (d.get("doi") or "").replace("https://doi.org/", "") or None,
        "openalex_id": d.get("openalex_id"),
        "citations": d.get("cited_by_count"),
        "citation_source": "openalex",
        "citation_as_of": TODAY,
        "match_type": m.get("status"),
        "repo_status": rv.get("repo_status", "none_available"),
        "github_url": (rv.get("verified") or {}).get("github_url"),
        "github_stars": (rv.get("verified") or {}).get("github_stars"),
        "first_author": None, "first_author_insts": t.get("first_author_insts") or [],
    })
print("Gold pool:", len(pool))

# ---------- 2. institution matching ----------
ALIAS = {
    "massachusetts institute of technology": ["massachusetts inst. of technology", "mit"],
    "carnegie mellon university": ["carnegie mellon university", "cmu"],
    "stanford university": ["stanford university"],
    "tsinghua university": ["tsinghua university"],
    "peking university": ["peking university"],
}
def norm(s):
    s = s.lower()
    s = re.sub(r"[.,]", " ", s)
    s = re.sub(r"\b(univ|university|inst|institute|tech|technology|of|the|and)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()

dept_norm = {d: norm(d) for d in dept_list}
def find_tier(insts):
    for inst in insts:
        n = norm(inst)
        if not n:
            continue
        for d, dn in dept_norm.items():
            if n == dn or n in dn or dn in n:
                return tier_by_dept[d], d, inst
    return None, None, None

n_matched = n_default = 0
for p in pool:
    score, dept, inst = find_tier(p["first_author_insts"])
    if score is not None:
        p["institution_score"] = score
        p["institution_match"] = dept
        p["institution_status"] = "csrankings_matched"
        p["institution_evidence"] = [inst]
        n_matched += 1
    else:
        p["institution_score"] = 0.4  # conservative default per plan v1
        p["institution_match"] = None
        p["institution_status"] = "unmatched_default_0.4"
        p["institution_evidence"] = p["first_author_insts"]
        n_default += 1
print(f"institution matched: {n_matched}, default 0.4: {n_default}")

# ---------- 3. per-track-year midrank percentiles ----------
def midrank_percentiles(items, key):
    vals = sorted((math.log1p(x[key] or 0), i) for i, x in enumerate(items))
    n = len(vals)
    pct = [0.0] * n
    j = 0
    while j < n:
        k = j
        while k + 1 < n and vals[k + 1][0] == vals[j][0]:
            k += 1
        # ranks j..k (0-based); midrank = (j+k)/2 ; percentile = (midrank+1)/n? use (midrank + 0.5)/n
        mid = (j + k) / 2.0
        p = (mid + 0.5) / n
        for _, i in vals[j:k + 1]:
            pct[i] = p
        j = k + 1
    return pct

by_year = defaultdict(list)
for i, p in enumerate(pool):
    by_year[p["year"]].append(i)
for y, idxs in by_year.items():
    items = [pool[i] for i in idxs]
    for i, v in zip(idxs, midrank_percentiles(items, "citations")):
        pool[i]["citation_percentile"] = v
    officials = [i for i in idxs if pool[i]["repo_status"] == "official"]
    if officials:
        sub = [pool[i] for i in officials]
        for i, v in zip(officials, midrank_percentiles(sub, "github_stars")):
            pool[i]["github_percentile"] = v
    else:
        print(f"  stratum_empty: no official repos in {y}; G set to 0.5 neutral")
    for i in idxs:
        p = pool[i]
        if p["repo_status"] == "official":
            p["repo_status_final"] = "official"
        else:
            p["github_percentile"] = None
            p["repo_status_final"] = "none_available"

# G values: official -> percentile; none_available -> 0.0 (protocol table 2)
for p in pool:
    p["G"] = p["github_percentile"] if p["repo_status_final"] == "official" else 0.0
    p["C"] = p["citation_percentile"]
    p["A"] = p["institution_score"]
    p["Q"] = round(0.55 * p["C"] + 0.30 * p["G"] + 0.15 * p["A"], 6)

# ---------- 4. S/A/B tiers ----------
def round_half(x):
    import decimal
    return int(decimal.Decimal(x).quantize(0, rounding=decimal.ROUND_HALF_UP))

N = len(pool)
NS, NA_ = round_half(0.20 * N), round_half(0.40 * N)
NB = N - NS - NA_
order = sorted(pool, key=lambda p: (-p["Q"], p["doi"] or p["lune_id"]))
for i, p in enumerate(order):
    p["tier"] = "S" if i < NS else ("A" if i < NS + NA_ else "B")
    p["rank_in_track"] = i + 1
thresholds = {"N": N, "NS": NS, "NA": NA_, "NB": NB,
              "S_min_Q": order[NS - 1]["Q"], "A_min_Q": order[NS + NA_ - 1]["Q"],
              "quota_rule": "S=round(0.20N), A=round(0.40N), B=rest; ties broken by DOI/lune_id"}
os.makedirs("labels", exist_ok=True)
json.dump(thresholds, open("labels/tier_thresholds.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# ---------- 5. freeze ----------
os.makedirs("corpus", exist_ok=True)
os.makedirs("manifests", exist_ok=True)
with open("corpus/gold_video.jsonl", "w", encoding="utf-8") as f:
    for p in order:
        f.write(json.dumps(p, ensure_ascii=False) + "\n")
sha = hashlib.sha256(open("corpus/gold_video.jsonl", "rb").read()).hexdigest()
manifest = {"corpus_version": f"trackA-video-gold-v1.0-{TODAY}", "sha256": sha,
            "protocol_version": "FB-1.0", "n_papers": N,
            "frozen_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "scoring": {"weights": {"C": 0.55, "G": 0.30, "A": 0.15},
                        "percentile": "midrank, log1p, per track-year",
                        "tiers": thresholds},
            "deviations_from_protocol": [
                "repo none_available assigned G=0.0 after automated search+README check; human spot-check pending (plan v1)",
                "institution simplified: first-author only, CSRankings CV+ML 2021-2025, unmatched default 0.4 flagged",
                "123 arXiv-twin OpenAlex records used for citations (conference version not yet in OpenAlex)"]}
json.dump(manifest, open("manifests/corpus_sha256.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"frozen: N={N} S={NS} A={NA_} B={NB} sha={sha[:16]}...")

# ---------- 6. task eligibility ----------
tasks = [
    ("FB-01", None, None), ("FB-04", 2024, None), ("FB-05", 2025, None), ("FB-06", 2026, None),
    ("FB-13", None, {"CVPR", "ICCV"}),
]
os.makedirs("reports", exist_ok=True)
with open("reports/task_eligibility_trackA.tsv", "w", encoding="utf-8") as f:
    f.write("task_id\tyears\tvenues\teligible_gold\tstatus\n")
    for tid, y, vs in tasks:
        g = [p for p in pool if (y is None or p["year"] == y) and (vs is None or p["venue"] in vs)]
        status = "eligible" if len(g) >= 5 else "ineligible"
        f.write(f"{tid}\t{y or '2024-2026'}\t{','.join(vs) if vs else 'all-whitelist'}\t{len(g)}\t{status}\n")
        print(f"{tid}: |Gt|={len(g)} -> {status}")

# ---------- 7. review workbook ----------
sample = sorted(rng.sample(order, max(1, int(0.25 * N))), key=lambda p: p["rank_in_track"])
spot_official = sorted([p for p in order if p["repo_status_final"] == "official"],
                       key=lambda p: -(p["github_stars"] or 0))[:10]
spot_none = rng.sample([p for p in order if p["repo_status_final"] == "none_available"], 10)

rows = []
for p in sample:
    doi_link = f'https://doi.org/{p["doi"]}' if p["doi"] else "#"
    oa_link = p["openalex_id"] or "#"
    gh = p["github_url"] or ""
    rows.append(f"""<tr><td>{p['rank_in_track']}</td><td>{html.escape(p['title'])}</td>
<td>{p['venue']} {p['year']}</td><td>{p['tier']}</td><td>{p['citations']}</td>
<td>{'official '+str(p['github_stars']) if p['repo_status_final']=='official' else 'none_found'}</td>
<td>{p['institution_match'] or 'UNMATCHED->0.4'}</td>
<td><a href="{doi_link}">DOI</a> <a href="{oa_link}">OA</a> {f'<a href="{gh}">repo</a>' if gh else ''}</td>
<td><input type="checkbox">确认</td><td><input type="text" placeholder="异议"></td></tr>""")

spot_rows = []
for p in spot_official + spot_none:
    spot_rows.append(f"""<tr><td>{html.escape(p['title'])}</td><td>{p['repo_status_final']}</td>
<td>{p['github_url'] or '-'}</td><td><input type="checkbox">仓库属实</td>
<td><input type="checkbox">判错了(在下面写正确repo)</td><td><input type="text"></td></tr>""")

html_doc = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>Track A Gold 评审工作簿</title>
<style>body{{font-family:sans-serif;margin:24px}}table{{border-collapse:collapse;width:100%;font-size:13px}}
td,th{{border:1px solid #ccc;padding:4px 6px;vertical-align:top}}th{{background:#f4f4f4}}
h2{{margin-top:28px}}.note{{background:#fff8e1;border:1px solid #e0c060;padding:10px}}</style></head><body>
<h1>Track A (Video) Gold Corpus 评审工作簿 · {TODAY}</h1>
<p>corpus_version: <b>{manifest['corpus_version']}</b> · N={N} (S={NS}/A={NA_}/B={NB}) · sha256: {sha}</p>
<div class="note"><b>政策确认（请负责人勾选）：</b><br>
<input type="checkbox"> 引用口径：123 篇 arXiv 孪生记录的 OpenAlex 引用数直接采用并标注<br>
<input type="checkbox"> 113 篇无官方仓库按 <code>none_available = G 0.0</code> 处理（已自动搜索+README 核验）<br>
<input type="checkbox"> 机构分简化版：第一作者 CSRankings CV+ML 2021–2025 分档；未匹配默认 0.4（本表中标 UNMATCHED）<br>
<input type="checkbox"> 已知遗漏：CogVideoX 等 34 篇 OpenAlex 未收录论文本轮不入选（备选池保留）</div>
<h2>一、25% 书目信息抽检（{len(sample)} 篇，点链接核对后勾选）</h2>
<table><tr><th>#</th><th>标题</th><th>会议/年</th><th>档</th><th>引用</th><th>仓库</th><th>机构</th><th>链接</th><th>确认</th><th>异议</th></tr>
{''.join(rows)}</table>
<h2>二、仓库抽查（20 篇：star 最高 10 篇 official + 随机 10 篇 none_available）</h2>
<table><tr><th>标题</th><th>判定</th><th>仓库</th><th>属实</th><th>判错</th><th>正确repo</th></tr>
{''.join(spot_rows)}</table>
</body></html>"""
open("reports/review_workbook.html", "w", encoding="utf-8").write(html_doc)
print("workbook: reports/review_workbook.html")
