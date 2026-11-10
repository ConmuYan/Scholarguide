#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 2c：修复 unverifiable 机构——去国别后缀重查 OpenAlex + 附属机构归并别名表"""
import json, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from institution_rescore_v2 import (http_json, norm, load_cache, save_cache,
    count_venue_works, openalex_institution, ALIAS_MAP, BASE, OUT_RUBRIC)

EXTRA_ALIAS = {
    'MIT Computer Science and Artificial Intelligence Laboratory': 'Massachusetts Institute of Technology',
    'First Affiliated Hospital of Zhengzhou University': 'Zhengzhou University',
}
ALIAS_MAP.update(EXTRA_ALIAS)

def clean_name(n):
    return re.sub(r'\s*\([^)]*\)\s*$', '', n).strip()

def main():
    rubric = json.load(open(OUT_RUBRIC, encoding='utf-8'))
    cache = load_cache()
    tiers = json.load(open(os.path.join(BASE, 'labels', 'institution_tiers_csrankings.json'), encoding='utf-8'))
    csr_norm_map = {}
    for t in tiers:
        csr_norm_map.setdefault(norm(t['dept']), []).append(t)

    def try_match(name):
        cand = [name, clean_name(name)]
        if name in ALIAS_MAP: cand.insert(0, ALIAS_MAP[name])
        oa = (cache.get(name) or {}).get('openalex') or {}
        if oa.get('display_name'): cand.append(oa['display_name'])
        cand += oa.get('aliases') or []
        for c in cand:
            if norm(c) in csr_norm_map: return csr_norm_map[norm(c)][0], c
        for c in cand:
            n = norm(c)
            if len(n) < 5: continue
            for cn, recs in csr_norm_map.items():
                if n in cn or cn in n: return recs[0], c
        return None, None

    targets = [n for n, e in rubric['entries'].items() if e['kind'] == 'unverifiable']
    print('待修复:', len(targets))
    new_industry = []
    for name in targets:
        entry = cache.setdefault(name, {})
        # 用干净名重查 OpenAlex
        if entry.get('openalex') is None:
            for q in [clean_name(name), name]:
                try:
                    rec = openalex_institution(q)
                except Exception:
                    rec = None
                if rec and 'id' in rec:
                    entry['openalex'] = {'id': rec['id'], 'ror': rec.get('ror'),
                        'display_name': rec.get('display_name'),
                        'aliases': [a.get('display_name') for a in rec.get('aliases', [])],
                        'type': rec.get('type'), 'works_count': rec.get('works_count')}
                    break
                time.sleep(0.2)
            entry['openalex_done'] = True
        m, via = try_match(name)
        oa = entry.get('openalex') or {}
        if m:
            entry['match'] = {'kind': 'csrankings', 'dept': m['dept'], 'rank': m['rank'],
                              'tier_score': m['tier_score'], 'via': via}
        elif oa and (oa.get('type') or '').lower() == 'education':
            entry['match'] = {'kind': 'verified_university_not_top100', 'tier_score': 0.4, 'via': oa.get('display_name')}
        elif oa:
            entry['match'] = {'kind': 'industry', 'tier_score': None, 'via': oa.get('display_name')}
            new_industry.append(name)
        save_cache(cache)
        print(name, '->', entry['match']['kind'], (oa or {}).get('display_name'))
        time.sleep(0.2)

    # 重算工业分位段（加入新工业机构）
    all_ind = [n for n, e in cache.items()
               if e.get('match', {}).get('kind') == 'industry' and n in {
                   **rubric['entries'], **{n: 1 for n in new_industry}}]
    all_ind = sorted(set(all_ind))
    sid = json.load(open(os.path.join(BASE, 'labels', 'openalex_source_ids.json'), encoding='utf-8'))
    for name in all_ind:
        entry = cache[name]
        if 'industry_count' in entry: continue
        ror = (entry.get('openalex') or {}).get('ror')
        if ror:
            total, per = count_venue_works(ror, sid)
            entry['industry_count'] = {'total': total, 'per': per}
        else:
            entry['industry_count'] = {'total': 0, 'per': {}}
        save_cache(cache)
        print('industry count:', name, entry['industry_count']['total'])
        time.sleep(0.2)
    counts = sorted(((cache[n]['industry_count']['total'], n) for n in all_ind), reverse=True)
    N = len(counts)
    for i, (c, n) in enumerate(counts):
        frac = (i + 0.5) / N
        ts = 1.0 if frac <= 0.25 else 0.8 if frac <= 0.5 else 0.6 if frac <= 0.75 else 0.4
        cache[n]['match'].update(tier_score=ts, industry_total=c, industry_rank=i + 1, industry_population=N)
    save_cache(cache)
    print('industry population:', N)
    print([(n, c) for c, n in counts])

    # 回写 rubric
    for name in targets + new_industry:
        e = cache[name]
        if 'match' not in e: continue
        m = e['match']; oa = e.get('openalex') or {}
        prev = rubric['entries'].get(name, {})
        rubric['entries'][name] = {'canonical': oa.get('display_name'), 'ror': oa.get('ror'),
            'openalex_url': oa.get('id'), 'type': oa.get('type'),
            'papers_affected': prev.get('papers_affected', []), **m}
    rubric['note_v2c'] = 'unverifiable 修复：去国别后缀重查 OpenAlex；MIT CSAIL→MIT、郑大一附院→郑州大学归并。'
    json.dump(rubric, open(OUT_RUBRIC, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    import collections
    print(collections.Counter(e['kind'] for e in rubric['entries'].values()))

if __name__ == '__main__':
    main()
