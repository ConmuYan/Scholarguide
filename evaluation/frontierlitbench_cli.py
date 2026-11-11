"""Create consistent FB prompts, import chat baselines and score local results."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import statistics
from pathlib import Path
import re
import sys
import uuid

from frontierlitbench_score import TASKS, TRACK_TASKS, VENUES, score

ROOT = Path(__file__).resolve().parents[1]
CORPORA = {'A': 'gold_video.jsonl', 'B': 'gold_world_models.jsonl', 'C': 'gold_agents.jsonl'}
TOPICS = {
    'A': 'video generation and editing, including text/image-to-video, video diffusion, controllable editing and temporal consistency',
    'B': 'world models, including latent dynamics, action-conditioned prediction, environment simulation and embodied planning',
    'C': 'LLM agents and tool-augmented reasoning, including tool use, multi-step planning, agent memory, self-correction and multi-agent systems',
}
PROMPT = '''You are an academic paper discovery and recommendation assistant. Recommend exactly five real, peer-reviewed papers that are most influential and research-significant for the specified frontier AI topic. Only use the provided publication years and conference whitelist. Rank the papers from most to least significant. For every recommendation provide: rank, exact paper title, authors (all if available), formal publication year, accepted venue, DOI or canonical URL, and a brief evidence-grounded reason. Do not invent papers or publication details. If fewer than five valid papers can be verified, return only the verified ones and explicitly report the gap.

Topic: {topic}
Years: {years}
Eligible conferences: {venues}
Count: 5'''
FORMAT = '''\n\nFor reliable import, provide the recommendations as one JSON object with a papers array in ranked order. Each paper should contain rank, title, authors, year, venue, doi or url, and reason. Use null for unknown details. Do not include any benchmark labels or scores.'''


def track_for(task):
    return next(k for k, values in TRACK_TASKS.items() if task in values)


def prompt_for(task):
    track = track_for(task)
    years, venues = TASKS[task]
    return PROMPT.format(topic=TOPICS[track], years=', '.join(map(str, sorted(years))),
                         venues=', '.join(sorted(venues or VENUES[track])))


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def load_gold(task, corpus=None):
    path = corpus or ROOT / 'frontierlitbench' / 'corpus' / CORPORA[track_for(task)]
    return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]


def save_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')


def parse_response(raw):
    """Accept one JSON object/array or one fenced JSON block; do not guess missing data."""
    stripped = raw.strip()
    fences = re.findall(r'```(?:json)?\s*([\s\S]*?)```', stripped, flags=re.I)
    if fences:
        if len(fences) != 1:
            raise ValueError('Expected exactly one JSON code block.')
        stripped = fences[0].strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError('Response must contain one valid JSON object or array; preserve the original response and correct the transcription.') from exc
    papers = value if isinstance(value, list) else value.get('papers') if isinstance(value, dict) else None
    if not isinstance(papers, list):
        raise ValueError('Response must contain a papers array.')
    for i, paper in enumerate(papers, 1):
        if not isinstance(paper, dict) or not isinstance(paper.get('title'), str) or not paper['title'].strip():
            raise ValueError(f'Paper {i} needs a title.')
        if paper.get('rank') is not None and paper['rank'] != i:
            raise ValueError('Explicit ranks must match the array order; do not silently reorder results.')
        if paper.get('year') is not None and type(paper['year']) is not int:
            raise ValueError(f'Paper {i} year must be an integer or null.')
    return papers


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prompt', help='Print the common prompt; no Gold data is included.')
    p.add_argument('--task', choices=TASKS, required=True)
    p.add_argument('--json-format', action='store_true', help='Append JSON formatting instructions for chat systems.')
    p = sub.add_parser('import-chat', help='Import a saved chat response without changing its ranking.')
    p.add_argument('--task', choices=TASKS, required=True)
    p.add_argument('--response', type=Path, required=True)
    p.add_argument('--system', required=True, help='System and model/version, e.g. ChatGPT (model name)')
    p.add_argument('--tools', required=True, help='Tool and browsing settings used in the run')
    p.add_argument('--out', type=Path, required=True, help='New result JSON path')
    p = sub.add_parser('score', help='Score a ScholarGuide or imported chat result against the local snapshot.')
    p.add_argument('--task', choices=TASKS, required=True)
    p.add_argument('--result', type=Path, required=True)
    p.add_argument('--adjudications', type=Path)
    p.add_argument('--corpus', type=Path, help='Explicit reference corpus in JSONL format')
    p.add_argument('--out', type=Path, required=True)
    p = sub.add_parser('summary', help='Summarize a complete set of task repetitions for one system.')
    p.add_argument('--track', choices=TRACK_TASKS, required=True)
    p.add_argument('--repeat', type=int, default=3)
    p.add_argument('--scores', type=Path, nargs='+', required=True)
    p.add_argument('--out', type=Path, required=True)
    p = sub.add_parser('overall', help='Average the three complete track summaries for one system.')
    p.add_argument('--summaries', type=Path, nargs=3, required=True)
    p.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == 'prompt':
        print(prompt_for(args.task) + (FORMAT if args.json_format else ''))
    elif args.command == 'import-chat':
        raw = args.response.read_text(encoding='utf-8-sig')
        papers = parse_response(raw)
        result = {'schema_version': 1, 'run_id': uuid.uuid4().hex, 'status': 'complete',
                  'task': args.task, 'system': args.system, 'tools': args.tools,
                  'imported_at_utc': datetime.now(timezone.utc).isoformat(),
                  'raw_response': raw, 'papers': papers}
        save_new(args.out, result)
        print('Saved', args.out)
    elif args.command == 'score':
        result = read_json(args.result)
        if result.get('task') and result['task'] != args.task:
            raise ValueError('Result task does not match the requested task.')
        adjudications = read_json(args.adjudications) if args.adjudications else None
        report = score(result, load_gold(args.task, args.corpus), args.task, adjudications)
        report['reference_status'] = 'exploratory; current corpus and labels are under review'
        report['corpus'] = str(args.corpus.resolve()) if args.corpus else CORPORA[track_for(args.task)]
        report['adjudications'] = str(args.adjudications.resolve()) if args.adjudications else None
        config = result.get('config') if isinstance(result.get('config'), dict) else {}
        report['system'] = result.get('system') or ('ScholarGuide / ' + str(config.get('model')) if config.get('model') else None)
        report['conditions'] = ({'tools': result['tools']} if result.get('tools') else
                                {'source': config.get('source'), 'method': result.get('method'),
                                 'research_mode': config.get('research_mode'), 'cache': config.get('cache')})
        save_new(args.out, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == 'summary':
        if args.repeat < 1:
            raise ValueError('repeat must be at least one')
        reports = [read_json(path) for path in args.scores]
        if any(r.get('task') not in TRACK_TASKS[args.track] for r in reports):
            raise ValueError('All score files must belong to the requested track.')
        references = {(r.get('corpus'), r.get('adjudications')) for r in reports}
        if len(references) > 1:
            raise ValueError('All runs must use the same corpus and adjudication file.')
        systems = {r.get('system') for r in reports}
        if None in systems or len(systems) != 1:
            raise ValueError('All runs must identify the same system and model.')
        conditions = {json.dumps(r.get('conditions'), sort_keys=True) for r in reports}
        if len(conditions) != 1:
            raise ValueError('All runs must use the same recorded tool settings.')
        run_ids = [r.get('run_id') for r in reports]
        if any(not x for x in run_ids) or len(set(run_ids)) != len(run_ids):
            raise ValueError('Each score file must have a distinct run ID.')
        per_task = {}
        for task in sorted(TRACK_TASKS[args.track]):
            runs = [r for r in reports if r['task'] == task]
            completed = [r for r in runs if r.get('status') == 'scored' and r.get('metrics') is not None]
            complete = len(runs) == args.repeat and len(completed) == args.repeat
            per_task[task] = {'runs': len(runs), 'scored_runs': len(completed),
                              'status': 'scored' if complete else 'incomplete',
                              'metrics': ({key: statistics.mean(r['metrics'][key] for r in completed)
                                           for key in ('ndcg_at_5','weighted_hit_at_5_percent','s_tier_hit_at_5')}
                                          if complete else None)}
        complete = all(x['status'] == 'scored' for x in per_task.values())
        result = {'track': args.track, 'system': next(iter(systems)), 'conditions': reports[0].get('conditions'),
                  'repetitions_per_task': args.repeat,
                  'status': 'scored' if complete else 'incomplete',
                  'macro_ndcg_at_5': statistics.mean(v['metrics']['ndcg_at_5'] for v in per_task.values()) if complete else None,
                  'reference_status': 'exploratory; current corpus and labels are under review',
                  'tasks': per_task}
        save_new(args.out, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        summaries = [read_json(p) for p in args.summaries]
        if {r.get('track') for r in summaries} != {'A', 'B', 'C'}:
            raise ValueError('Provide exactly one summary for each of tracks A, B and C.')
        if (len({r.get('system') for r in summaries}) != 1 or
                len({json.dumps(r.get('conditions'), sort_keys=True) for r in summaries}) != 1 or
                len({r.get('repetitions_per_task') for r in summaries}) != 1):
            raise ValueError('Track summaries must use the same system, tool settings and repeat count.')
        complete = all(r.get('status') == 'scored' and isinstance(r.get('macro_ndcg_at_5'), (int, float)) for r in summaries)
        result = {'system': summaries[0]['system'], 'conditions': summaries[0]['conditions'],
                  'status': 'scored' if complete else 'incomplete',
                  'track_ndcg_at_5': {r['track']:r.get('macro_ndcg_at_5') for r in summaries},
                  'overall_ndcg_at_5': statistics.mean(r['macro_ndcg_at_5'] for r in summaries) if complete else None,
                  'reference_status': 'exploratory; current corpus and labels are under review'}
        save_new(args.out, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(2)
