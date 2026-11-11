"""Offline FrontierLitBench scorer. Never supplies gold data to a system.

Example: python evaluation/frontierlitbench_score.py --result runs/.../result.json \
  --corpus frontierlitbench/corpus/gold_agents.jsonl --task FB-03 --out score.json
Unmatched/ambiguous entries are pending adjudication; metrics stay null until resolved.
Optional --adjudications accepts a shared JSON list. Each record has a DOI or
arXiv ID, and either {"decision":"invalid"} or {"decision":"eligible",
"identity_verified":true,"track":"C","year":2025,"venue":"ACL","tier":"S"}.
Adjudicate with frozen reference thresholds before supplying this file; its
eligible records extend the IDCG pool for all systems without changing Gold.
"""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import re
import unicodedata
from urllib.parse import unquote

TASKS = {
    'FB-01': ({2024, 2025, 2026}, None),
    'FB-04': ({2024}, None), 'FB-05': ({2025}, None), 'FB-06': ({2026}, None),
    'FB-13': ({2024, 2025, 2026}, {'CVPR', 'ICCV'}),
    'FB-02': ({2024, 2025, 2026}, None),
    'FB-07': ({2024}, None), 'FB-08': ({2025}, None), 'FB-09': ({2026}, None),
    'FB-14': ({2024, 2025, 2026}, {'ICLR', 'ICML'}),
    'FB-03': ({2024, 2025, 2026}, None),
    'FB-10': ({2024}, None), 'FB-11': ({2025}, None), 'FB-12': ({2026}, None),
    'FB-15': ({2024, 2025, 2026}, {'ACL', 'EMNLP'}),
}
GAINS = {'S': 5, 'A': 3, 'B': 1}
TRACK_TASKS = {'A': {'FB-01','FB-04','FB-05','FB-06','FB-13'},
               'B': {'FB-02','FB-07','FB-08','FB-09','FB-14'},
               'C': {'FB-03','FB-10','FB-11','FB-12','FB-15'}}
VENUES = {'A': {'CVPR','ICCV','ICLR','ICML','NeurIPS','ACM MM','SIGGRAPH'},
          'B': {'AAAI','CVPR','ICCV','ICLR','ICML','NeurIPS'},
          'C': {'ICLR','ICML','NeurIPS','ACL','EMNLP','AAAI'}}


def doi(value):
    value = unquote(str(value or '')).strip().lower()
    m = re.search(r'10\.\d{4,9}/[^\s?#]+', value)
    if not m:
        return ''
    v = m.group().rstrip('.,;')
    # arXiv DOIs are aliases for an arXiv identifier, not publication DOI.
    return '' if v.startswith('10.48550/arxiv.') else v


def arxiv(value):
    value = str(value or '').strip().lower()
    m = re.search(r'(?:arxiv[.:/]|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5})(?:v\d+)?', value)
    if not m:
        m = re.fullmatch(r'(\d{4}\.\d{4,5})(?:v\d+)?', value)
    return m.group(1) if m else ''


def title(value):
    v = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return ''.join(c for c in v if c.isalnum())


def identities(p):
    values = [p.get(k) for k in ('id', 'paper_id', 'doi', 'arxiv_id', 'arxiv_id_resolved', 'url')]
    nested = p.get('identifiers') or {}
    if isinstance(nested, dict):
        values.extend(nested.get(k) for k in ('doi', 'arxiv_id', 'canonical_url'))
    values += list(p.get('dois') or []) + list(p.get('arxiv_ids') or [])
    return ({x for v in values if (x := doi(v))}, {x for v in values if (x := arxiv(v))})


