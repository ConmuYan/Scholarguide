"""Lightweight official-repo verification (per TrackA_精简筛选方案_v1.md).

For each likely_in_scope paper: take its GitHub search proposals, fetch each
candidate repo's README (GitHub API), and accept the highest-star repo whose
README mentions the paper's method token (acronym/CamelCase lead word of the
title, or >=3 salient title words). Everything else -> none_available (0.0),
pending human spot-check in the review workbook.
Output: labels/repo_verification.jsonl
"""
import json, os, re, time, base64, urllib.request, urllib.error

def load_key():
    kv = {}
    for line in open("keys.local.env", encoding="utf-8"):
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    return kv["GITHUB_TOKEN"]

TOKEN = load_key()

def gh(url):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}", "User-Agent": "frontierlitbench-prep",
        "Accept": "application/vnd.github.raw+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            limit = r.headers.get("X-RateLimit-Remaining")
            return r.read().decode("utf-8", "replace"), limit
    except urllib.error.HTTPError as e:
        return None, e.headers.get("X-RateLimit-Remaining") if e.headers else None
    except Exception:
        return None, None

STOP = {"with", "from", "using", "via", "for", "and", "the", "a", "an", "of", "to", "in",
        "towards", "your", "our", "its", "toward", "without", "are", "is", "by", "on"}

def method_token(title):
    """First title token if acronym/CamelCase-ish (typical method name)."""
    m = re.match(r"\s*([A-Za-z][A-Za-z0-9+.-]{2,30})\b", title or "")
    if not m:
        return None, []
    tok = m.group(1)
    is_acronym = (tok.isupper() and len(tok) >= 3) or (
        re.search(r"[a-z]", tok) and re.search(r"[A-Z]", tok[1:]) and len(tok) >= 4)
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9+-]*", title)
             if w.lower() not in STOP and len(w) >= 4][:6]
    return (tok if is_acronym else None), words

triage = [json.loads(l) for l in open("labels/triage_video.jsonl", encoding="utf-8")]
proposals = {}
for l in open("labels/github_candidates.jsonl", encoding="utf-8"):
    r = json.loads(l)
    proposals[r["lune_id"]] = r

targets = [t for t in triage if t["bucket"] == "likely_in_scope"]
PROG = "labels/repo_verification.jsonl"
done = set()
if os.path.exists(PROG):
    for l in open(PROG, encoding="utf-8"):
        try:
            done.add(json.loads(l)["lune_id"])
        except Exception:
            pass
out = open(PROG, "a", encoding="utf-8")
n = 0
for t in targets:
    if t["lune_id"] in done:
        continue
    tok, words = method_token(t["title"])
    props = (proposals.get(t["lune_id"]) or {}).get("repo_candidates", [])
    verified = None
    checks = []
    for p in props[:3]:
        full = p["full_name"]
        # stars via search result are fresh enough; verify README mention
        readme, limit = gh(f"https://api.github.com/repos/{full}/readme")
        if readme is None:
            checks.append({"repo": full, "readme_found": False})
            continue
        low = readme.lower()
        ok = False
        if tok and tok.lower().strip("+.-") in low.replace("-", "").replace("_", "").replace("+", ""):
            ok = True
        else:
            hits = sum(1 for w in words if w.lower() in low)
            ok = hits >= 3
        checks.append({"repo": full, "readme_found": True, "method_token": tok,
                       "word_hits": sum(1 for w in words if w.lower() in low) if not tok else None,
                       "mention_match": ok})
        if ok and verified is None:
            verified = {"github_url": p["html_url"], "full_name": full,
                        "github_stars": p["stars"], "stars_as_of": time.strftime("%Y-%m-%d")}
    rec = {"lune_id": t["lune_id"], "title": t["title"],
           "repo_status": "official" if verified else "none_available",
           "verified": verified, "checks": checks,
           "note": "none_available = automated search+README check found no official repo; human spot-check pending"}
    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
    n += 1
    if n % 20 == 0:
        out.flush()
        print(f"{n} verified this run (rate remaining={limit})", flush=True)
    time.sleep(0.5)
out.close()
print("done", n)
