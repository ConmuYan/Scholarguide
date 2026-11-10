#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""机构严格复核 v2（对齐 FrontierLitBench §4.3 / Table 3）
1) OpenAlex /institutions 规范化 + 别名匹配 CSRankings 名单（571 家）
2) 高校：命中名单→1.0/0.8/0.6；真实高校但不在前100→已核验 0.4（留证据）
3) 工业实验室：OpenAlex 统计 2021-2025 在 Track A 顶会产出，群体分位段映射
4) 输出 labels/institution_rubric_v2.json + labels/institution_rescore_v2.json
支持断点续跑（cache 文件）。
"""
import json, re, time, unicodedata, urllib.parse, urllib.request, os, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(BASE, 'labels', 'institution_v2_cache.json')
OUT_RUBRIC = os.path.join(BASE, 'labels', 'institution_rubric_v2.json')
MAILTO = 'qukaiyuan@connect.hku.hk'
UA = 'FrontierLitBench-TrackA/1.0 (mailto:%s)' % MAILTO

TRACK_A_VENUE_KEYWORDS = {
    'cvpr': ['computer vision and pattern recognition'],
    'iccv': ['international conference on computer vision'],
    'eccv': ['european conference on computer vision'],
    'icml': ['international conference on machine learning'],
    'nips': ['neural information processing systems', 'neurips'],
    'iclr': ['international conference on learning representations'],
}

# 人工别名表：OpenAlex 机构名 → CSRankings 名单写法（含附属机构归并到母高校）
ALIAS_MAP = {
    'University of Science and Technology of China': 'USTC',
    'University of Electronic Science and Technology of China': 'UESTC',
    'King Abdullah University of Science and Technology': 'KAUST',
    'Ludwig-Maximilians-Universität München': 'LMU Munich',
    'LMU Klinikum': 'LMU Munich',  # LMU 大学附属医院 → 归并 LMU
    'USC Institute for Creative Technologies': 'University of Southern California',  # USC 下属研究所
}

def http_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as e:
            if attempt == 3: raise
            time.sleep(2 * (attempt + 1))

def norm(s):
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r'[\u2010-\u2015\u2212]', '-', s)  # 各种 dash
    s = re.sub(r'[^a-z0-9 ]', ' ', s)
    s = re.sub(r'\b(univ|university|institute|inst|of|the|at|and|dept|department|college|school)\b', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

def load_cache():
    if os.path.exists(CACHE):
        return json.load(open(CACHE, encoding='utf-8'))
    return {}

def save_cache(c):
    json.dump(c, open(CACHE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

def openalex_institution(name):
    """返回最佳匹配的 OpenAlex 机构记录"""
    q = urllib.parse.quote(name)
    url = f'https://api.openalex.org/institutions?search={q}&per-page=5&mailto={MAILTO}'
    data = http_json(url)
    results = data.get('results', [])
    if not results: return None
    # 简单取第一条（OpenAlex search 已按相关性排序）
    return results[0]

def openalex_source_ids():
    """拿 Track A 六个顶会的 OpenAlex source id"""
    out = {}
    for key, kws in TRACK_A_VENUE_KEYWORDS.items():
        found = None
        for kw in kws:
            q = urllib.parse.quote(kw)
            url = f'https://api.openalex.org/sources?search={q}&per-page=10&mailto={MAILTO}'
            data = http_json(url)
            for s in data.get('results', []):
                dn = (s.get('display_name') or '').lower()
                if any(k in dn for k in kws):
                    found = {'id': s['id'], 'name': s['display_name']}
                    break
            if found: break
        out[key] = found
        print('source', key, found)
    return out

def count_venue_works(ror_id, source_ids):
    """统计某机构 2021-2025 在指定 source 集合的论文数"""
    total = 0
    per = {}
    for key, sid in source_ids.items():
        if not sid: continue
        url = ('https://api.openalex.org/works?filter='
               f'authorships.institutions.ror:{ror_id},'
               'from_publication_date:2021-01-01,to_publication_date:2025-12-31,'
               f'primary_location.source.id:{sid["id"].split("/")[-1]}'
               f'&per-page=1&mailto={MAILTO}')
        try:
            data = http_json(url)
            n = data.get('meta', {}).get('count', 0)
        except Exception:
            n = 0
        per[key] = n
        total += n
        time.sleep(0.3)
    return total, per

def main():
    gold = [json.loads(l) for l in open(os.path.join(BASE, 'corpus', 'gold_video.jsonl'), encoding='utf-8')]
    tiers = json.load(open(os.path.join(BASE, 'labels', 'institution_tiers_csrankings.json'), encoding='utf-8'))
    csr_names = [t['dept'] for t in tiers]
    csr_norm_map = {}
    for t in tiers:
        csr_norm_map.setdefault(norm(t['dept']), []).append(t)

    # 收集所有需要复核的机构名（当前未匹配的）
    targets = {}
    for g in gold:
        if g.get('institution_status') == 'csrankings_matched':
            continue
        for inst in (g.get('first_author_insts') or []):
            targets.setdefault(inst, []).append(g['lune_id'])
    print('待复核机构:', len(targets))

    cache = load_cache()
    # 步骤1：OpenAlex 规范化（含别名）
    for name in targets:
        if name in cache and cache[name].get('openalex') is not None or (name in cache and cache[name].get('openalex_done')):
            continue
        try:
            rec = openalex_institution(name)
        except Exception as e:
            rec = {'error': str(e)}
        entry = cache.setdefault(name, {})
        if rec and isinstance(rec, dict) and 'id' in rec:
            entry['openalex'] = {
                'id': rec['id'], 'ror': rec.get('ror'),
                'display_name': rec.get('display_name'),
                'aliases': [a.get('display_name') for a in rec.get('aliases', [])],
                'type': rec.get('type'),
                'country': (rec.get('geo') or {}).get('country'),
                'works_count': rec.get('works_count'),
                'homepage': rec.get('homepage_url'),
            }
        else:
            entry['openalex'] = None
        entry['openalex_done'] = True
        save_cache(cache)
        print('oa:', name, '->', (entry['openalex'] or {}).get('display_name'), (entry['openalex'] or {}).get('type'))
        time.sleep(0.3)

    # 步骤2：用 规范名+别名 匹配 CSRankings
    def try_match(name):
        cand = [name]
        if name in ALIAS_MAP:
            cand.insert(0, ALIAS_MAP[name])
        oa = cache.get(name, {}).get('openalex') or {}
        if oa.get('display_name'): cand.append(oa['display_name'])
        cand += oa.get('aliases') or []
        for c in cand:
            n = norm(c)
            if n in csr_norm_map:
                return csr_norm_map[n][0], c
        # 双向包含
        for c in cand:
            n = norm(c)
            if len(n) < 5: continue
            for cn, recs in csr_norm_map.items():
                if n in cn or cn in n:
                    return recs[0], c
        return None, None

    industry = []
    for name in targets:
        entry = cache[name]
        if 'match' in entry: continue
        m, via = try_match(name)
        oa = entry.get('openalex') or {}
        if m:
            entry['match'] = {'kind': 'csrankings', 'dept': m['dept'],
                              'rank': m['rank'], 'tier_score': m['tier_score'], 'via': via}
        else:
            itype = (oa.get('type') or '').lower()
            if oa and itype == 'education':
                entry['match'] = {'kind': 'verified_university_not_top100', 'tier_score': 0.4,
                                  'via': oa.get('display_name')}
            elif oa:
                entry['match'] = {'kind': 'industry', 'tier_score': None, 'via': oa.get('display_name')}
                industry.append(name)
            else:
                entry['match'] = {'kind': 'unverifiable', 'tier_score': None, 'via': None}
        save_cache(cache)

    kinds = {}
    for name, e in cache.items():
        if name in targets:
            kinds.setdefault(e['match']['kind'], []).append(name)
    print(json.dumps({k: v for k, v in kinds.items()}, ensure_ascii=False, indent=1))

    # 步骤3：工业实验室产出统计 + 分位段映射
    if industry:
        sid_cache = os.path.join(BASE, 'labels', 'openalex_source_ids.json')
        if os.path.exists(sid_cache):
            source_ids = json.load(open(sid_cache, encoding='utf-8'))
        else:
            source_ids = openalex_source_ids()
            json.dump(source_ids, open(sid_cache, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        for name in industry:
            entry = cache[name]
            if 'industry_count' in entry: continue
            ror = (entry.get('openalex') or {}).get('ror')
            if not ror:
                entry['industry_count'] = {'total': 0, 'per': {}}
            else:
                total, per = count_venue_works(ror, source_ids)
                entry['industry_count'] = {'total': total, 'per': per}
            save_cache(cache)
            print('industry:', name, entry['industry_count']['total'])
        # 分位段：按 total 排序，前25%→1.0, 25-50→0.8, 50-75→0.6, 其余→0.4
        counts = sorted(((cache[n]['industry_count']['total'], n) for n in industry), reverse=True)
        N = len(counts)
        for i, (c, n) in enumerate(counts):
            frac = (i + 0.5) / N
            ts = 1.0 if frac <= 0.25 else 0.8 if frac <= 0.5 else 0.6 if frac <= 0.75 else 0.4
            cache[n]['match']['tier_score'] = ts
            cache[n]['match']['industry_total'] = c
            cache[n]['match']['industry_rank'] = i + 1
            cache[n]['match']['industry_population'] = N
        save_cache(cache)
        print('industry quartiles:', [(n, c) for c, n in counts])

    # 步骤4：汇总 rubric
    rubric = {
        'version': 'institution_rubric_v2',
        'as_of': '2026-11-08',
        'method': 'OpenAlex /institutions 规范化+别名匹配 CSRankings(cv+ml,2021-2025,generated-author-info.csv)；工业实验室按 OpenAlex 2021-2025 TrackA 顶会(CVPR/ICCV/ECCV/ICML/NeurIPS/ICLR)产出分位段映射；证据=OpenAlex 机构页/ROR + csrankings.org',
        'reviewer': 'AI-pipeline(Kimi) 初核，待组内人工抽查',
        'entries': {}
    }
    for name in targets:
        e = cache[name]
        m = e['match']
        oa = e.get('openalex') or {}
        rubric['entries'][name] = {
            'canonical': oa.get('display_name'), 'ror': oa.get('ror'),
            'openalex_url': oa.get('id'), 'type': oa.get('type'),
            'papers_affected': targets[name],
            **m,
        }
    json.dump(rubric, open(OUT_RUBRIC, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('rubric written:', OUT_RUBRIC)

if __name__ == '__main__':
    main()
