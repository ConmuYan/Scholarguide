#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Track A pilot evaluation (run_version pilot): run the 5 FB-1.0 Track A tasks
through a Lune-retrieval pipeline (mechanical proxy for ScholarGuide's retrieval
stage), match Top-5 against frozen Gold v1.2.1, and compute FB-1.0 §8 metrics.

Honest scope: this is a RETRIEVAL PILOT, not the official system run — the full
ScholarGuide agent (LLM reasoning + tools) was not executed here. One run per
task (protocol suggests 3 for official runs). Results are optimistic-bound
because Gold itself was discovered via Lune (same source).
"""
import json, os, re, sys, unicodedata, datetime, collections
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '07_管线脚本'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lune_client import LuneClient

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLD = os.path.join(BASE, '02_最终语料', 'gold_video.jsonl')
OUTD = os.path.join(BASE, '04_检查与报告')

TRACK_DESC = ("video generation and editing: text-to-video and image-to-video generation, "
              "video diffusion models and Diffusion Transformers, controllable video editing, "
              "and temporal consistency")
VENUE_WHITELIST = ["CVPR", "ICCV", "ICLR", "ICML", "NeurIPS", "ACM MM", "SIGGRAPH"]

TASKS = [
    ('FB-01', None, None, "2024-2026"),
    ('FB-04', 2024, None, "2024"),
    ('FB-05', 2025, None, "2025"),
    ('FB-06', 2026, None, "2026"),
    ('FB-13', None, {"CVPR", "ICCV"}, "2024-2026, venues CVPR/ICCV"),
]

PROMPT_TEMPLATE = """You are an academic paper discovery and recommendation assistant. Recommend exactly five real,
peer-reviewed papers that are most influential and research-significant for the specified frontier AI topic.
Only use the provided publication years and conference whitelist. Rank the papers from most to least
significant. For every recommendation provide: rank, exact paper title, authors (all if available), formal
publication year, accepted venue, DOI or canonical URL, and a brief evidence-grounded reason. Do
not invent papers or publication details. If fewer than five valid papers can be verified, return only
the verified ones and explicitly report the gap.
Topic: {TRACK_DESCRIPTION}
Years: {YEAR_FILTER}
Eligible conferences: {VENUE_WHITELIST}
Count: 5"""

def norm_title(s):
    s = unicodedata.normalize('NFKD', s or '').lower()
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9 ]', ' ', s)).strip()

def norm_doi(d):
    return re.sub(r'^https?://(dx\.)?doi\.org/', '', (d or '').strip().lower())

def main():
    gold = [json.loads(l) for l in open(GOLD, encoding='utf-8')]
    scored = [g for g in gold if g.get('Q') is not None]
    by_doi = {norm_doi(g.get('doi')): g for g in scored if g.get('doi')}
    by_title_year = {(norm_title(g['title']), g['year']): g for g in scored}
    by_pid = {g['lune_id']: g for g in scored}
    # title-only fallback when exactly one gold candidate shares the title (guards year metadata drift)
    title_groups = collections.defaultdict(list)
    for g in scored:
        title_groups[norm_title(g['title'])].append(g)

    client = LuneClient()
    client.initialize()

    results = []
    raw_all = {}
    for tid, year, venues, yf in TASKS:
        pool = [g for g in scored if (year is None or g['year'] == year) and (venues is None or g['venue'] in venues)]
        prompt = PROMPT_TEMPLATE.replace('{TRACK_DESCRIPTION}', TRACK_DESC).replace('{YEAR_FILTER}', yf).replace('{VENUE_WHITELIST}', ', '.join(VENUE_WHITELIST))
        # retrieval: one search with the topic text; rank by Lune relevance score (proxy for system ranking)
        resp = client.call('search_papers', query=TRACK_DESC, limit=50)
        raw_all[tid] = resp
        items = resp.get('results', []) if isinstance(resp, dict) else []
        # protocol constraint: system may only use whitelist venues; out-of-whitelist → g=0
        wl = set(VENUE_WHITELIST)
        items = [it for it in items if it.get('conference') in wl]
        def build_top(order_items):
            seen, out = set(), []
            for it in order_items:
                if year is not None and it.get('year') != year: continue
                if venues is not None and it.get('conference') not in venues: continue
                key = it.get('paper_id') or norm_doi(it.get('doi')) or (norm_title(it.get('title')), it.get('year'))
                if key in seen: continue
                seen.add(key)
                out.append(it)
                if len(out) == 5: break
            return out

        def score_top(top):
            recs, gains, pending = [], [], []
            for rank, it in enumerate(top, 1):
                g = by_pid.get(it.get('paper_id'))
                if g is None and it.get('doi'):
                    g = by_doi.get(norm_doi(it.get('doi')))
                if g is None:
                    g = by_title_year.get((norm_title(it.get('title')), it.get('year')))
                if g is None:
                    grp = title_groups.get(norm_title(it.get('title')))
                    if grp and len(grp) == 1:
                        g = grp[0]
                if g is not None:
                    gain = {'S': 5, 'A': 3, 'B': 1}[g['tier']]
                    recs.append({'rank': rank, 'title': it['title'], 'venue': it.get('conference'), 'year': it.get('year'),
                                 'matched': True, 'tier': g['tier'], 'gain': gain, 'doi': it.get('doi')})
                    gains.append(gain)
                else:
                    recs.append({'rank': rank, 'title': it['title'], 'venue': it.get('conference'), 'year': it.get('year'),
                                 'matched': False, 'tier': None, 'gain': 0, 'doi': it.get('doi')})
                    gains.append(0)
                    pending.append(it['title'])
            while len(gains) < 5: gains.append(0)
            import math
            dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains[:5]))
            ideal = sorted(({'S': 5, 'A': 3, 'B': 1}[g['tier']] for g in pool), reverse=True)[:5]
            while len(ideal) < 5: ideal.append(0)
            idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
            return {'top5': recs, 'hits@5': sum(1 for r in recs if r['matched']),
                    'WeightedHit@5_pct': round(sum(gains[:5]) / 25 * 100, 1),
                    'nDCG@5': round(dcg / idcg, 4) if idcg else 0.0,
                    'S_tier_hit@5': 1 if any(r['matched'] and r['tier'] == 'S' for r in recs) else 0,
                    'pending_adjudication': pending}

        probeA = score_top(build_top(items))                                  # Lune relevance order
        probeB = score_top(build_top(sorted(items, key=lambda x: -(x.get('citation_count') or 0))))  # influence rerank by citations
        results.append({'task': tid, 'prompt': prompt, 'n_pool': len(pool),
                        'probeA_lune_relevance': probeA, 'probeB_citation_rerank': probeB})
        print(tid, 'pool', len(pool),
              '| A: hits', probeA['hits@5'], 'nDCG', probeA['nDCG@5'],
              '| B: hits', probeB['hits@5'], 'nDCG', probeB['nDCG@5'])

    avgA = sum(r['probeA_lune_relevance']['nDCG@5'] for r in results) / len(results)
    avgB = sum(r['probeB_citation_rerank']['nDCG@5'] for r in results) / len(results)
    out = {'run_version': 'trackA_pilot_lune_retrieval_2026-11-10',
           'as_of': '2026-11-10', 'corpus_version': 'trackA_video_v1.2.1',
           'disclaimer': 'Retrieval pilot via Lune (same source as Gold construction) — optimistic bound; NOT the official ScholarGuide system run. 1 run/task.',
           'macro_avg_nDCG@5_probeA_lune_relevance': round(avgA, 4),
           'macro_avg_nDCG@5_probeB_citation_rerank': round(avgB, 4),
           'tasks': results}
    json.dump(out, open(os.path.join(OUTD, 'pilot_results_trackA.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    json.dump(raw_all, open(os.path.join(OUTD, 'pilot_raw_responses_trackA.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    with open(os.path.join(OUTD, 'pilot_results_trackA.md'), 'w', encoding='utf-8') as f:
        f.write(f"""# Track A pilot hit-rate run — 2026-11-10

