"""Bounded anonymous publication probes; no model, keys or history access.

Run with a new --out directory. This checks source identity/status on a tiny
public sample. It is neither a relevance benchmark nor a recommendation run.
"""
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import Budget, Config, GuideError
from network import source_urlopen as urlopen
from research_policy import publication_matches
from source import AcademicSource


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists() and any(args.out.iterdir()):
        parser.error('--out must be new or empty')
    args.out.mkdir(parents=True, exist_ok=True)
    config = Config(source='openreview,openalex', cache=False, source_timeout=8,
                    task_timeout=75, openalex_key='', s2_key='', deepseek_search_key='')
    source = AcademicSource(config, Budget(75))
    report = {'checked_at': datetime.now(timezone.utc).isoformat(),
              'mode': 'anonymous public API; no model calls; no credentials or history loaded',
              'requests': [], 'checks': [], 'model_calls': 0}
    started = time.monotonic()

    def fetch_json(url, *, provider, **kwargs):
        if len(report['requests']) >= 6 or time.monotonic() - started >= 65:
            raise GuideError('Anonymous probe request/time bound reached.')
        if urlsplit(url).hostname not in ('api2.openreview.net', 'api.openalex.org'):
            raise GuideError('Unexpected probe domain.')
        entry = {'url': url, 'provider': provider,
                 'started_at': datetime.now(timezone.utc).isoformat()}
        report['requests'].append(entry)
        try:
            with urlopen(Request(url, headers={'User-Agent': 'ScholarGuide-publication-check/1.0'}), timeout=8) as response:
                payload = json.load(response)
                entry['http_status'] = response.status
            path = args.out / f'response-{len(report["requests"]):02}.json'
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            entry.update(success=True, response_file=path.name)
            return payload
        except Exception as exc:
            entry.update(success=False, error_type=type(exc).__name__)
            raise GuideError(f'{provider}: anonymous probe failed ({type(exc).__name__}).') from None
        finally:
            (args.out / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    source._json = fetch_json
    cases = [('openreview', {'venue': 'ICLR', 'accepted_only': True,
                            'presentation_types': ['poster'], 'year_from': 2025, 'year_to': 2025}),
             ('openalex', {'venue': 'Artificial Intelligence Review',
                           'publication_type': 'journal', 'accepted_only': True,
                           'year_from': 2024, 'year_to': 2026})]
    for name, task in cases:
        try:
            if name == 'openreview':
                items = source._openreview('in-context learning', 1,
                    {'openreview_venue_ids': ['ICLR.cc/2025/Conference']})
            else:
                items = source._openalex('large language model survey', 2,
                    venue='Artificial Intelligence Review', year_from=2024, year_to=2026,
                    constraints={'publication_type': 'journal', 'journal_issns': ['0269-2821', '1573-7462']})
            passed = bool(items) and all(publication_matches(item, task) for item in items)
            report['checks'].append({'provider': name, 'passed': passed, 'returned': len(items),
                'task': task, 'papers': [item.to_dict() for item in items],
                'boundary': 'Public metadata status/identity only. Article is not automatically a survey.'})
        except GuideError as exc:
            report['checks'].append({'provider': name, 'passed': False, 'error': str(exc), 'task': task})
    report.update(passed=all(check['passed'] for check in report['checks']),
                  elapsed_seconds=round(time.monotonic() - started, 3),
                  finished_at=datetime.now(timezone.utc).isoformat())
    (args.out / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('passed', 'elapsed_seconds', 'model_calls')}, ensure_ascii=False))
    for check in report['checks']:
        print(check['provider'], 'PASS' if check['passed'] else 'FAIL', check.get('returned', 0), check.get('error', ''))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
