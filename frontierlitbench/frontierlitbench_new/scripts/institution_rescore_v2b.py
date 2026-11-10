#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 2b：对 OpenAlex 无论文级机构的 53 篇，用第一作者的 OpenAlex 作者档案
（last_known_institutions）补机构，再走同一套 CSRankings/工业分位匹配，并入 rubric v2。"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from institution_rescore_v2 import (http_json, norm, load_cache, save_cache,
    count_venue_works, BASE, CACHE, OUT_RUBRIC)

GOLD = os.path.join(BASE, 'corpus', 'gold_video.jsonl')

def main():
    gold = [json.loads(l) for l in open(GOLD, encoding='utf-8')]
    targets_papers = [g for g in gold if g.get('institution_status') == 'unverified_excluded'
                      or (g.get('institution_status') == 'unmatched_default_0.4' and not g.get('first_author_insts'))]
    print('需补机构的论文:', len(targets_papers))

    cache = load_cache()
    # 1) 逐篇取 first author 的 last_known_institutions
    fa_cache_p = os.path.join(BASE, 'labels', 'first_author_lookup_cache.json')
    fa_cache = json.load(open(fa_cache_p, encoding='utf-8')) if os.path.exists(fa_cache_p) else {}
    for i, g in enumerate(targets_papers):
        lid = g['lune_id']
        if lid in fa_cache: continue
        try:
            wid = g['openalex_id'].split('/')[-1]
            w = http_json(f'https://api.openalex.org/works/{wid}?mailto=qukaiyuan@connect.hku.hk')
            au = (w.get('authorships') or [{}])[0]
            author_id = (au.get('author') or {}).get('id')
            insts = au.get('institutions') or []
            rec = {'first_author': (au.get('author') or {}).get('display_name'),
                   'author_id': author_id,
                   'work_level_insts': [x.get('display_name') for x in insts]}
            if not insts and author_id:
                aid = author_id.split('/')[-1]
                a = http_json(f'https://api.openalex.org/authors/{aid}?mailto=qukaiyuan@connect.hku.hk')
                lki = a.get('last_known_institutions') or []
                rec['last_known_insts'] = [{'display_name': x.get('display_name'),
                                            'ror': x.get('ror'), 'type': x.get('type'),
                                            'id': x.get('id')} for x in lki]
            fa_cache[lid] = rec
        except Exception as e:
            fa_cache[lid] = {'error': str(e)}
        if (i + 1) % 10 == 0 or i == len(targets_papers) - 1:
            json.dump(fa_cache, open(fa_cache_p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            print(f'  author lookup {i+1}/{len(targets_papers)}')
        time.sleep(0.15)
    json.dump(fa_cache, open(fa_cache_p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    # 2) 汇总出现的新机构名，走匹配
    tiers = json.load(open(os.path.join(BASE, 'labels', 'institution_tiers_csrankings.json'), encoding='utf-8'))
    csr_norm_map = {}
    for t in tiers:
        csr_norm_map.setdefault(norm(t['dept']), []).append(t)
    from institution_rescore_v2 import ALIAS_MAP

    new_insts = {}
    for g in targets_papers:
        rec = fa_cache.get(g['lune_id']) or {}
        ilist = rec.get('work_level_insts') or [x['display_name'] for x in rec.get('last_known_insts', [])]
        for name in ilist:
            new_insts.setdefault(name, []).append(g['lune_id'])
    print('新机构名:', len(new_insts))

    def try_match(name):
        cand = [name]
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

    industry = []
    for name in new_insts:
        entry = cache.setdefault(name, {})
        if 'match' in entry: continue
        if 'openalex_done' not in entry:
            try:
                from institution_rescore_v2 import openalex_institution
                rec = openalex_institution(name)
            except Exception:
                rec = None
            if rec and 'id' in rec:
                entry['openalex'] = {'id': rec['id'], 'ror': rec.get('ror'),
                    'display_name': rec.get('display_name'),
                    'aliases': [a.get('display_name') for a in rec.get('aliases', [])],
                    'type': rec.get('type'), 'works_count': rec.get('works_count')}
            else:
                entry['openalex'] = None
            entry['openalex_done'] = True
            time.sleep(0.2)
        m, via = try_match(name)
        oa = entry.get('openalex') or {}
        if m:
            entry['match'] = {'kind': 'csrankings', 'dept': m['dept'], 'rank': m['rank'],
                              'tier_score': m['tier_score'], 'via': via}
        elif oa and (oa.get('type') or '').lower() == 'education':
            entry['match'] = {'kind': 'verified_university_not_top100', 'tier_score': 0.4, 'via': oa.get('display_name')}
        elif oa:
            entry['match'] = {'kind': 'industry', 'tier_score': None, 'via': oa.get('display_name')}
            industry.append(name)
        else:
            entry['match'] = {'kind': 'unverifiable', 'tier_score': None, 'via': None}
        save_cache(cache)

    # 3) 新工业机构加入群体，全部重新算分位段
    rubric = json.load(open(OUT_RUBRIC, encoding='utf-8'))
    old_ind = [n for n, e in rubric['entries'].items() if e.get('kind') == 'industry']
    all_ind = sorted(set(old_ind) | set(industry))
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
        time.sleep(0.2)
    counts = sorted(((cache[n]['industry_count']['total'], n) for n in all_ind), reverse=True)
    N = len(counts)
    for i, (c, n) in enumerate(counts):
        frac = (i + 0.5) / N
        ts = 1.0 if frac <= 0.25 else 0.8 if frac <= 0.5 else 0.6 if frac <= 0.75 else 0.4
        cache[n]['match'].update(tier_score=ts, industry_total=c, industry_rank=i + 1, industry_population=N)
    save_cache(cache)
    print('industry population now:', N)
    print([(n, c) for c, n in counts])

    # 4) 更新 rubric
    for name in new_insts:
        e = cache[name]; m = e['match']; oa = e.get('openalex') or {}
        rubric['entries'][name] = {'canonical': oa.get('display_name'), 'ror': oa.get('ror'),
            'openalex_url': oa.get('id'), 'type': oa.get('type'),
            'papers_affected': new_insts[name], **m}
    rubric['note_v2b'] = '53 篇 OpenAlex 无论文级机构的记录，改用第一作者 OpenAlex 作者档案 last_known_institutions（作者级证据，已逐篇记录 author_id 与来源）。'
    json.dump(rubric, open(OUT_RUBRIC, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    # 5) 把机构名回写到 gold 的 first_author_insts（供 apply_rescore_v2 用）
    for g in gold:
        if g['lune_id'] in fa_cache and not g.get('first_author_insts'):
            rec = fa_cache[g['lune_id']]
            ilist = rec.get('work_level_insts') or [x['display_name'] for x in rec.get('last_known_insts', [])]
            g['first_author_insts'] = ilist
            g['first_author'] = rec.get('first_author')
            g['institution_evidence_note'] = '作者级证据(OpenAlex last_known_institutions)' if not rec.get('work_level_insts') else '论文级证据(OpenAlex authorships)'
            g['institution_status'] = 'pending_rescore'
    with open(GOLD, 'w', encoding='utf-8') as f:
        for g in gold:
            f.write(json.dumps(g, ensure_ascii=False) + '\n')
    print('gold updated with author-level insts')

if __name__ == '__main__':
    main()