def score(result, gold, task, adjudications=None):
    if task not in TASKS:
        raise ValueError('unknown task: ' + str(task))
    if not isinstance(result, dict) or not isinstance(result.get('papers'), list):
        raise ValueError('result must have a papers list')
    track = next(k for k, v in TRACK_TASKS.items() if task in v)
    if isinstance(gold, list):
        if track == 'A':
            gold = [p for p in gold if not (isinstance(p, dict) and
                    p.get('institution_status') == 'unverified_excluded' and p.get('tier') is None)]
        gold = [dict(p, paper_id=p.get('paper_id') or p.get('lune_id'),
                     tier=p.get('tier') or (p.get('impact') or {}).get('tier'))
                if isinstance(p, dict) else p for p in gold]
    if not isinstance(gold, list) or any(not isinstance(p, dict) or p.get('tier') not in GAINS for p in gold):
        raise ValueError('gold must be a list of records with valid tier')
    gold_ids = [p.get('paper_id') for p in gold]
    if any(not isinstance(i, str) or not i.strip() for i in gold_ids) or len(set(gold_ids)) != len(gold_ids):
        raise ValueError('gold paper IDs must be present and unique')
    years, venues = TASKS[task]
    def eligible(p):
        return p.get('year') in years and p.get('venue') in (venues or VENUES[track])
    valid = [p for p in gold if eligible(p)]
    if len(valid) < 5:
        raise ValueError('task requires at least five eligible Gold records')
    index = {'doi': {}, 'arxiv': {}, 'title_year': {}}
    def add(kind, key, i):
        if key:
            index[kind].setdefault(key, set()).add(i)
    for i, p in enumerate(gold):
        ds, ars = identities(p)
        for v in ds: add('doi', v, i)
        for v in ars: add('arxiv', v, i)
        if title(p.get('title')) and p.get('year'):
            add('title_year', (title(p['title']), str(p['year'])), i)
    # The same adjudication pool must be used for every compared system. It is
    # an independent set of new identities; frozen Gold is never modified.
    if adjudications is None:
        adjudications = []
    if not isinstance(adjudications, list):
        raise ValueError('adjudications must be a list')
    adjud_index = {'doi': {}, 'arxiv': {}, 'title_year': {}}
    for i, p in enumerate(adjudications):
        if not isinstance(p, dict):
            raise ValueError('adjudication must be an object')
        ds, ars = identities(p)
        if not ds and not ars:
            raise ValueError('adjudication requires a DOI or arXiv ID')
        if len(ds) > 1 or len(ars) > 1:
            raise ValueError('adjudication has conflicting identifiers')
        decision = p.get('decision')
        if decision not in ('eligible', 'invalid'):
            raise ValueError('adjudication decision must be eligible or invalid')
        if decision == 'eligible':
            if (p.get('identity_verified') is not True or p.get('track') != track
                    or type(p.get('year')) is not int or p['year'] not in (2024, 2025, 2026)
                    or p.get('venue') not in VENUES[track]
                    or p.get('tier') not in GAINS):
                raise ValueError('eligible adjudication requires verified identity, track, year, venue and tier')
        elif p.get('tier') is not None:
            raise ValueError('invalid adjudication cannot have a tier')
        for kind, keys in (('doi', ds), ('arxiv', ars)):
            for key in keys:
                if key in index[kind]:
                    raise ValueError('adjudication conflicts with frozen Gold identity')
                if key in adjud_index[kind]:
                    raise ValueError('duplicate adjudication identity')
                adjud_index[kind][key] = i
        if title(p.get('title')) and p.get('year'):
            key = (title(p['title']), str(p['year']))
            adjud_index['title_year'].setdefault(key, set()).add(i)
    output, seen_gold, seen_adjud, seen_unknown = [], set(), set(), set()
    items = result['papers'][:5]
    for position in range(5):
        if position >= len(items) or items[position] is None:
            output.append({'rank': position+1, 'status': 'missing', 'gain': 0}); continue
        p = items[position]
        if not isinstance(p, dict):
            output.append({'rank': position+1, 'status': 'pending_adjudication', 'gain': None, 'reason': 'invalid_record'}); continue
        ds, ars = identities(p)
        matches = set()
        match_method = None
        # Conflicting strong identifiers must never be silently resolved by
        # taking the first match. Keep the recommendation for adjudication.
        strong_matches = set().union(*(index['doi'].get(k, set()) for k in ds),
                                     *(index['arxiv'].get(k, set()) for k in ars))
        for kind, keys in [('doi', ds), ('arxiv', ars), ('title_year', {(title(p.get('title')), str(p.get('year'))) } if title(p.get('title')) and p.get('year') else set())]:
            hits = set().union(*(index[kind].get(key, set()) for key in keys)) if keys else set()
            if hits:
                matches, match_method = hits, kind
                break
        row = {'rank': position+1, 'title': p.get('title'), 'match_method': match_method}
        adj_hits = {adjud_index[kind][key] for kind, keys in (('doi', ds), ('arxiv', ars))
                    for key in keys if key in adjud_index[kind]}
        adj_method = 'adjudicated_strong_id'
        if not matches and not adj_hits and title(p.get('title')) and p.get('year'):
            adj_hits = adjud_index['title_year'].get((title(p['title']), str(p['year'])), set())
            adj_method = 'adjudicated_title_year'
        if matches and adj_hits:
            row.update(status='pending_adjudication', gain=None, reason='conflicting_identifiers')
            output.append(row)
            continue
        if len(matches) == 1:
            i = next(iter(matches)); g = gold[i]
            gold_dois, gold_arxivs = identities(g)
            conflict = (len(strong_matches) > 1 or
                        bool(ds and gold_dois and ds.isdisjoint(gold_dois)) or
                        bool(ars and gold_arxivs and ars.isdisjoint(gold_arxivs)))
            if conflict:
                row.update(status='pending_adjudication', gain=None, reason='conflicting_identifiers')
                output.append(row)
                continue
            row['paper_id'] = g.get('paper_id')
            if i in seen_gold:
                row.update(status='duplicate', gain=0)
            else:
                seen_gold.add(i)
                row.update(status='matched' if eligible(g) else 'out_of_scope', gain=GAINS[g['tier']] if eligible(g) else 0, tier=g['tier'])
        elif len(matches) > 1:
            row.update(status='pending_adjudication', gain=None, reason='ambiguous_match')
        else:
            if len(adj_hits) > 1:
                row.update(status='pending_adjudication', gain=None, reason='conflicting_identifiers')
                output.append(row)
                continue
            if adj_hits:
                i = next(iter(adj_hits)); adjud = adjudications[i]
                adois, aarxivs = identities(adjud)
                if ((ds and adois and ds.isdisjoint(adois)) or
                        (ars and aarxivs and ars.isdisjoint(aarxivs))):
                    row.update(status='pending_adjudication', gain=None, reason='conflicting_identifiers')
                elif i in seen_adjud:
                    row.update(status='duplicate', gain=0)
                else:
                    seen_adjud.add(i)
                    row['match_method'] = adj_method
                    if adjud['decision'] == 'invalid':
                        row.update(status='invalid_adjudicated', gain=0)
                    elif eligible(adjud):
                        row.update(status='matched_adjudicated', tier=adjud['tier'], gain=GAINS[adjud['tier']])
                    else:
                        row.update(status='out_of_scope', tier=adjud['tier'], gain=0)
                output.append(row)
                continue
            # Match duplicate unknown recommendations conservatively by identity.
            keys = {('doi', k) for k in ds} | {('arxiv', k) for k in ars}
            if title(p.get('title')) and p.get('year'):
                keys.add(('title_year', title(p['title']), str(p['year'])))
            if keys & seen_unknown:
                row.update(status='duplicate', gain=0)
            else:
                seen_unknown.update(keys)
                row.update(status='pending_adjudication', gain=None, reason='not_in_gold')
        output.append(row)
    additions = [p for p in adjudications if p['decision'] == 'eligible' and eligible(p)]
    ideal = sorted([GAINS[p['tier']] for p in valid] + [GAINS[p['tier']] for p in additions], reverse=True)[:5]
    idcg = sum(g / math.log2(i+2) for i, g in enumerate(ideal))
    pending = any(x['gain'] is None for x in output)
    metrics = None
    # A completed search may legitimately return fewer than five verified
    # papers; missing positions receive zero. An error or interruption is not
    # an empty recommendation and must never be presented as a scored run.
    exhausted = (result.get('status') == 'partial' and bool(items) and
                 result.get('stop_reason') in ('search_budget_exhausted', 'repeated_query'))
    run_failed = result.get('status') not in (None, 'complete', 'completed') and not exhausted
    if not pending and not run_failed:
        gains = [x['gain'] for x in output]
        metrics = {'ndcg_at_5': sum(g / math.log2(i+2) for i, g in enumerate(gains)) / idcg,
                   'weighted_hit_at_5_percent': sum(gains) / 25 * 100,
                   's_tier_hit_at_5': sum(g == 5 for g in gains)}
    return {'task': task, 'run_id': result.get('run_id'), 'run_status': result.get('status'),
            'status': 'run_incomplete' if run_failed else ('pending_adjudication' if pending else 'scored'), 'metrics': metrics,
            'eligible_gold_count': len(valid), 'eligible_adjudicated_count': len(additions),
            'eligible_s_count': sum(p['tier']=='S' for p in valid + additions),
            'ideal_gains': ideal, 'idcg': idcg, 'positions': output,
            'extra_recommendations_ignored': max(0, len(result['papers'])-5)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--task', choices=TASKS, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--adjudications', type=Path, help='JSON list of unified Gold-outside decisions')
    args = parser.parse_args()
    result = json.loads(args.result.read_text(encoding='utf-8-sig'))
    gold = [json.loads(line) for line in args.corpus.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    adjudications = json.loads(args.adjudications.read_text(encoding='utf-8-sig')) if args.adjudications else None
    report = score(result, gold, args.task, adjudications)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
