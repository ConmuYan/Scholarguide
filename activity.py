"""Read-only activity feed for the local UI.

Browses two kinds of artefacts under ``runs/``:

* single-run jobs: ``runs/<32-hex>/result.json`` (trace is inside the result);
* multi-turn sessions: ``runs/web/<session>/state.sqlite3`` and
  ``runs/chat/<name>.sqlite3`` (events, task versions, queries, ledger).

Nothing here mutates a database; sessions are opened with ``mode=ro``.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3

from common import RUNS_DIR
from recovery import JobLease, RecoveryUnsafe

JOB_RE = re.compile(r'[a-f0-9]{32}')
SESSION_RE = re.compile(r'[A-Za-z0-9_-]{1,80}')


def _latest_session_result(directory: Path) -> dict:
    files = []
    for path in directory.glob('turn-*/result.json'):
        try:
            files.append((int(path.parent.name.split('-', 1)[1]), path))
        except (IndexError, ValueError):
            continue
    if not files:
        return {}
    try:
        value = json.loads(max(files)[1].read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _session_runtime_status(directory, latest, db_status):
    """Prefer an actual in-flight turn over an older committed result."""
    try:
        runtime = json.loads((directory / 'runtime.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return latest.get('status') or ('running' if db_status == 'active' else db_status)
    if not isinstance(runtime, dict):
        return latest.get('status') or db_status
    if runtime.get('state') == 'running':
        committed = max((int(p.parent.name.split('-', 1)[1]) for p in directory.glob('turn-*/result.json')
                         if p.parent.name.split('-', 1)[1].isdigit()), default=0)
        if committed < int(runtime.get('expected_turn') or committed + 1):
            try:
                lease = JobLease(directory)
            except RecoveryUnsafe:
                return 'running'
            else:
                lease.close()
                return 'interrupted'
    if runtime.get('state') == 'failed':
        return 'failed'
    return latest.get('status') or db_status


def _read_session(db: Path) -> dict | None:
    try:
        con = sqlite3.connect(f'file:{db.as_posix()}?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        row = con.execute(
            'SELECT session_id, created_at, updated_at, status FROM sessions '
            'ORDER BY updated_at DESC LIMIT 1').fetchone()
        if row is None:
            con.close()
            return None
        session_id = row['session_id']
        events = con.execute('SELECT COUNT(*) AS n FROM events WHERE session_id = ?',
                             (session_id,)).fetchone()['n']
        revision = con.execute('SELECT COALESCE(MAX(revision), 0) AS r FROM task_versions '
                               'WHERE session_id = ?', (session_id,)).fetchone()['r']
        delivered = con.execute('SELECT COUNT(*) AS n FROM events WHERE session_id = ? '
                                'AND action = ?', (session_id, 'delivered')).fetchone()['n']
        last = con.execute('SELECT action, created_at FROM events WHERE session_id = ? '
                           'ORDER BY seq DESC LIMIT 1', (session_id,)).fetchone()
        task_row = con.execute('SELECT task_json FROM task_versions WHERE session_id = ? '
                               'ORDER BY revision DESC LIMIT 1', (session_id,)).fetchone()
        task = json.loads(task_row['task_json']) if task_row else {}
        con.close()
        latest = _latest_session_result(db.parent)
        meta = {}
        try:
            meta = json.loads((db.parent / 'meta.json').read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        files = list(db.parent.glob('turn-*/result.json'))
        message = latest.get('user_input') or ''
        return {'kind': 'session', 'id': session_id, 'db': str(db),
                'status': _session_runtime_status(db.parent, latest, row['status']),
                'created_at': row['created_at'], 'updated_at': row['updated_at'],
                'events': events, 'revision': revision,
                'turns': len(files) or delivered,
                'title': meta.get('display_title') or meta.get('title') or message or task.get('topic') or ('会话 ' + session_id[:8]),
                'message': message, 'topic': task.get('topic') or '',
                'mode': meta.get('mode') or latest.get('mode') or 'unknown',
                'source': meta.get('source') or (latest.get('config') or {}).get('source') or 'arxiv',
                'papers': len(latest.get('papers') or []),
                'last_action': last['action'] if last else '',
                'last_at': last['created_at'] if last else ''}
    except (sqlite3.Error, OSError):
        return None



def _normalise_event(action: str, payload: dict) -> tuple[str, dict]:
    """Legacy DBs used search/assess for both decisions and tool observations."""
    if (action in ('search', 'assess', 'ask', 'finish')
            and 'attempted' in payload
            and not any(key in payload for key in
                        ('query', 'status', 'returned', 'assessed', 'question'))):
        payload = dict(payload)
        payload['decision'] = action.upper()
        return 'decision', payload
    return action, payload



class ActivityFeed:
    def __init__(self, runs: Path = RUNS_DIR):
        self.runs = Path(runs)

    # -- index ---------------------------------------------------------
    def index(self, query: str = '') -> dict:
        runs: list[dict] = []
        for path in self.runs.glob('*/result.json'):
            job_id = path.parent.name
            if not JOB_RE.fullmatch(job_id):
                continue
            try:
                result = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if not isinstance(result, dict):
                continue
            metrics = result.get('metrics') or {}
            runs.append({'kind': 'run', 'id': job_id, 'status': result.get('status', ''),
                         'mode': result.get('mode', ''), 'method': result.get('method', ''),
                         'papers': len(result.get('papers') or []),
                         'started_at': result.get('started_at', ''),
                         'elapsed': metrics.get('elapsed_seconds'),
                         'llm_calls': metrics.get('llm_calls'),
                         'topic': (result.get('request') or {}).get('topic', ''),
                         'message': result.get('user_input', ''),
                         'events': len(result.get('trace') or []),
                         'title': result.get('display_title') or (result.get('request') or {}).get('topic') or ('运行 ' + job_id[:8]),
                         '_sort': result.get('started_at') or path.stat().st_mtime})
        runs.sort(key=lambda item: (item.get('_sort') or '', item['id']), reverse=True)
        for item in runs:
            item.pop('_sort', None)
        sessions: list[dict] = []
        candidates = list((self.runs / 'web').glob('*/state.sqlite3'))
        candidates += list((self.runs / 'chat').glob('*.sqlite3'))
        seen: set[str] = set()
        for db in candidates:
            info = _read_session(db)
            if info and info['id'] not in seen:
                seen.add(info['id'])
                sessions.append(info)
        sessions.sort(key=lambda item: item.get('updated_at') or '', reverse=True)
        total_runs, total_sessions = len(runs), len(sessions)
        total_events = (sum(item.get('events', 0) for item in sessions)
                        + sum(item.get('events', 0) for item in runs))
        completed = sum(item.get('status') in ('finished', 'complete') for item in sessions)
        completed += sum(item.get('status') == 'complete' for item in runs)
        q = (query or '').strip().casefold()
        def matches(item):
            if not q:
                return True
            return q in ' '.join(str(item.get(k) or '') for k in
                                 ('id', 'title', 'topic', 'message', 'mode', 'source', 'status')).casefold()
        filtered_runs = [item for item in runs if matches(item)]
        filtered_sessions = [item for item in sessions if matches(item)]
        filtered = {'runs': len(filtered_runs), 'sessions': len(filtered_sessions)}
        runs, sessions = filtered_runs, filtered_sessions
        return {'runs': runs[:100], 'sessions': sessions[:100],
                'totals': {'runs': total_runs, 'sessions': total_sessions,
                           'events': total_events,
                           'completed': completed,
                           'total': total_runs + total_sessions},
                'filtered': filtered,
                'truncated': {'runs': max(0, len(runs) - 100),
                              'sessions': max(0, len(sessions) - 100)}}

    # -- detail --------------------------------------------------------
    def run(self, job_id: str) -> dict | None:
        if not JOB_RE.fullmatch(job_id):
            return None
        path = self.runs / job_id / 'result.json'
        try:
            result = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        if not isinstance(result, dict):
            return None
        return {'kind': 'run', 'id': job_id, 'result': result}

    def _session_db(self, session_id: str) -> Path | None:
        if not SESSION_RE.fullmatch(session_id):
            return None
        candidates = list((self.runs / 'web').glob('*/state.sqlite3'))
        candidates += list((self.runs / 'chat').glob('*.sqlite3'))
        for db in candidates:
            info = _read_session(db)
            if info and info['id'] == session_id:
                return db
        return None

    def session(self, session_id: str) -> dict | None:
        db = self._session_db(session_id)
        if db is None:
            return None
        con = sqlite3.connect(f'file:{db.as_posix()}?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        try:
            info = _read_session(db)
            if info is None:
                return None
            row = con.execute('SELECT task_json FROM task_versions WHERE session_id = ? '
                              'ORDER BY revision DESC LIMIT 1', (session_id,)).fetchone()
            task = json.loads(row['task_json']) if row else {}
            events = []
            for event in con.execute('SELECT seq, revision, action, payload_json, created_at '
                                     'FROM events WHERE session_id = ? ORDER BY seq', (session_id,)):
                try:
                    payload = json.loads(event['payload_json'] or '{}')
                except ValueError:
                    payload = {'raw': event['payload_json']}
                action, payload = _normalise_event(event['action'], payload)
                events.append({'seq': event['seq'], 'revision': event['revision'],
                               'action': action, 'payload': payload,
                               'created_at': event['created_at']})
            queries = [dict(row) for row in con.execute(
                'SELECT revision, action_seq, query, returned_count, new_count, created_at '
                'FROM queries WHERE session_id = ? ORDER BY id', (session_id,))]
            cost = con.execute('SELECT COALESCE(SUM(reserved),0) AS r, '
                               'COALESCE(SUM(settled),0) AS s, COALESCE(SUM(refunded),0) AS f '
                               'FROM budget_ledger WHERE session_id = ?',
                               (session_id,)).fetchone()
            by_action = {row['action']: {'reserved': row['r'], 'settled': row['s'],
                                         'refunded': row['f']}
                         for row in con.execute(
                             'SELECT action, SUM(reserved) AS r, SUM(settled) AS s, '
                             'SUM(refunded) AS f FROM budget_ledger WHERE session_id = ? '
                             'GROUP BY action', (session_id,))}
            history = []
            for path in db.parent.glob('turn-*/result.json'):
                try:
                    number = int(path.parent.name.split('-', 1)[1])
                    result = json.loads(path.read_text(encoding='utf-8'))
                except (IndexError, ValueError, OSError):
                    continue
                if not isinstance(result, dict):
                    continue
                history.append({'turn': number, 'status': result.get('status', ''),
                                'message': result.get('user_input', ''),
                                'papers': len(result.get('papers') or []),
                                'revision': result.get('revision', 0),
                                'cost': result.get('cost') or (result.get('metrics') or {}).get('cost')})
            history.sort(key=lambda item: item['turn'])
            return {'kind': 'session', 'id': session_id, 'session': {k: info[k] for k in
                    ('status', 'created_at', 'updated_at', 'events', 'revision', 'turns',
                     'title', 'mode', 'source', 'papers')},
                    'task': task, 'history': history, 'events': events, 'queries': queries,
                    'spend': history[-1].get('cost') if history else None,
                    'cost': {'reserved': cost['r'], 'settled': cost['s'], 'refunded': cost['f'],
                             'by_action': by_action}}
        finally:
            con.close()
