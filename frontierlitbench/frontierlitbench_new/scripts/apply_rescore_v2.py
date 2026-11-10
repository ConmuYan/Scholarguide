#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 institution_rubric_v2 重算 A → Q → S/A/B，冻结 corpus v1.2"""
import json, os, shutil, hashlib, collections

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLD = os.path.join(BASE, 'corpus', 'gold_video.jsonl')

gold = [json.loads(l) for l in open(GOLD, encoding='utf-8')]
rubric = json.load(open(os.path.join(BASE, 'labels', 'institution_rubric_v2.json'), encoding='utf-8'))['entries']

def score_for(insts):
    """协议：取该作者经确认的最高匹配机构分"""
    best, det = None, None
    for inst in insts or []:
        e = rubric.get(inst)
        if not e or e.get('tier_score') is None:
            continue
        if best is None or e['tier_score'] > best:
            best, det = e['tier_score'], e
    return best, det

changed = 0
unresolvable = []
for g in gold:
    old = (g.get('A'), g.get('institution_status'))
    if g.get('institution_status') == 'csrankings_matched':
        continue  # 本来就匹配上的不动
    a, det = score_for(g.get('first_author_insts'))
    if a is None:
        unresolvable.append(g['title'])
        g['A'] = None
        g['institution_status'] = 'unverified_excluded'
        continue
    g['A'] = a
    g['institution_score'] = a
    g['institution_match'] = det.get('dept') or det.get('via')
    g['institution_status'] = det['kind']
    g['institution_evidence'] = {
        'openalex_url': det.get('openalex_url'), 'ror': det.get('ror'),
        'csrankings_rank': det.get('rank'), 'industry_total_2021_2025': det.get('industry_total'),
        'industry_rank_in_population': det.get('industry_rank'),
        'reason': det['kind'], 'reviewer': 'AI-pipeline(Kimi) 初核，待组内人工抽查',
    }
    changed += 1

# 重算 Q 与分档
valid = [g for g in gold if g.get('A') is not None and g.get('G') is not None]
for g in valid:
    g['Q'] = round(0.55 * g['C'] + 0.30 * g['G'] + 0.15 * g['A'], 6)
valid.sort(key=lambda g: (-g['Q'], g.get('doi') or '', g['lune_id']))
N = len(valid)
ns, na = round(0.2 * N), round(0.4 * N)
for i, g in enumerate(valid):
    g['rank_in_track'] = i + 1
    g['tier'] = 'S' if i < ns else 'A' if i < ns + na else 'B'

print('rescore changed:', changed, '| unresolvable:', len(unresolvable), unresolvable)
print('N valid:', N, 'quota S/A/B:', ns, na, N - ns - na)
print('tier dist:', collections.Counter(g['tier'] for g in valid))
print('A dist:', collections.Counter(g['A'] for g in valid))
# 分档变化
old_gold = {json.loads(l)['lune_id']: json.loads(l) for l in open(GOLD, encoding='utf-8')}
moved = [(g['title'][:60], old_gold[g['lune_id']]['tier'], g['tier']) for g in valid
         if old_gold[g['lune_id']]['tier'] != g['tier']]
print('tier changed:', len(moved))
for t in moved[:15]: print('  ', t)

# 备份 v1.1，冻结 v1.2
shutil.copy(GOLD, GOLD.replace('.jsonl', '_v1.1_backup.jsonl'))
order = {g['lune_id']: g for g in gold}
with open(GOLD, 'w', encoding='utf-8') as f:
    for g in gold:
        g['corpus_version'] = 'trackA_video_v1.2'
        f.write(json.dumps(g, ensure_ascii=False) + '\n')
h = hashlib.sha256(open(GOLD, 'rb').read()).hexdigest()
mani_p = os.path.join(BASE, 'manifests', 'corpus_sha256.json')
mani = json.load(open(mani_p, encoding='utf-8'))
mani['corpus/gold_video.jsonl'] = {'sha256': h, 'corpus_version': 'trackA_video_v1.2',
    'as_of': '2026-11-08', 'note': '机构评分严格化复核：别名/附属机构归并 + 工业实验室按 OpenAlex 2021-2025 顶会产出分位段映射；见 labels/institution_rubric_v2.json；v1.1 备份于 corpus/gold_video_v1.1_backup.jsonl'}
json.dump(mani, open(mani_p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('frozen v1.2 sha256:', h[:16])
