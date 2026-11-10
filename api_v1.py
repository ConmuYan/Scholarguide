"""Versioned, application-backed JSON API. No evaluation or dataset dependency."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import uuid
from urllib.parse import parse_qsl, urlsplit

from common import GuideError, REMOTE_OUTCOMES, Request, utc_now

API_VERSION = 'v1'
IDENTITY = re.compile(r'^[a-f0-9]{32}$')


class ApiError(GuideError):
    def __init__(self, message, *, code='invalid_request', field=None, status=400, retryable=False):
        super().__init__(message)
        self.code, self.field, self.status, self.retryable = code, field, status, retryable


def error_payload(exc, status=400):
    error = {
        'code': getattr(exc, 'code', '') or ('not_found' if status == 404 else 'invalid_request'),
        'message': str(exc), 'field': getattr(exc, 'field', None),
        'retryable': bool(getattr(exc, 'retryable', status in (408, 429, 502, 503, 504)))}
    remote = getattr(exc, 'remote_outcome', None)
    if isinstance(remote, str) and remote in REMOTE_OUTCOMES:
        error['remote_outcome'] = remote
        error['retryable'] = False
    transaction = getattr(exc, 'transaction_outcome', None)
    if transaction in ('committed', 'unknown', 'rolled_back_or_unknown', 'not_started'):
        error['transaction_outcome'] = transaction
        error['retryable'] = False
    if getattr(exc, 'retry_safe', None) is False:
        error['retry_safe'] = False
        error['retryable'] = False
    return {'api_version': API_VERSION, 'error': error}


# Execution errors can contain remote text, queries or credentials. Persist only
# explicitly recognized classifications; never infer safety from their message.
_SAFE_EXECUTION_CODES = frozenset({'remote_outcome_unknown', 'remote_success_local_failure',
                                   'source_error', 'user_cancelled', 'turn_failed',
                                   'execution_error', 'transaction_outcome_unknown'})


def execution_error(value, *, default_code='execution_error'):
    def get(key):
        return value.get(key) if isinstance(value, dict) else getattr(value, key, None)
    code = get('code') or get('error_code')
    if not isinstance(code, str) or code not in _SAFE_EXECUTION_CODES:
        code = default_code
    error = {'code': code, 'message': 'Execution did not complete normally. Prior results are retained; do not retry automatically.',
             'field': None, 'retryable': False}
    remote = get('remote_outcome')
    if isinstance(remote, str) and remote in REMOTE_OUTCOMES:
        error['remote_outcome'] = remote
    transaction = get('transaction_outcome')
    if transaction in ('committed', 'unknown', 'rolled_back_or_unknown', 'not_started'):
        error['transaction_outcome'] = transaction
    if get('retry_safe') is False:
        error['retry_safe'] = False
    return error


def has_outcome(exc):
    return bool(getattr(exc, '_execution_failure', False)) or any(
        getattr(exc, key, None) is not None for key in
        ('remote_outcome', 'transaction_outcome', 'retry_safe'))


def normalize_event(event, *, operation_id='', session_id='', turn=None, index=0):
    payload = deepcopy(event.get('payload') or {
        key: value for key, value in event.items()
        if key not in ('seq', 'step', 'action', 'at', 'created_at', 'event_id')})
    seq = int(event.get('seq') or event.get('step') or index)
    return {'schema_version': 1, 'seq': seq,
            'event_id': event.get('event_id') or f'{operation_id or session_id}:{seq}',
            'operation_id': operation_id, 'session_id': session_id,
            'turn': turn, 'revision': event.get('revision', payload.get('revision')),
            'at': event.get('at') or event.get('created_at') or payload.get('at') or '',
            'action': event.get('action') or 'progress', 'payload': payload}


def _candidate_text(value, limit=4096):
    return value if isinstance(value, str) and len(value) <= limit else None


def _candidate_url(value):
    value = _candidate_text(value, 4096)
    if not value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.port == 0 or parts.fragment):
            return None
        # The OpenReview forum and group links use an id query. Unknown query
        # parameters can contain credentials, so they are not reflected.
        if parts.query and (parts.hostname not in ('openreview.net', 'api.openreview.net', 'api2.openreview.net')
                            or any(key != 'id' for key, _ in parse_qsl(parts.query, keep_blank_values=True))
                            or not parse_qsl(parts.query, keep_blank_values=True)):
            return None
        return value
    except ValueError:
        return None


def _candidate_strings(value, limit=32):
    if not isinstance(value, list):
        return None
    return [text for item in value[:limit] if (text := _candidate_text(item)) is not None]


def _candidate_evidence_value(value):
    if isinstance(value, str):
        return _candidate_text(value)
    if type(value) is bool or value is None:
        return value
    if type(value) is int and -(10 ** 12) <= value <= 10 ** 12:
        return value
    if isinstance(value, list):
        return _candidate_strings(value)
    if isinstance(value, dict):
        # Known source/venue fields only; never recurse into arbitrary objects.
        result = {}
        for key in ('id', 'display_name', 'name', 'type', 'issn_l', 'volume', 'pages'):
            text = _candidate_text(value.get(key))
            if text is not None:
                result[key] = text
        for key in ('issn', 'alternate_issns', 'alternate_names'):
            values = _candidate_strings(value.get(key))
            if values is not None:
                result[key] = values
            elif (text := _candidate_text(value.get(key))) is not None:
                result[key] = text
        url = _candidate_url(value.get('url'))
        if url is not None:
            result['url'] = url
        return result
    return None


def _candidate_record(value):
    if not isinstance(value, dict):
        return None
    result = {}
    for key in ('type', 'status', 'presentation', 'venue_id', 'venue_name', 'date',
                'date_precision', 'doi', 'document_type', 'observed_at', 'verified_at',
                'accepted_at', 'first_public_at', 'decision', 'verification_reason'):
        text = _candidate_text(value.get(key))
        if text is not None:
            result[key] = text
    for key in ('verified', 'identity_verified', 'retracted', 'deleted'):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    for key in ('issn', 'publication_types', 'decisions'):
        values = _candidate_strings(value.get(key))
        if values is not None:
            result[key] = values
        elif key == 'issn' and (text := _candidate_text(value.get(key))) is not None:
            result[key] = text
    if isinstance(value.get('evidence'), list):
        result['evidence'] = []
        for evidence in value['evidence'][:16]:
            if not isinstance(evidence, dict):
                continue
            item = {}
            for key in ('source', 'field'):
                text = _candidate_text(evidence.get(key))
                if text is not None:
                    item[key] = text
            url = _candidate_url(evidence.get('url'))
            if url is not None:
                item['url'] = url
            evidence_field = evidence.get('field')
            allowed_fields = ('repository', 'primary_location.source', 'type', 'doi', 'is_retracted',
                              'publicationVenue', 'publicationTypes', 'journal', 'externalIds.DOI',
                              'content.venueid', 'content.venue', 'group.id', 'decision',
                              'citation_conference_title', 'citation_doi')
            if isinstance(evidence_field, str) and evidence_field in allowed_fields and 'value' in evidence:
                clean = _candidate_evidence_value(evidence['value'])
                if clean is not None or evidence['value'] is None:
                    item['value'] = clean
            result['evidence'].append(item)
        if len(value['evidence']) > 16:
            result['verified'] = False
    return result


def _candidate_publication(value):
    result = _candidate_record(value)
    if result is None:
        return None
    if type(value.get('schema_version')) is int and value['schema_version'] == 1:
        result['schema_version'] = 1
    if isinstance(value.get('records'), list):
        result['records'] = [record for item in value['records'][:16]
                             if (record := _candidate_record(item)) is not None]
    if isinstance(value.get('conflicts'), list):
        result['conflicts'] = []
        for conflict in value['conflicts'][:16]:
            if not isinstance(conflict, dict):
                continue
            item = {}
            venue = _candidate_text(conflict.get('venue_id'))
            statuses = _candidate_strings(conflict.get('statuses'))
            if venue is not None:
                item['venue_id'] = venue
            if statuses is not None:
                item['statuses'] = statuses
            result['conflicts'].append(item)
    if ((isinstance(value.get('records'), list) and len(value['records']) > 16)
            or (isinstance(value.get('conflicts'), list) and len(value['conflicts']) > 16)):
        result['verified'] = False
        for record in result.get('records', []):
            record['verified'] = False
    return result


def public_candidates(raw):
    """Expose bounded discovery metadata, never arbitrary remote objects."""
    if not isinstance(raw, list):
        return []
    result = []
    for candidate in raw[:20]:
        if not isinstance(candidate, dict):
            continue
        identity = _candidate_text(candidate.get('id') or candidate.get('paper_id'))
        title = _candidate_text(candidate.get('title'))
        if not identity or title is None:
            continue
        item = {'paper_id': identity, 'title': title}
        for key in ('id', 'venue', 'doi', 'arxiv_id', 'source', 'abstract_source', 'snippet', 'abstract'):
            text = _candidate_text(candidate.get(key), 20000 if key == 'abstract' else 4096)
            if text is not None:
                item[key] = text
        for key in ('url', 'open_access_url'):
            url = _candidate_url(candidate.get(key))
            if url is not None:
                item[key] = url
        for key in ('authors', 'sources'):
            values = _candidate_strings(candidate.get(key))
            if values is not None:
                item[key] = values
        if type(candidate.get('year')) is int and 1900 <= candidate['year'] <= 2100:
            item['year'] = candidate['year']
        for key in ('metadata_only', 'provenance_conflict'):
            if type(candidate.get(key)) is bool:
                item[key] = candidate[key]
        publication = _candidate_publication(candidate.get('publication'))
        if publication is not None:
            item['publication'] = publication
        if len(json.dumps(item, ensure_ascii=True)) > 100000:
            continue
        result.append(item)
    return result


def public_result(raw):
    if not isinstance(raw, dict):
        return None
    papers = []
    fields = ('id', 'title', 'url', 'authors', 'year', 'venue', 'doi', 'arxiv_id',
              'sources', 'source', 'abstract', 'abstract_source', 'open_access_url',
              'metadata_only', 'snippet', 'publication', 'provenance_conflict',
              'paper_type', 'category_status', 'fit_score', 'reason', 'evidence_quote',
              'evidence_verified', 'relevance', 'citation_count', 'citation_source', 'citation_observed_at')
    for rank, paper in enumerate(raw.get('papers') or (), start=1):
        item = {key: deepcopy(paper.get(key)) for key in fields
                if key in paper and key not in ('url', 'open_access_url', 'publication')}
        for key in ('url', 'open_access_url'):
            safe_url = _candidate_url(paper.get(key))
            if safe_url is not None:
                item[key] = safe_url
        publication = _candidate_publication(paper.get('publication'))
        if publication is not None:
            item['publication'] = publication
        item['rank'] = rank
        item['paper_id'] = paper.get('id') or paper.get('paper_id')
        item['evidence'] = ([{'quote': paper['evidence_quote'],
                             'source': paper.get('abstract_source') or paper.get('source'),
                             'quote_verified': bool(paper.get('evidence_verified'))}]
                            if paper.get('evidence_quote') else [])
        papers.append(item)
    metrics = raw.get('metrics') or {}
    return {'status': raw.get('status', 'unknown'),
            'stop_reason': raw.get('stop_reason') or '',
            'request': deepcopy(raw.get('request') or {}), 'papers': papers,
            'returned_count': len(papers), 'candidates': public_candidates(raw.get('candidates')),
            'gaps': deepcopy(raw.get('gaps') or []),
            'warnings': deepcopy(raw.get('warnings') or []),
            'question': raw.get('question') or '',
            'usage': deepcopy(metrics), 'cost': deepcopy(raw.get('cost') or metrics.get('cost')),
            'research_brief': deepcopy(raw.get('research_brief')),
            'research_coverage': deepcopy(raw.get('research_coverage')),
            'diagnosis': deepcopy(raw.get('diagnosis') or {}),
            'revision': raw.get('revision'), 'mode': raw.get('mode')}


class OperationStore:
    """Durable admission and idempotency, separate from Agent business state."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as con:
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('CREATE TABLE IF NOT EXISTS operations ('
                        'id TEXT PRIMARY KEY, request_key TEXT UNIQUE, fingerprint TEXT NOT NULL, '
                        'body TEXT NOT NULL)')

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=15)
        try:
            with con:
                yield con
        finally:
            con.close()

    def reserve(self, key, kind, payload):
        if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 160
                                or any(ord(c) < 32 for c in key)):
            raise ApiError('client_request_id must be 1–160 printable characters.', field='client_request_id')
        digest = hashlib.sha256(json.dumps([kind, payload], sort_keys=True,
                                          ensure_ascii=False).encode()).hexdigest()
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if key:
                row = con.execute('SELECT fingerprint, body FROM operations WHERE request_key=?', (key,)).fetchone()
                if row:
                    if row[0] != digest:
                        raise ApiError('The idempotency key was already used for a different request.',
                                       code='idempotency_conflict', status=409, field='client_request_id')
                    return json.loads(row[1]), False
            body = {'id': uuid.uuid4().hex, 'kind': kind, 'created_at': utc_now(),
                    'target_id': '', 'turn': 1, 'start_seq': 0, 'admission': 'pending'}
            con.execute('INSERT INTO operations VALUES(?,?,?,?)',
                        (body['id'], key, digest, json.dumps(body)))
            return body, True

    def save(self, body):
        with self.connect() as con:
            con.execute('UPDATE operations SET body=? WHERE id=?', (json.dumps(body, ensure_ascii=False), body['id']))

    def get(self, identity):
        if not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
            return None
        with self.connect() as con:
            row = con.execute('SELECT body FROM operations WHERE id=?', (identity,)).fetchone()
        return json.loads(row[0]) if row else None