**Scope**: retrieval pilot. The 5 Track A tasks (FB-01/04/05/06/13) were run through a Lune-retrieval
pipeline (mechanical proxy for ScholarGuide's retrieval stage; the full agent was not executed).
Top-5 matched against frozen Gold v1.2.1 (paper_id → DOI → normalized title+year, protocol §7.2),
scored with frozen gains (S=5/A=3/B=1). One run per task.

Two probes, same retrieved pool:
- **Probe A** — Top-5 in Lune's own relevance order (raw retrieval, no influence reranking).
- **Probe B** — Top-5 re-ranked by Lune `citation_count` (metadata any system can see; approximates an "influential papers" intent).

**Macro-average nDCG@5: Probe A = {avgA:.4f} / Probe B = {avgB:.4f}**

| Task | Valid pool | A: Hits@5 | A: nDCG@5 | A: WH@5 | B: Hits@5 | B: nDCG@5 | B: WH@5 |
|---|---|---|---|---|---|---|---|
""")
        for r in results:
            a, b = r['probeA_lune_relevance'], r['probeB_citation_rerank']
            f.write(f"| {r['task']} | {r['n_pool']} | {a['hits@5']}/5 | {a['nDCG@5']:.4f} | {a['WeightedHit@5_pct']}% "
                    f"| {b['hits@5']}/5 | {b['nDCG@5']:.4f} | {b['WeightedHit@5_pct']}% |\n")
        f.write("""
## Caveats
- Optimistic bound: Gold was itself discovered via Lune (same index), so retrieval overlap is inflated vs. an independent source.
- A real ScholarGuide run (LLM + tools, 3 repeats per protocol §7.1) may score differently in either direction.
- Unmatched Top-5 papers (pending_adjudication per §7.3) were counted as gain 0 here; they are listed in the JSON, not adjudicated. Several are plausibly valid papers sitting in the 322-paper review backup pool.
""")
    print('macro avg nDCG@5  A:', round(avgA, 4), ' B:', round(avgB, 4))
    print('written to', OUTD)

if __name__ == '__main__':
    main()
