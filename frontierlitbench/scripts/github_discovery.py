"""Step 6b discovery pass: propose official-repo CANDIDATES via GitHub search.

For each triaged paper (in_scope + needs_review), search GitHub for repos
whose name/description matches the paper title's salient words. Top 3 kept
with match metadata. repo_status stays 'unverified' until a human confirms
the repo is linked from the paper/project page/authors (protocol table 2).
"""
import json, os, re, time, urllib.parse, urllib.request

def load_key():
    kv = {}
    for line in open("keys.local.env", encoding="utf-8"):
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    return kv["GITHUB_TOKEN"]

TOKEN = load_key()

def gh_search(q):
    url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode({"q": q, "sort": "stars", "per_page": 3})
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}", "User-Agent": "frontierlitbench-prep",
        "Accept": "application/vnd.github+json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode())
                return d.get("items", []), r.headers.get("X-RateLimit-Remaining")
        except urllib.error.HTTPError as e:
            if e.code == 403 and attempt < 2:
                time.sleep(30); continue
            return [], None
        except Exception:
            time.sleep(5)
    return [], None

STOP = {"with", "from", "using", "via", "for", "and", "the", "a", "an", "of", "to", "in", "towards", "your", "our", "its"}

def salient_words(title, n=5):
    words = re.findall(r"[A-Za-z][A-Za-z0-9+-]*", title)
    picked = []
    for w in words:
        wl = w.lower()
        if wl in STOP or len(wl) < 3:
            continue
        picked.append(w)
        if len(picked) >= n:
            break
    return picked

rows = [json.loads(l) for l in open("labels/triage_video.jsonl", encoding="utf-8")]
targets = [r for r in rows if r["bucket"] == "likely_in_scope"]
print("targets:", len(targets))

PROG = "labels/github_candidates.jsonl"
done = set()
if os.path.exists(PROG):
    for l in open(PROG, encoding="utf-8"):
        try:
            done.add(json.loads(l)["lune_id"])
        except Exception:
            pass
out = open(PROG, "a", encoding="utf-8")
n_done = 0
for r in targets:
    if r["lune_id"] in done:
        continue
    words = salient_words(r["title"] or "")
    if not words:
        continue
    q = " ".join(words) + " in:name,description"
    items, remaining = gh_search(q)
    cands_out = []
    for it in items:
        cands_out.append({
            "full_name": it["full_name"], "html_url": it["html_url"],
            "stars": it["stargazers_count"], "description": (it.get("description") or "")[:200],
            "created_at": it["created_at"],
        })
    out.write(json.dumps({"lune_id": r["lune_id"], "title": r["title"],
                          "query": q, "repo_candidates": cands_out}, ensure_ascii=False) + "\n")
    n_done += 1
    if n_done % 25 == 0:
        out.flush()
        print(f"{n_done} searched, rate remaining={remaining}", flush=True)
    time.sleep(1.2)
out.close()
print("done", n_done)