class ProductApi:
    def __init__(self, jobs, chat, directory):
        self.jobs, self.chat = jobs, chat
        self.store = OperationStore(Path(directory) / 'operations.sqlite3')

    @staticmethod
    def _input(raw, kind):
        if not isinstance(raw, dict):
            raise ApiError('Request must be a JSON object.')
        allowed = {'query', 'request', 'message', 'count', 'source', 'providers', 'filters',
                   'request_data', 'mode', 'stream', 'client_request_id', 'session_id',
                   'reading_goal', 'research_mode', 'scenario', 'cache'}
        unknown = set(raw) - allowed
        if unknown:
            raise ApiError('Unknown request fields: ' + ', '.join(sorted(unknown)), field=sorted(unknown)[0])
        message = raw.get('message', raw.get('query', raw.get('request', '')))
        request_data = raw.get('request_data')
        if request_data is not None:
            if not isinstance(request_data, dict):
                raise ApiError('request_data must be an object.', field='request_data')
            unknown_task = set(request_data) - set(Request.__dataclass_fields__)
            if unknown_task:
                raise ApiError('Unknown structured task fields: ' + ', '.join(sorted(unknown_task)), field='request_data')
            Request.from_dict(request_data)
            message = message or request_data.get('topic', '')
        if not isinstance(message, str) or not message.strip() or len(message) > 2000:
            raise ApiError('Provide a nonempty query/message of at most 2000 characters.', field='message')
        from quantities import quantity_constraints
        quantity_constraints(message)  # reject explicit over-limit requests before admission
        mode = raw.get('mode', 'live')
        if mode not in ('live', 'demo'):
            raise ApiError('mode must be live or demo.', field='mode')
        source = raw.get('source', '' if kind == 'chat' and raw.get('session_id')
                         else ('multi' if mode == 'live' else 'arxiv'))
        if 'providers' in raw:
            providers = raw['providers']
            if (not isinstance(providers, list) or not providers
                    or any(not isinstance(name, str) or not name for name in providers)):
                raise ApiError('providers must be a nonempty array of source names.', field='providers')
            source = ','.join(dict.fromkeys(providers))
        if not isinstance(source, str):
            raise ApiError('source must be a string.', field='source')
        from common import SOURCE_NAMES, source_names
        if any(name not in SOURCE_NAMES for name in source_names(source)):
            raise ApiError('Unknown source.', field='source')
        if type(raw.get('stream', True)) is not bool or type(raw.get('research_mode', True)) is not bool:
            raise ApiError('stream and research_mode must be booleans.')
        if 'cache' in raw and type(raw['cache']) is not bool:
            raise ApiError('cache must be a boolean.', field='cache')
        filters = raw.get('filters', {})
        if not isinstance(filters, dict):
            raise ApiError('filters must be an object.', field='filters')
        permitted = set(Request.__dataclass_fields__) - {'topic', 'unsupported'}
        if set(filters) - permitted:
            raise ApiError('Unknown filter fields: ' + ', '.join(sorted(set(filters) - permitted)), field='filters')
        constraints = dict(filters)
        if 'count' in raw:
            if type(raw['count']) is not int or not 1 <= raw['count'] <= 10:
                raise ApiError('Each recommendation supports 1–10 papers; default is 5.', field='count')
            constraints['target_count'] = raw['count']
        if raw.get('reading_goal'):
            constraints['reading_goal'] = raw['reading_goal']
        # Validate combinations without replacing the natural-language research topic.
        if constraints:
            Request.from_dict({'topic': 'validation', **constraints})
        if mode == 'demo' and (constraints or request_data is not None):
            raise ApiError('The fixed offline demo does not accept custom filters or counts; use live mode.', field='mode')
        session_id = raw.get('session_id', '')
        if not isinstance(session_id, str) or (session_id and not IDENTITY.fullmatch(session_id)):
            raise ApiError('Invalid session_id.', field='session_id')
        if kind == 'search' and session_id:
            raise ApiError('Search creates a new workspace; use chat to continue one.', field='session_id')
        return {'message': message.strip(), 'request': message.strip(), 'mode': mode,
                'source': source, 'stream': raw.get('stream', True),
                'scenario': raw.get('scenario', 'followup'), 'request_data': request_data,
                'research_mode': raw.get('research_mode', True),
                'constraints': constraints, 'cache': raw.get('cache', True),
                'session_id': session_id}

    def submit(self, kind, raw, *, key=None):
        data = self._input(raw, kind)
        key = key or raw.get('client_request_id')
        with self.store.lock:
            operation, fresh = self.store.reserve(key, kind, data)
            if not fresh:
                return self.snapshot(operation['id'])
            try:
                if kind == 'search':
                    # Compatibility Web and public API use this same Jobs application path.
                    request = {'request': data['request'], 'mode': data['mode'], 'source': data['source'],
                               'method': 'A', 'cache': data['cache'], 'research_mode': data['research_mode'],
                               'request_constraints': data['constraints']}
                    if data['mode'] == 'demo':
                        request = {'mode': 'demo', 'method': 'A', 'scenario': data['scenario']}
                    if data['request_data'] is not None:
                        request['request_data'] = data['request_data']
                    operation['target_id'] = self.jobs.submit(request)
                else:
                    sid = data.pop('session_id')
                    if sid:
                        before = self.chat.snapshot(sid)
                        if before is None:
                            raise ApiError('Session not found.', code='not_found', status=404)
                        operation['turn'] = int(before.get('turns') or len(before.get('history') or [])) + 1
                        existing = self.chat.events(sid) or []
                        operation['start_seq'] = max((int(row.get('seq') or 0) for row in existing), default=0)
                        response = self.chat.send(sid, data, operation_id=operation['id'])
                    else:
                        response = self.chat.create(data, operation_id=operation['id'])
                    operation['target_id'] = response['session_id']
                    operation['turn'] = int(response.get('turn') or operation['turn'])
                    operation['start_seq'] = int(response.get('start_seq') or 0)
                operation['admission'] = 'accepted'
                self.store.save(operation)
            except Exception as exc:
                operation.update(admission='failed', error=(execution_error(exc) if has_outcome(exc) else
                                                            error_payload(exc, getattr(exc, 'status', 400))['error']))
                self.store.save(operation)
                raise
        return self.snapshot(operation['id'])

    def snapshot(self, identity):
        op = self.store.get(identity)
        if op is None:
            return None
        result, error, session_id, recovery = None, op.get('error'), '', None
        if op['admission'] == 'failed':
            state = 'failed'
        elif op['admission'] == 'pending':
            state = 'admission_unknown'
            error = {'code': 'admission_unknown', 'message': 'Submission outcome is not confirmed. This key will not dispatch a duplicate.',
                     'field': None, 'retryable': False}
        elif op['kind'] == 'search':
            value = self.jobs.snapshot(op['target_id'])
            if value is None:
                state = 'interrupted'
                error = {'code': 'operation_unavailable', 'message': 'Saved operation is unavailable.', 'retryable': False, 'field': None}
            else:
                raw = value.get('result')
                session_id = (raw or {}).get('chat_session_id') or value.get('session_id') or ''
                state = {'done': 'completed', 'error': 'failed', 'needs_retry': 'interrupted'}.get(value['state'], value['state'])
                result = public_result(raw)
                recovery = value.get('recovery')
                if value.get('error'):
                    error = execution_error(value.get('error_details') or {})
        else:
            session_id = op['target_id']
            value = self.chat.snapshot(session_id)
            raw = self.chat.turn_result(session_id, op['turn'])
            operation_state = self.chat.operation_snapshot(session_id, op['id'])
            terminal_state = (operation_state or {}).get('state')
            matches = raw is not None and (raw.get('operation_id') in (None, op['id']))
            if terminal_state == 'failed':
                state = 'failed'
                error = execution_error(operation_state.get('error_details') or operation_state, default_code='turn_failed')
                if operation_state.get('persistence_confirmed') is False:
                    error['persistence_confirmed'] = False
            elif terminal_state == 'completed' and matches and operation_state.get('persistence_confirmed') is not False:
                state = 'completed'
                result = public_result(raw)
            elif terminal_state is None and matches and not raw.get('operation_id'):
                # Legacy histories did not record operation identities.
                state = 'completed'
                result = public_result(raw)
            elif terminal_state == 'running' and value and (value.get('running') or value.get('status') == 'running'):
                state = 'running'
            else:
                state = 'interrupted'
                error = {'code': 'turn_interrupted',
                         'message': 'Turn outcome or persistence is not confirmed; do not retry automatically.',
                         'field': None, 'retryable': False, 'persistence_confirmed': False}
        base = f'/api/v1/operations/{identity}'
        return {'api_version': API_VERSION, 'operation_id': identity,
                'session_id': session_id, 'turn': op['turn'], 'kind': op['kind'],
                'state': state, 'created_at': op['created_at'],
                'links': {'self': base, 'events': base + '/events', 'cancel': base + '/cancel', 'result': base + '/result'},
                'result': result, 'error': error, 'recovery': recovery}

    def events(self, identity, after=0):
        op = self.store.get(identity)
        if op is None:
            return None
        if op['kind'] == 'search':
            value = self.jobs.snapshot(op['target_id']) if op.get('target_id') else None
            rows = (value or {}).get('trace') or []
            sid = ((value or {}).get('result') or {}).get('chat_session_id') or ''
        else:
            sid = op['target_id']
            rows = self.chat.events(sid, max(after, op.get('start_seq', 0))) or []
            raw = self.chat.turn_result(sid, op['turn'])
            terminal = self.chat.operation_snapshot(sid, op['id']) or {}
            end = terminal.get('end_seq')
            if end is None and terminal.get('state') != 'failed' and (raw or {}).get('operation_id') in (None, identity):
                end = (raw or {}).get('event_seq_end')
            if end is not None:
                rows = [row for row in rows if int(row.get('seq') or 0) <= int(end)]
        normalized = [normalize_event(row, operation_id=identity, session_id=sid, turn=op['turn'], index=index)
                      for index, row in enumerate(rows, 1)]
        return [row for row in normalized if row['seq'] > after]

    def cancel(self, identity):
        op = self.store.get(identity)
        if op is None:
            return None
        if op['kind'] == 'search':
            value = self.jobs.cancel(op['target_id'])
        else:
            value = self.chat.cancel(op['target_id'], operation_id=identity)
        return {'api_version': API_VERSION, 'operation_id': identity, **(value or {'accepted': False})}
