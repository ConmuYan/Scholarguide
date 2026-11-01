"""Local browser UI; stdlib server and a bounded background job queue.

Model credentials stay in Python. Only public configuration, progress, and results
are returned to the browser. The server binds exclusively to loopback.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import os
from pathlib import Path
import shutil
import re
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid
import webbrowser

from activity import ActivityFeed
from providers import ProviderStore
from common import Budget, CancellationToken, Config, DATA_DIR, GuideError, Paper, ROOT, RUNS_DIR, SOURCE_NAMES, Request, source_names, utc_now
from diagnosis import diagnose
from demo import ChatDemoLLM, DemoSource, demo_run
from report import bibtex, markdown, save_run
from session import PROMPT_VERSION, ChatSession, DEFAULT_LIMITS, FeedbackBudgetExhausted
from state import StateStore
from workflow import (run, validate_request_constraints, validate_assessments,
                      grounded_assessments, select_papers, coverage)
from model_identity import research_model_identity
from recovery import (SearchJournal, JournalSource, JournalLLM, JobLease,
                      RecoveryUnsafe, atomic_json, execution_identity)
from source import AcademicSource
from llm import JsonLLM
from network import execution_context
from api_v1 import ProductApi, ApiError, error_payload, normalize_event, execution_error, has_outcome, public_candidates
from execution_events import bind_events
from memory_store import MemoryStore, MemoryError as PreferenceError, RevisionConflict, _key, _encode, _revision, _number

STATIC = ROOT / 'web'


class TurnRunning(GuideError):
    code = 'turn_running'


class SessionArchived(GuideError):
    code = 'session_archived'


class DeletionUnavailable(GuideError):
    code = 'deletion_unavailable'


def _deletion_unavailable():
    raise DeletionUnavailable('删除功能暂不可用：尚未建立跨进程安全删除协议；数据未删除。')



def _source_value(value, *, default='arxiv'):
    from common import source_names
    if value is None or value == '':
        return default
    if not isinstance(value, str) or any(name not in SOURCE_NAMES for name in source_names(value)):
        raise GuideError('数据源必须是已配置的来源名称或 multi。')
    return value


def _constraints(value):
    if not isinstance(value, dict):
        raise GuideError('筛选条件必须是 JSON 对象。')
    allowed = set(Request.__dataclass_fields__) - {'topic', 'unsupported'}
    unknown = set(value) - allowed
    if unknown:
        raise GuideError('未知筛选字段：' + ', '.join(sorted(unknown)))
    # Partial natural-language constraints may not yet contain the parsed count.
    check = {'topic': 'constraint validation', 'target_count': value.get('target_count', 10), **value}
    parsed = asdict(Request.from_dict(check))
    return {key: deepcopy(parsed[key]) for key in value}


def validate_input(data: dict) -> dict:
    if not isinstance(data, dict):
        raise GuideError('请求必须是 JSON 对象。')
    mode, method = data.get('mode', 'live'), data.get('method', 'A')
    if mode not in ('live', 'demo') or method not in ('S', 'A'):
        raise GuideError('运行模式或检索方式无效。')
    research_mode = data.get('research_mode', True)
    cache = data.get('cache', True)
    if type(research_mode) is not bool or type(cache) is not bool:
        raise GuideError('research_mode 和 cache 必须是布尔值。')
    source = _source_value(data.get('source'))
    goal = data.get('reading_goal') or ''
    if not isinstance(goal, str) or goal not in ('', 'auto', 'orientation', 'comparison', 'frontier', 'general'):
        raise GuideError('阅读目标无效。')
    if mode == 'demo':
        if 'request_constraints' in data or 'request_data' in data:
            raise GuideError('固定离线演示不接受研究筛选约束；请使用在线搜索。')
        scenario = data.get('scenario', 'followup')
        if scenario not in ('followup', 'satisfied', 'insufficient'):
            raise GuideError('演示场景无效。')
        return {'mode': mode, 'method': method, 'scenario': scenario}
    request = data.get('request', '')
    structured = data.get('request_data')
    if structured is not None:
        if not isinstance(structured, dict):
            raise GuideError('request_data 必须是对象。')
        Request.from_dict(structured)
        request = request or structured.get('topic') or ''
    if not isinstance(request, str) or not request.strip() or len(request) > 2000:
        raise GuideError('请填写研究需求，长度不超过 2000 字。')
    from quantities import quantity_constraints
    quantity_constraints(request)
    validated = {'mode': mode, 'method': method, 'source': source, 'cache': cache,
                 'request': request.strip(), 'reading_goal': goal, 'research_mode': research_mode}
    if structured is not None:
        validated['request_data'] = deepcopy(structured)
    if 'request_constraints' in data:
        validated['request_constraints'] = _constraints(data['request_constraints'])
    if 'research_preferences' in data:
        preferences = data['research_preferences']
        fields = ('venue_scope', 'publication_type', 'accepted_only', 'presentation_types')
        if not isinstance(preferences, dict) or set(preferences) - set(fields):
            raise GuideError('研究筛选字段无效。')
        validated['research_preferences'] = _constraints(preferences)
    return validated


class Jobs:
    def __init__(self, output: Path = RUNS_DIR, runner=None, importer=None,
                 providers: ProviderStore | None = None):
        self.output = output
        self.providers = providers
        self.sources = None
        self.session_service = None
        self.runner = runner or self._execute
        self.recoverable_runner = runner is None
        self.importer = importer
        self.lock = threading.Lock()
        self.items: dict[str, dict] = {}
        self.cancel_tokens: dict[str, CancellationToken] = {}
        self.requests: dict[str, dict] = {}
        self.leases: dict[str, JobLease] = {}
        self.active: str | None = None

    def _config(self, data):
        base = self.providers.config(Config.from_env()) if self.providers else Config.from_env()
        config = replace(base, source=data.get('source', base.source), cache=data.get('cache', base.cache),
                         research_mode=data.get('research_mode', True) if data.get('mode') != 'demo' else False)
        return self.sources.configure(config) if self.sources is not None else config

    def _persist(self, job_id):
        atomic_json(self.output / job_id / 'job.json',
                    {'version': 1, 'input': self.requests[job_id],
                     'recoverable_runner': self.recoverable_runner,
                     'job': self.items[job_id]})

    def _execute(self, data: dict, progress) -> dict:
        base = self._config(data)
        budget = Budget(base.task_timeout, cancel_token=data.get('_cancel_token') or CancellationToken())
        if data['mode'] == 'demo':
            return demo_run(data['scenario'], data['method'], progress=progress, config=base,
                            budget=budget)
        config = base
        if config.research_mode and self.session_service is not None:
            # Live Web Search and Chat execute the same persistent LangGraph Agent.
            def linked(session_id):
                with self.lock:
                    self.items[data['_job_id']]['session_id'] = session_id
                    self._persist(data['_job_id'])
            constraints = {**data.get('request_constraints', {}), **data.get('research_preferences', {})}
            payload = self.session_service.create({
                'mode': 'live', 'message': data['request'], 'source': config.source,
                'stream': False, 'research_mode': True, 'constraints': constraints,
                'goal_hint': data.get('reading_goal') or None,
                'request_data': data.get('request_data')}, progress=progress,
                cancel_token=budget.cancel_token, on_created=linked)
            result = self.session_service.turn_result(payload['session_id'], 1)
            result['chat_session_id'] = payload['session_id']
            result['execution_engine'] = 'langgraph'
            result['method'] = 'V2'
            return result
        # Managed jobs have a durable identity. The synchronous adapter remains
        # usable by embedders/tests without manufacturing a persisted user job.
        journal = (SearchJournal(self.output / data['_job_id'] / 'calls.json', config,
                                 resume=bool(data.get('_resume'))) if data.get('_job_id') else None)
        source = AcademicSource(config, budget)
        llm = JsonLLM(config, budget)
        if journal is not None:
            source = JournalSource(source, journal, budget)
            llm = JournalLLM(llm, journal, budget)
        with execution_context(cancelled=lambda: budget.cancel_token.requested):
            result = run(data['request'], config, data['method'], progress=progress, budget=budget,
                         source=source, llm=llm, goal_hint=data.get('reading_goal') or None,
                         request_data=data.get('request_data'),
                         request_constraints=data.get('request_constraints'),
                         research_preferences=data.get('research_preferences'))
        if journal is not None:
            result.setdefault('metrics', {})['recovery'] = {
                'replayed_calls': journal.replayed_calls,
                'new_calls': len(journal.document['calls']) - journal.initial_calls,
                'policy': 'reuse_recorded_outputs; never_replay_unknown_external_outcomes'}
        return result

    def submit(self, data: dict) -> str:
        data = validate_input(data)
        with self.lock:
            if self.active is not None:
                raise GuideError('已有任务正在运行，请等待完成后再提交。')
            while len(self.items) >= 20:
                oldest = next(iter(self.items))
                del self.items[oldest]
                self.cancel_tokens.pop(oldest, None)
                self.requests.pop(oldest, None)
            job_id = uuid.uuid4().hex
            token = CancellationToken()
            self.leases[job_id] = JobLease(self.output / job_id)
            self.cancel_tokens[job_id] = token
            self.requests[job_id] = deepcopy(data)
            data['_cancel_token'] = token
            data['_job_id'] = job_id
            self.items[job_id] = {'id': job_id, 'state': 'running', 'created_at': utc_now(),
                                  'mode': data['mode'], 'trace': [], 'result': None, 'error': None,
                                  'cancel_requested': False, 'cancellation_state': ''}
            try:
                self._persist(job_id)
            except Exception:
                self.leases.pop(job_id).close()
                self.items.pop(job_id, None)
                self.cancel_tokens.pop(job_id, None)
                self.requests.pop(job_id, None)
                raise
            self.active = job_id
        threading.Thread(target=self._worker, args=(job_id, data), daemon=True).start()
        return job_id

    def _worker(self, job_id: str, data: dict) -> None:
        def progress(event, checkpoint=None):
            with self.lock:
                row = deepcopy(event)
                row['seq'] = len(self.items[job_id]['trace']) + 1
                row['step'] = row['seq']
                row.setdefault('at', utc_now())
                row['event_id'] = f'{job_id}:{row["seq"]}'
                self.items[job_id]['trace'].append(row)
                self.items[job_id]['stage'] = event.get('action') or ''
                self.items[job_id]['updated_at'] = utc_now()
                if checkpoint is not None:
                    self.items[job_id]['checkpoint'] = deepcopy(checkpoint)
                self._persist(job_id)
        progress.accepts_checkpoint = True
        try:
            result = self.runner(data, progress)
            chat_session_id = result.get('chat_session_id')
            if self.importer is not None and not chat_session_id:
                try:
                    chat_session_id = self.importer(result, job_id)
                except Exception as exc:  # import failure must not destroy the saved run
                    result.setdefault('warnings', []).append(
                        f'搜索已完成，但未能创建 Chat 会话（{type(exc).__name__}）。')
            if chat_session_id:
                result['chat_session_id'] = chat_session_id
            with self.lock:
                if self.items[job_id]['trace']:
                    result['trace'] = deepcopy(self.items[job_id]['trace'])
            try:
                save_run(result, self.output / job_id)
            except OSError:
                result.setdefault('warnings', []).append('本次结果未能保存到磁盘；请从网页下载报告。')
            with self.lock:
                self.items[job_id].update(state='done', result=deepcopy(result),
                                          trace=deepcopy(result.get('trace', [])),
                                          cancellation_state=('stopped' if result.get('stop_reason') == 'user_cancelled' else ''))
                self.items[job_id].pop('checkpoint', None)
                self.items[job_id].pop('recovery', None)
                self._persist(job_id)
        except Exception as exc:
            details = execution_error(exc)
            try:
                atomic_json(self.output / job_id / 'failure.json', {'error': details, 'at': utc_now()})
            except OSError:
                pass
            message = '任务未完成，已保留此前结果；请勿自动重试。'
            with self.lock:
                item = self.items[job_id]
                item.update(state='error', error=message, error_details=details)
                if 'checkpoint' in item:
                    # Checkpoints are internal workflow state, not a validated public result.
                    item['recovery'] = {'checkpoint_available': True, 'can_resume': False,
                                        'reason': 'execution_failed',
                                        'message': 'Checkpoint retained for inspection; automatic retry is unsafe.'}
                self._persist(job_id)
        finally:
            with self.lock:
                self.active = None
                lease = self.leases.pop(job_id, None)
                if lease:
                    lease.close()

    def cancel(self, job_id: str) -> dict | None:
        with self.lock:
            item = self.items.get(job_id)
            if item is not None:
                accepted = item['state'] == 'running'
                if accepted:
                    self.cancel_tokens[job_id].request()
                    sid = item.get('session_id')
                    if sid and self.session_service is not None:
                        self.session_service.cancel(sid)
                    item.update(cancel_requested=True, cancellation_state='stopping')
                    self._persist(job_id)
                return {'id': job_id, 'accepted': accepted,
                        'cancel_requested': bool(item.get('cancel_requested')),
                        'cancellation_state': item.get('cancellation_state') or ''}
        snapshot = self.snapshot(job_id)
        if snapshot is None:
            return None
        return {'id': job_id, 'accepted': False,
                'cancel_requested': bool(snapshot.get('cancel_requested')),
                'cancellation_state': snapshot.get('cancellation_state') or ''}

    def delete(self, job_id: str) -> dict | None:
        """Fail closed until execution and deletion share a durable process lock."""
        _deletion_unavailable()

    def snapshot(self, job_id: str) -> dict | None:
        with self.lock:
            value = self.items.get(job_id)
            if value is not None:
                return deepcopy(value)
        if not re.fullmatch(r'[a-f0-9]{32}', job_id):
            return None
        path = self.output / job_id / 'result.json'
        try:
            result = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return self._interrupted(job_id)
        if not isinstance(result, dict):
            return None
        return {'id': job_id, 'state': 'done', 'created_at': result.get('started_at'),
                'mode': result.get('mode'), 'trace': result.get('trace', []),
                'result': result, 'error': None,
                'cancel_requested': result.get('stop_reason') == 'user_cancelled',
                'cancellation_state': 'stopped' if result.get('stop_reason') == 'user_cancelled' else ''}

    @staticmethod
    def _checkpoint_preview(document, item):
        """Rebuild a bounded preview from internally consistent workflow state."""
        try:
            if document.get('recoverable_runner') is not True or item.get('session_id'):
                return None
            cp = item.get('checkpoint')
            if not isinstance(cp, dict):
                return None
            raw_candidates, raw_papers, raw_assessments = (cp.get('candidates'), cp.get('papers'),
                                                           cp.get('assessments'))
            if (not isinstance(raw_candidates, list) or len(raw_candidates) > 20
                    or not isinstance(raw_papers, list) or len(raw_papers) > 10
                    or not isinstance(raw_assessments, dict) or len(raw_assessments) > 20):
                return None
            request_raw = cp.get('request')
            request = Request.from_dict(request_raw)
            if asdict(request) != request_raw or request.unsupported:
                return None
            data = document.get('input') or {}
            if not isinstance(data, dict):
                return None
            explicit = {}
            for field in ('request_data', 'request_constraints', 'research_preferences'):
                constraints = data.get(field) or {}
                if not isinstance(constraints, dict):
                    return None
                explicit.update({key: value for key, value in constraints.items()
                                 if key in Request.__dataclass_fields__})
            if data.get('reading_goal') in ('orientation', 'comparison', 'frontier', 'general'):
                explicit['reading_goal'] = data['reading_goal']
            expected = asdict(Request.from_dict({**request_raw, **explicit}))
            if expected != request_raw:
                return None
            names = set(Paper.__dataclass_fields__)
            candidates = []
            for row in raw_candidates:
                if (not isinstance(row, dict) or set(row) != names
                        or any(not isinstance(row.get(k), str) for k in ('id', 'title', 'abstract', 'url'))
                        or not row['id'] or not row['title']
                        or not isinstance(row.get('publication'), dict)
                        or not isinstance(row.get('sources'), list)
                        or any(not isinstance(s, str) for s in row['sources'])):
                    return None
                candidates.append(Paper(**row))
            by_id = {p.id: p for p in candidates}
            if len(by_id) != len(candidates):
                return None
            if any(not isinstance(k, str) or k not in by_id or not isinstance(v, dict)
                   or v.get('paper_id') != k for k, v in raw_assessments.items()):
                return None
            assessed = [p for p in candidates if p.id in raw_assessments]
            rows = [raw_assessments[p.id] for p in assessed]
            validate_assessments({'assessments': rows}, assessed)
            checked = grounded_assessments(rows, assessed, request.category)
            selected_ids = []
            for row in raw_papers:
                if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                    return None
                identity = row['id']
                if identity in selected_ids or identity not in checked or not checked[identity]['evidence_verified']:
                    return None
                if row.get('abstract') != by_id[identity].abstract:
                    return None
                selected_ids.append(identity)
            if len(selected_ids) > request.target_count:
                return None
            config = cp.get('config')
            if not isinstance(config, dict) or type(config.get('research_mode')) is not bool:
                return None
            research = config['research_mode']
            if data.get('mode') == 'live':
                if (config.get('source') != data.get('source', config.get('source')) or
                        research != data.get('research_mode', research)):
                    return None
                allowed_sources = set(source_names(config.get('source')))
                if any(set(p.sources) - allowed_sources or
                       (p.source and p.source not in allowed_sources) or
                       (p.abstract_source and p.abstract_source not in allowed_sources)
                       for p in candidates):
                    return None
            selected = select_papers([by_id[i] for i in selected_ids], checked, request, research=research)
            if {p['id'] for p in selected} != set(selected_ids):
                return None
            rebuilt = {p['id']: p for p in selected}
            if any(any(row.get(key) != value for key, value in rebuilt[row['id']].items())
                   for row in raw_papers):
                return None
            for row in selected:
                projected = public_candidates([row])
                if not projected:
                    return None
                # Keep the verified assessment, but publish only the bounded
                # structured publication metadata from the candidate projection.
                row['publication'] = projected[0].get('publication', {})
            return {'mode': cp.get('mode', 'live'), 'method': cp.get('method', 'A'),
                    'status': 'partial', 'stop_reason': 'process_interrupted',
                    'request': asdict(request), 'papers': selected,
                    'candidates': public_candidates([p.to_dict() for p in candidates]),
                    'assessments': checked, 'gaps': coverage(selected, request, research=research),
                    'trace': deepcopy(item.get('trace') or []), 'warnings': []}
        except (GuideError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            return None

    def _interrupted(self, job_id):
        path = self.output / job_id / 'job.json'
        document = SearchJournal.read(path)
        item = document.get('job')
        if not isinstance(item, dict):
            return None
        item = deepcopy(item)
        if item.get('state') in ('done', 'error'):
            return item
        journal_doc = SearchJournal.read(path.with_name('calls.json'))
        rows = journal_doc.get('calls')
        corrupt_journal = (journal_doc.get('version') != 1 or not isinstance(rows, list)
                           or any(not isinstance(row, dict) or row.get('state') not in ('done', 'error', 'pending')
                                  for row in rows))
        if corrupt_journal:
            rows = []
        uncertain = sum(row.get('state') == 'pending' for row in rows)
        recoverable = document.get('recoverable_runner') is True and not item.get('session_id')
        recovery = {'can_resume': recoverable and not uncertain,
                    'completed_calls': len(rows) - uncertain, 'uncertain_calls': uncertain,
                    'reason': 'unknown_external_outcome' if uncertain else 'process_interrupted',
                    'message': ('中断时有外部请求尚未确认完成，可能已产生费用；已保存结果可用，不能安全自动重放。'
                                if uncertain else '已保存完成的步骤，继续后将复用这些结果，只执行尚未开始的步骤。')}
        if not recoverable:
            recovery.update(can_resume=False, reason='custom_runner',
                            message='自定义执行器没有恢复协议。已保存执行记录，请检查后新建任务。')
        elif corrupt_journal:
            recovery.update(can_resume=False, reason='journal_corrupt',
                            message='恢复记录损坏；已保存结果可用，不能安全自动重放。')
        elif not uncertain and (document.get('input') or {}).get('mode') != 'demo':
            try:
                current_identity = execution_identity(self._config(document['input']))
            except (GuideError, ValueError, OSError):
                current_identity = None
            if journal_doc.get('identity') != current_identity:
                recovery.update(can_resume=False, reason='runtime_changed',
                                message='模型、后端、配置或程序版本已改变；保留现有结果，请重新创建任务。')
        # A lease proves another process still owns the task, without trusting
        # stale state/PIDs or creating a second worker after an observation gap.
        try:
            lease = JobLease(path.parent)
        except RecoveryUnsafe:
            item.update(state='running', recovery={'can_resume': False, 'reason': 'owned_by_process',
                                                  'message': '任务仍在另一个服务进程运行。'})
            return item
        else:
            lease.close()
        if 'checkpoint' in item:
            recovery['checkpoint_available'] = True
        item['result'] = self._checkpoint_preview(document, item) if recoverable else None
        # Include candidates even if the process died just before the next
        # workflow event. They are discovery data, not verified recommendations.
        candidates = []
        data = document.get('input') if isinstance(document.get('input'), dict) else {}
        selected_sources = data.get('source')
        allowed_sources = set(source_names(selected_sources)) if isinstance(selected_sources, str) else set()
        for row in rows if recoverable else []:
            if len(candidates) >= 20:
                break
            if row.get('state') != 'done' or not isinstance(row.get('kind'), str) or not row['kind'].startswith('source.'):
                continue
            value = row.get('output')
            if not isinstance(value, dict) or not isinstance(value.get('papers'), list):
                continue
            for paper in value['papers'][:20]:
                if len(candidates) >= 20:
                    break
                if not isinstance(paper, dict):
                    continue
                origins = paper.get('sources') or []
                if not isinstance(origins, list) or any(not isinstance(origin, str) for origin in origins):
                    continue
                origins = set(origins)
                origins.update(origin for origin in (paper.get('source'), paper.get('abstract_source'))
                               if isinstance(origin, str) and origin)
                if data.get('mode') == 'live' and (not origins or not origins <= allowed_sources):
                    continue
                candidates.extend(public_candidates([paper]))
        if candidates:
            preview = item.get('result')
            if not isinstance(preview, dict):
                preview = {'mode': (document.get('input') or {}).get('mode', 'live'),
                           'method': (document.get('input') or {}).get('method', 'A'),
                           'status': 'partial', 'stop_reason': 'process_interrupted',
                           'request': {}, 'papers': [], 'candidates': [],
                           'trace': item.get('trace') or [], 'warnings': [recovery['message']]}
                item['result'] = preview
            seen = {paper.get('id') or paper.get('paper_id') for paper in preview.get('candidates') or []}
            for paper in candidates:
                if len(preview.get('candidates') or []) >= 20:
                    break
                identity = paper.get('id') or paper.get('paper_id')
                if identity not in seen:
                    preview.setdefault('candidates', []).append(paper)
                    seen.add(identity)
        item.update(state='needs_retry', error='', recovery=recovery,
                    cancel_requested=False, cancellation_state='')
        return item

    def resume(self, job_id: str) -> str:
        if not re.fullmatch(r'[a-f0-9]{32}', job_id):
            raise GuideError('任务不存在。')
        with self.lock:
            if self.active is not None:
                raise GuideError('已有任务正在运行，请等待完成后再继续。')
            snapshot = self._interrupted(job_id)
            if snapshot is None or snapshot.get('state') != 'needs_retry':
                raise GuideError('任务已结束或仍在运行，不能重复继续。')
            if not (snapshot.get('recovery') or {}).get('can_resume'):
                raise RecoveryUnsafe((snapshot.get('recovery') or {}).get('message') or '不能安全恢复此任务。')
            document = SearchJournal.read(self.output / job_id / 'job.json')
            data = validate_input(document.get('input') or {})
            if data['mode'] != 'demo':
                # Validate before taking ownership or changing the saved preview.
                SearchJournal(self.output / job_id / 'calls.json', self._config(data), resume=True)
            lease = JobLease(self.output / job_id)
            token = CancellationToken()
            self.leases[job_id] = lease
            self.requests[job_id] = deepcopy(data)
            self.cancel_tokens[job_id] = token
            self.items[job_id] = {**snapshot, 'state': 'running', 'trace': deepcopy(snapshot.get('trace') or []),
                                  'result': deepcopy(snapshot.get('result')), 'error': None, 'stage': 'resuming',
                                  'updated_at': utc_now(), 'cancel_requested': False,
                                  'cancellation_state': ''}
            self.active = job_id
            try:
                self._persist(job_id)
            except Exception:
                self.active = None
                self.leases.pop(job_id).close()
                self.items.pop(job_id, None)
                raise
            data.update(_job_id=job_id, _cancel_token=token, _resume=True)
        threading.Thread(target=self._worker, args=(job_id, data), daemon=True).start()
        return job_id




class ChatService:
    """Minimal stateful multi-turn service for the local web UI.

    Demo mode is fully offline and deterministic; live mode uses the same
    ChatSession as the CLI. Every turn and a meta.json are written under
    <output>/<session>/; list_sessions/snapshot can rehydrate a saved session
    after a browser or server restart, and the Chat UI exposes saved sessions.
    """

    def __init__(self, output: Path = RUNS_DIR / 'web',
                 providers: ProviderStore | None = None):
        self.output = output
        self.providers = providers
        self.sources = None
        self.lock = threading.Lock()
        self.sessions: dict[str, dict] = {}

    @staticmethod
    def _read_meta(directory: Path) -> dict:
        try:
            value = json.loads((directory / 'meta.json').read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _read_runtime(directory: Path) -> dict:
        try:
            value = json.loads((directory / 'runtime.json').read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _title(meta: dict, first: dict | None = None) -> str:
        """A follow-up must never rename the research workspace implicitly."""
        first = first or {}
        value = (meta.get('display_title') or meta.get('default_title') or
                 (first.get('request') or {}).get('topic') or first.get('user_input') or '未命名研究')
        return ' '.join(str(value).split())[:80]

    @staticmethod
    def _result_summary(result: dict, task: dict | None = None) -> dict:
        fields = ('topic', 'purpose', 'reading_goal', 'target_count', 'category',
                  'minimum_category_count', 'venue', 'year_from', 'year_to',
                  'venue_scope', 'publication_type', 'accepted_only', 'presentation_types')
        safe_task = {key: deepcopy(value) for key, value in (task or result.get('request') or {}).items()
                     if key in fields}
        coverage = result.get('research_coverage') or {}
        facets = {str(row.get('id')): str(row.get('label') or row.get('id'))
                  for row in coverage.get('facets') or [] if isinstance(row, dict)}
        missing = []
        for row in coverage.get('missing') or []:
            facet_id = str(row.get('id') or '') if isinstance(row, dict) else str(row)
            if facet_id:
                missing.append({'id': facet_id, 'label': facets.get(facet_id, facet_id)})
        return {'current_task': safe_task, 'paper_count': len(result.get('papers') or []),
                'coverage_status': str(coverage.get('status') or ''), 'missing_facets': missing}

    def _runtime_view(self, directory: Path, results: list[dict], *, running: bool = False) -> dict:
        """Expose interruption honestly; this is not a Chat call replay protocol."""
        value = self._read_runtime(directory)
        if value.get('state') != 'running':
            return value
        confirmed = max((int(row.get('turn') or 0) for row in results), default=0)
        try:
            expected = int(value.get('expected_turn') or (confirmed + 1))
        except (ValueError, TypeError):
            expected = confirmed + 1
        if expected > 0 and confirmed >= expected:
            return {}  # result.json is the durable commit marker, even after a late crash.
        if running:
            return value
        try:
            lease = JobLease(directory)
        except RecoveryUnsafe:
            return {**value, 'state': 'running', 'external_owner': True}
        else:
            lease.close()
        return {**value, 'state': 'interrupted', 'error_code': 'turn_interrupted',
                'error': '上一轮在服务中断前未确认完成，外部请求可能已产生费用。已保留此前结果；不会自动重发，请明确提交下一条消息。'}

    @staticmethod
    def _current_task(reader: sqlite3.Connection, session_id: str) -> dict:
        row = reader.execute(
            'SELECT t.task_json FROM session_task st JOIN task_versions t '
            'ON t.session_id=st.session_id AND t.revision=st.revision WHERE st.session_id=?',
            (session_id,)).fetchone()
        try:
            value = json.loads(row[0]) if row else {}
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            return {}

    def _refresh_saved_results(self, record: dict) -> None:
        """A second observer can discover turns committed by the executing process."""
        known = {int(row.get('turn') or 0) for row in record['results']}
        for path in self._result_files(record['directory']):
            number = int(path.parent.name.split('-', 1)[1])
            if number in known:
                continue
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if isinstance(value, dict):
                value['turn'] = number
                record['results'].append(value)
        record['results'].sort(key=lambda row: int(row.get('turn') or 0))

    @staticmethod
    def _last_event_seq(record):
        reader = getattr(record.get('store'), 'events', None)
        if not callable(reader):
            return 0  # Embedders may provide a non-persistent adapter.
        session_id = getattr(record.get('chat'), 'session_id', record['directory'].name)
        return max((int(event.get('seq') or 0) for event in reader(session_id)), default=0)

    def _claim_turn(self, record: dict, operation_id=None) -> dict:
        if record.get('running'):
            raise TurnRunning('这一轮还在运行，请等待完成。')
        try:
            lease = JobLease(record['directory'])
        except RecoveryUnsafe:
            raise TurnRunning('这一轮仍在另一个服务进程运行，请等待完成。') from None
        try:
            previous = self._read_runtime(record['directory'])
            previous_id = previous.get('operation_id')
            cached = record.get('operation_terminals', {}).get(previous_id)
            terminal = cached or (previous if previous.get('state') in ('failed', 'completed', 'interrupted') else None)
            if terminal and previous_id and re.fullmatch(r'[a-f0-9]{32}', previous_id):
                path = record['directory'] / 'operations' / (previous_id + '.json')
                try:
                    saved = json.loads(path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    saved = {}
                if not isinstance(saved, dict) or saved.get('state') == 'running':
                    atomic_json(path, terminal)
            self._refresh_saved_results(record)
            prepare = getattr(record['chat'], 'prepare_execution', None)
            if prepare is not None:
                prepare()
            numbers = [int(row.get('turn') or 0) for row in record['results']]
            for directory in record['directory'].glob('turn-*'):
                match = re.fullmatch(r'turn-([1-9][0-9]*)', directory.name)
                if match and directory.is_dir():
                    numbers.append(int(match.group(1)))
            expected = max(numbers, default=0) + 1
            start_seq = self._last_event_seq(record)
            operation_id = operation_id or uuid.uuid4().hex
            admission = {'operation_id': operation_id, 'turn': expected, 'start_seq': start_seq}
            atomic_json(record['directory'] / 'runtime.json', {
                'version': 2, 'state': 'running', 'expected_turn': expected,
                **admission, 'started_at': utc_now(), 'updated_at': utc_now(), 'error': '', 'error_code': ''})
            atomic_json(record['directory'] / 'operations' / (operation_id + '.json'),
                        {**admission, 'state': 'running'})
            record.update(running=True, error='', error_code='', lease=lease,
                          result_persistence_failed=False, **admission)
            return admission
        except Exception:
            lease.close()
            raise

    def _finish_turn(self, record: dict, error: Exception | None = None) -> None:
        persistence_failed = bool(record.get('result_persistence_failed'))
        message = ('本轮结果未能确认保存，已保留此前结果；不会自动重发。'
                   if persistence_failed and error is None else
                   ('本轮执行未完成，已保留此前结果。请检查模型配置，再明确提交下一条消息。'
                    if error is not None else ''))
        details = execution_error(error, default_code='turn_failed') if error is not None else None
        if error is not None:
            # Synchronous callers also cross the HTTP/admission boundary.
            try:
                error._execution_failure = True
            except (AttributeError, TypeError):
                pass
        with record['lock']:
            try:
                terminal = {'version': 2, 'state': ('failed' if error is not None else
                                                    'interrupted' if persistence_failed else 'completed'),
                            'updated_at': utc_now(), 'error': message,
                            'operation_id': record.get('operation_id'), 'turn': record.get('turn'),
                            'start_seq': record.get('start_seq', 0),
                            'end_seq': self._last_event_seq(record),
                            'error_code': details['code'] if details else ''}
                if details:
                    terminal['error_details'] = details
                if persistence_failed:
                    terminal['persistence_confirmed'] = False
                    if not details:
                        terminal['error_code'] = 'turn_interrupted'
                paths = [record['directory'] / 'runtime.json']
                if record.get('operation_id'):
                    paths.append(record['directory'] / 'operations' / (record['operation_id'] + '.json'))
                persisted = []
                for path in paths:
                    try:
                        atomic_json(path, terminal)
                        persisted.append(path)
                    except OSError:
                        pass
                if len(persisted) != len(paths):
                    terminal['persistence_confirmed'] = False
                record.setdefault('operation_terminals', {})[record.get('operation_id')] = terminal
            finally:
                record.update(running=False, error=message,
                              error_code=details['code'] if details else ('turn_interrupted' if persistence_failed else ''))
                lease = record.pop('lease', None)
                if lease is not None:
                    lease.close()

    @staticmethod
    def _result_files(directory: Path) -> list[Path]:
        def number(path: Path) -> int:
            try:
                return int(path.parent.name.split('-', 1)[1])
            except (IndexError, ValueError):
                return 0
        return sorted(directory.glob('turn-*/result.json'), key=number)

    def _evict_idle(self) -> None:
        """Evict only idle records; a running background turn owns its store."""
        while len(self.sessions) > 20:
            idle = [key for key, item in self.sessions.items() if not item.get('running')]
            if not idle:
                break
            old_id = idle[0]
            old = self.sessions.pop(old_id)
            try:
                old['store'].close()
            except Exception:
                pass

    def _load_record(self, session_id: str) -> dict | None:
        """Rehydrate a session saved before a browser/server restart."""
        if not re.fullmatch(r'[a-f0-9]{32}', session_id):
            return None
        directory = self.output / session_id
        state_path = directory / 'state.sqlite3'
        if not state_path.exists():
            return None
        meta = self._read_meta(directory)
        mode = meta.get('mode', 'demo')
        try:
            store = StateStore(state_path)
        except (OSError, sqlite3.Error):
            return None
        config = (self.providers.config(Config()) if self.providers else Config()) if mode == 'demo' else (
            self.providers.config(Config.from_env()) if self.providers else Config.from_env())
        if meta.get('source'):
            config = replace(config, source=meta['source'])
        config = replace(config, research_mode=bool(meta.get('research_mode', mode == 'live')) if mode == 'live' else False)
        if self.sources is not None and mode == 'live':
            config = self.sources.configure(config)
        source = llm = None
        if mode == 'demo':
            source = DemoSource(meta.get('scenario', 'followup'))
            llm = ChatDemoLLM(ask_first=False)
        try:
            chat = ChatSession(config, store, session_id=session_id, source=source,
                               llm=llm, reuse=bool(meta.get('reuse', True)))
        except (GuideError, OSError):
            store.close()
            return None
        results = []
        for path in self._result_files(directory):
            try:
                item = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if not isinstance(item, dict):
                continue
            # The artifact path is the stable turn identity even if earlier
            # files were removed or an old payload omitted its turn field.
            item['turn'] = int(path.parent.name.split('-', 1)[1])
            if meta.get('origin') == 'search':
                item.setdefault('imported_from', 'search')
            results.append(item)
        record = {'chat': chat, 'store': store, 'directory': directory,
                  'created_at': meta.get('created_at') or utc_now(),
                  'results': results, 'lock': threading.Lock(),
                  'mode': mode, 'source': config.source, 'research_mode': config.research_mode,
                  'scenario': meta.get('scenario', 'followup'),
                  'origin': meta.get('origin', 'chat'),
                  'display_title': self._title(meta, results[0] if results else {}),
                  'title_is_custom': bool(meta.get('title_is_custom')),
                  'provider': meta.get('provider') or config.provider,
                  'model': meta.get('model') or config.model,
                  'profile_name': meta.get('profile_name') or config.profile_name,
                  'runtime_provider': meta.get('runtime_provider') or meta.get('provider') or config.provider,
                  'protocol': meta.get('protocol') or config.protocol,
                  'profile_id': meta.get('profile_id') or config.profile_id,
                  'archived_at': meta.get('archived_at') or '',
                  'input_price': config.input_price, 'output_price': config.output_price,
                  'currency': config.currency,
                  'reuse': bool(meta.get('reuse', True))}
        with self.lock:
            current = self.sessions.get(session_id)
            if current is not None:
                store.close()
                return current
            self.sessions[session_id] = record
            self._evict_idle()
        return record

    def list_sessions(self) -> list[dict]:
        """Return saved sessions on disk, including prior processes."""
        if not self.output.exists():
            return []
        items = []
        for state_path in self.output.glob('*/state.sqlite3'):
            session_id = state_path.parent.name
            if not re.fullmatch(r'[a-f0-9]{32}', session_id):
                continue
            meta = self._read_meta(state_path.parent)
            try:
                con = sqlite3.connect(f'file:{state_path.as_posix()}?mode=ro', uri=True)
                con.row_factory = sqlite3.Row
                row = con.execute('SELECT status, created_at, updated_at, budget_json FROM sessions').fetchone()
                task = self._current_task(con, session_id)
                con.close()
            except sqlite3.Error:
                continue
            if row is None:
                continue
            results = self._result_files(state_path.parent)
            last = {}
            if results:
                try:
                    value = json.loads(results[-1].read_text(encoding='utf-8'))
                    last = value if isinstance(value, dict) else {}
                except (OSError, ValueError):
                    last = {}
            first = last
            if len(results) > 1:
                try:
                    first = json.loads(results[0].read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    first = {}
            if not isinstance(first, dict):
                first = {}
            try:
                budget = json.loads(row['budget_json'] or '{}')
            except (TypeError, ValueError):
                budget = {}
            feedback = None
            archived_at = meta.get('archived_at') or ''
            status = last.get('status') or row['status']
            live_record = self.sessions.get(session_id)
            running = bool(live_record and live_record.get('running'))
            runtime = self._runtime_view(state_path.parent, [{'turn': int(path.parent.name.split('-', 1)[1])}
                                                           for path in results], running=running)
            running = running or runtime.get('state') == 'running'
            if running:
                status = 'running'
            elif runtime.get('state') in ('failed', 'interrupted'):
                status = 'failed'
            awaiting = status == 'awaiting_user'
            can_send = not archived_at and not running
            caps = {'can_send': can_send, 'can_retry': not archived_at and not running,
                    'feedback_phases_left': None, 'feedback_phases_cap': None,
                    'awaiting_user': awaiting, 'archived': bool(archived_at),
                    'running': running}
            if runtime.get('state') in ('interrupted', 'failed'):
                caps['can_retry'] = False
            title = self._title(meta, first)
            items.append({'session_id': session_id,
                          'status': status,
                          'mode': meta.get('mode', 'demo'),
                          'source': meta.get('source', 'arxiv'),
                          'origin': meta.get('origin', 'chat'),
                          'display_title': title, 'title': title,
                          'title_is_custom': bool(meta.get('title_is_custom')),
                          **self._result_summary(last, task),
                          'error': runtime.get('error') or '',
                          'error_code': runtime.get('error_code') or '',
                          'interrupted': runtime.get('state') == 'interrupted',
                          'provider': meta.get('provider') or 'openai',
                          'runtime_provider': meta.get('runtime_provider') or meta.get('provider') or 'openai',
                          'profile_name': meta.get('profile_name') or '',
                          'model': meta.get('model') or '',
                          'protocol': meta.get('protocol') or 'chat_completions',
                          'profile_id': meta.get('profile_id') or 'env',
                          'archived_at': archived_at,
                          'archived': bool(archived_at),
                          'feedback_phases_left': None,
                          'feedback_phases_cap': None,
                          'capabilities': caps,
                          'turns': len(results),
                          'message': last.get('user_input') or '',
                          'created_at': meta.get('created_at') or row['created_at'],
                          'updated_at': row['updated_at']})
        items.sort(key=lambda item: item.get('updated_at') or '', reverse=True)
        return items

    @staticmethod
    def validate(data: dict) -> dict:
        if not isinstance(data, dict):
            raise GuideError('请求必须是 JSON 对象。')
        structured = data.get('request_data')
        if structured is not None:
            if not isinstance(structured, dict):
                raise GuideError('request_data must be an object.')
            Request.from_dict(structured)
        message = data.get('message') or (structured or {}).get('topic') or ''
        if not isinstance(message, str) or not message.strip() or len(message) > 2000:
            raise GuideError('请填写 1–2000 字的消息。')
        from quantities import quantity_constraints
        quantity_constraints(message)
        mode, scenario = data.get('mode', 'demo'), data.get('scenario', 'followup')
        if not isinstance(mode, str) or mode not in ('demo', 'live'):
            raise GuideError('mode 必须是 demo 或 live。')
        if not isinstance(scenario, str) or scenario not in ('followup', 'ask'):
            raise GuideError('演示场景无效。')
        if 'reuse' in data and type(data['reuse']) is not bool:
            raise GuideError('reuse 必须是布尔值。')
        if data.get('reuse') is False:
            raise GuideError('证据复用由有效性检查自动管理；产品接口不提供消融开关。')
        stream = data.get('stream', False)
        research_mode = data.get('research_mode', True)
        if type(stream) is not bool or type(research_mode) is not bool:
            raise GuideError('stream 和 research_mode 必须是布尔值。')
        source = _source_value(data.get('source'), default='')
        constraints = _constraints(data.get('constraints', {}))
        goal_hint = data.get('goal_hint')
        if goal_hint is not None and goal_hint not in ('', 'auto', 'orientation', 'comparison', 'frontier', 'general'):
            raise GuideError('阅读目标无效。')
        return {'message': message.strip(), 'mode': mode, 'scenario': scenario,
                'reuse': True, 'stream': stream, 'source': source,
                'request_data': structured, 'research_mode': research_mode,
                'constraints': constraints, 'goal_hint': goal_hint}

    def _new_chat(self, data: dict):
        if data['mode'] == 'demo':
            config = self.providers.config(Config()) if self.providers else Config()
            if data.get('source'):
                config = replace(config, source=data['source'])
            source = DemoSource('followup')
            llm = ChatDemoLLM(ask_first=data['scenario'] == 'ask')
        else:
            config = self.providers.config(Config.from_env()) if self.providers else Config.from_env()
            if data.get('source'):
                config = replace(config, source=data['source'])
            config = replace(config, research_mode=data.get('research_mode', True))
            config.validate()
            source = llm = None
        if self.sources is not None and data['mode'] == 'live':
            config = self.sources.configure(config)
        return config, source, llm

    def _start_background(self, record: dict, fn, claimed: bool = False) -> None:
        def run():
            failure = None
            try:
                with execution_context(cancelled=lambda: bool(
                        getattr(record['chat'], 'cancel_requested', False))):
                    result = fn()
                with record['lock']:
                    self._append(record, result)
            except Exception as exc:  # noqa: BLE001 - surface to the browser
                failure = exc
            finally:
                self._finish_turn(record, failure)
        if not claimed:
            with record['lock']:
                self._claim_turn(record)
        threading.Thread(target=run, daemon=True).start()

    def create(self, data: dict, *, progress=None, cancel_token=None, on_created=None, operation_id=None) -> dict:
        data = self.validate(data)
        request_data = data.get('request_data')
        if request_data is not None and not isinstance(request_data, dict):
            raise GuideError('request_data must be an object.')
        config, source, llm = self._new_chat(data)
        session_id = uuid.uuid4().hex
        directory = self.output / session_id
        directory.mkdir(parents=True, exist_ok=True)
        try:
            store = StateStore(directory / 'state.sqlite3')
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        admitted = False
        try:
            created_at = utc_now()
            default_title = ' '.join(str((request_data or {}).get('topic') or data['message']).split())[:80]
            atomic_json(directory / 'meta.json', {
                'session_id': session_id, 'created_at': created_at,
                'default_title': default_title, 'display_title': default_title, 'title_is_custom': False,
                'mode': data['mode'], 'scenario': data['scenario'],
                'reuse': data['reuse'], 'source': config.source, 'research_mode': config.research_mode,
                'origin': 'chat', 'provider': config.provider, 'model': config.model,
                'profile_name': config.profile_name, 'runtime_provider': config.provider,
                'protocol': config.protocol, 'profile_id': config.profile_id,
                'input_price': config.input_price, 'output_price': config.output_price,
                'currency': config.currency,
            })
            chat = ChatSession(config, store, session_id=session_id, source=source,
                               llm=llm, reuse=True, progress=progress)
            record = {'chat': chat, 'store': store, 'directory': directory,
                      'created_at': created_at, 'results': [], 'lock': threading.Lock(),
                      'mode': data['mode'], 'source': config.source, 'research_mode': config.research_mode,
                      'scenario': data['scenario'], 'reuse': data['reuse'], 'origin': 'chat',
                      'display_title': default_title, 'title_is_custom': False,
                      'provider': config.provider, 'model': config.model,
                      'profile_name': config.profile_name, 'runtime_provider': config.provider,
                      'protocol': config.protocol, 'profile_id': config.profile_id,
                      'input_price': config.input_price, 'output_price': config.output_price,
                      'currency': config.currency, 'archived_at': '',
                      'running': False, 'error': ''}
            with self.lock:
                self.sessions[session_id] = record
            start = (lambda: chat.start(request_data=request_data, message=data['message'],
                                         constraints=data.get('constraints'), goal_hint=data.get('goal_hint')))
            with record['lock']:
                admission = self._claim_turn(record, operation_id=operation_id)
                admitted = True
                if cancel_token is not None:
                    chat.budget.cancel_token = cancel_token
            if on_created is not None:
                on_created(session_id)
            if data.get('stream'):
                self._start_background(record, start, claimed=True)
                return {'session_id': session_id, 'running': True, **admission}
            try:
                result = start()
                with record['lock']:
                    self._append(record, result)
            except Exception as exc:
                self._finish_turn(record, exc)
                raise
            else:
                self._finish_turn(record)
            return {'session_id': session_id, 'result': self._public(result), **admission}
        except Exception as exc:
            if admitted:
                if record.get('running'):
                    self._finish_turn(record, exc)
                raise
            # Before admission no external work or published session exists.
            with self.lock:
                self.sessions.pop(session_id, None)
            try:
                store.close()
            except Exception:
                pass
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def _capabilities(self, record: dict) -> dict:
        chat = record['chat']
        # The background worker owns the StateStore connection. Polling must
        # read committed state through its own connection, rather than execute
        # concurrent statements on that same Python sqlite3 connection.
        if isinstance(record.get('store'), StateStore):
            path = Path(record['store'].path).resolve().as_uri() + '?mode=ro'
            with closing(sqlite3.connect(path, uri=True)) as reader:
                row = reader.execute(
                    'SELECT status, budget_json FROM sessions WHERE session_id = ?',
                    (chat.session_id,)).fetchone()
            budget = json.loads(row[1] or '{}') if row else {}
            awaiting = bool(row and row[0] == 'awaiting_user')
            caps = {'can_send': True, 'can_retry': True,
                    'feedback_phases_left': None, 'feedback_phases_cap': None,
                    'awaiting_user': awaiting, 'session_status': row[0] if row else ''}
        else:
            caps = (chat.capabilities() if hasattr(chat, 'capabilities') else
                    {'can_send': True, 'can_retry': True, 'feedback_phases_left': 2,
                     'feedback_phases_cap': 2, 'awaiting_user': False})
        archived = bool(record.get('archived_at'))
        runtime = self._runtime_view(record['directory'], record['results'], running=bool(record.get('running')))
        running = bool(record.get('running')) or runtime.get('state') == 'running'
        caps.update({'archived': archived, 'running': running,
                     'can_stop': running and callable(getattr(chat, 'request_stop', None)) and not getattr(chat, 'cancel_requested', False),
                     'can_send': bool(caps.get('can_send')) and not archived and not running,
                     'can_retry': bool(caps.get('can_retry')) and not archived and not running})
        if runtime.get('state') in ('interrupted', 'failed'):
            caps['can_retry'] = False
        return caps

    def _refresh_live_provider(self, record: dict) -> None:
        if record.get('mode') != 'live' or self.providers is None:
            return
        config = self.providers.config(Config.from_env())
        if record.get('source'):
            config = replace(config, source=record['source'])
        config = replace(config, research_mode=bool(record.get('research_mode',
            getattr(getattr(record['chat'], 'config', None), 'research_mode', True))))
        if self.sources is not None:
            config = self.sources.configure(config)
        record['chat'].rebind_provider(config)
        record['provider'] = config.provider
        record['model'] = config.model
        record['profile_name'] = config.profile_name
        record['runtime_provider'] = config.provider
        record['protocol'] = config.protocol
        record['profile_id'] = config.profile_id
        record['input_price'] = config.input_price
        record['output_price'] = config.output_price
        record['currency'] = config.currency
        try:
            directory = record.get('directory')
            if directory is not None:
                with record['lock']:
                    meta = self._read_meta(directory)
                    meta.update({'source': config.source, 'provider': config.provider, 'model': config.model,
                                 'profile_name': config.profile_name,
                                 'runtime_provider': config.provider,
                                 'protocol': config.protocol, 'profile_id': config.profile_id,
                                 'input_price': config.input_price,
                                 'output_price': config.output_price,
                                 'currency': config.currency})
                    atomic_json(Path(directory) / 'meta.json', meta)
        except OSError:
            pass

    def send(self, session_id: str, data: dict, *, operation_id=None) -> dict:
        data = self.validate(data)
        if data.get('request_data') is not None:
            raise GuideError('继续会话请使用 message 与 constraints；request_data 仅用于创建。')
        record = self.sessions.get(session_id) or self._load_record(session_id)
        if record is None:
            raise KeyError(session_id)
        with record['lock']:
            if self._capabilities(record).get('running'):
                raise TurnRunning('这一轮还在运行，请等待完成。')
            if record.get('archived_at'):
                raise SessionArchived('会话已归档；请先恢复后再继续。')
            can_send = getattr(record['chat'], 'can_send', None)
            if callable(can_send) and not can_send():
                raise FeedbackBudgetExhausted('本会话的反馈预算已用尽；请新建会话或归档当前会话。')
            if data.get('source'):
                record['source'] = data['source']
            admission = self._claim_turn(record, operation_id=operation_id)
            stream = bool(data.get('stream'))
        background_started = False
        failure = None
        try:
            self._refresh_live_provider(record)
            def send_turn():
                options = {}
                if data.get('constraints'):
                    options['constraints'] = data['constraints']
                if data.get('goal_hint'):
                    options['goal_hint'] = data['goal_hint']
                return record['chat'].send(data['message'], **options)
            if stream:
                self._start_background(record, send_turn, claimed=True)
                background_started = True
                return {'session_id': session_id, 'running': True, **admission}
            result = send_turn()
            with record['lock']:
                self._append(record, result)
            return {'session_id': session_id, 'result': self._public(result), **admission}
        except Exception as exc:
            failure = exc
            raise
        finally:
            if not background_started:
                self._finish_turn(record, failure)

    def recover(self, session_id: str, code: str, *, stream=False) -> dict:
        record = self.sessions.get(session_id) or self._load_record(session_id)
        if record is None:
            raise KeyError(session_id)
        with record['lock']:
            if self._capabilities(record).get('running'):
                raise TurnRunning('这一轮还在运行，请等待完成。')
            if record.get('archived_at'):
                raise SessionArchived('会话已归档；请先恢复后再继续。')
            if not record['chat'].can_send():
                raise FeedbackBudgetExhausted('本会话的反馈预算已用尽；请新建会话或归档当前会话。')
            self._claim_turn(record)
        failure = None
        try:
            self._refresh_live_provider(record)
            if stream:
                self._start_background(record, lambda: record['chat'].apply_recovery(code), claimed=True)
                return {'session_id': session_id, 'running': True}
            result = record['chat'].apply_recovery(code)
            with record['lock']:
                self._append(record, result)
            return {'session_id': session_id, 'result': self._public(result)}
        except Exception as exc:
            failure = exc
            raise
        finally:
            if not stream:
                self._finish_turn(record, failure)

    def fork_source(self, session_id: str, source: str) -> dict:
        record = self.sessions.get(session_id) or self._load_record(session_id)
        if record is None:
            raise KeyError(session_id)
        source = _source_value(source)
        last = record['results'][-1] if record['results'] else {}
        task = record['chat'].task() or last.get('request')
        message = last.get('user_input') or task.get('topic') or ''
        if not message:
            raise GuideError('当前会话没有可复用的请求。')
        return self.create({'mode': record.get('mode', 'live'),
                            'scenario': record.get('scenario', 'followup'),
                            'reuse': bool(record.get('reuse', True)),
                            'source': source, 'message': message,
                            'research_mode': record.get('research_mode', True),
                            'request_data': dict(task), 'stream': True})

    def operation_snapshot(self, session_id: str, operation_id: str):
        if not re.fullmatch(r'[a-f0-9]{32}', session_id) or not re.fullmatch(r'[a-f0-9]{32}', operation_id):
            return None
        record = self.sessions.get(session_id)
        if record:
            with record['lock']:
                cached = record.get('operation_terminals', {}).get(operation_id)
                if cached is not None:
                    return deepcopy(cached)
        try:
            value = json.loads((self.output / session_id / 'operations' / (operation_id + '.json')).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            value = None
        runtime = self._read_runtime(self.output / session_id)
        if (not isinstance(value, dict) or value.get('state') == 'running') and runtime.get('operation_id') == operation_id and runtime.get('state') in ('failed', 'completed', 'interrupted'):
            return runtime
        if (isinstance(value, dict) and value.get('state') == 'running'
                and runtime.get('operation_id') and runtime.get('operation_id') != operation_id):
            return {**value, 'state': 'interrupted', 'end_seq': runtime.get('start_seq', value.get('start_seq', 0))}
        return value if isinstance(value, dict) else None

    def cancel(self, session_id: str, *, operation_id=None) -> dict | None:
        record = self.sessions.get(session_id) or self._load_record(session_id)
        if record is None:
            return None
        with record['lock']:
            if operation_id is not None and record.get('operation_id') != operation_id:
                return {'session_id': session_id, 'accepted': False, 'cancel_requested': False,
                        'cancellation_state': '', 'reason': 'operation_not_current'}
            accepted = bool(record.get('running')) and callable(getattr(record['chat'], 'request_stop', None))
            if accepted:
                record['chat'].request_stop()
            requested = bool(getattr(record['chat'], 'cancel_requested', False))
            last = record['results'][-1] if record['results'] else {}
            return {'session_id': session_id, 'accepted': accepted,
                    'cancel_requested': requested,
                    'cancellation_state': ('stopping' if accepted and requested else
                        ('stopped' if last.get('stop_reason') == 'user_cancelled' else ''))}

    def presentation(self, session_id: str, number: int, store_path) -> dict:
        fallback = {'status': 'not_applied', 'paper_ids': []}
        if not re.fullmatch(r'[a-f0-9]{32}', session_id) or type(number) is not int or not 1 <= number <= 999999:
            return fallback
        record = self.sessions.get(session_id)
        if record is None:
            return fallback
        try:
            with record['lock']:
                self._refresh_saved_results(record)
                runtime = self._read_runtime(record['directory'])
                if record.get('running') or runtime.get('state') == 'running' or not record['results']:
                    return fallback
                result = record['results'][-1]
                if result.get('turn') != number or record.get('result_persistence_failed'):
                    return fallback
                path = record['directory'] / f'turn-{number}' / 'result.json'
                saved = json.loads(path.read_text(encoding='utf-8'))
                papers = self._public(result)['papers']
                saved_papers = self._public(saved)['papers']
                if papers != saved_papers or not 1 <= len(papers) <= 10:
                    return fallback
                ids = [p.get('id') for p in papers]
                if any(type(i) is not str or not i.strip() or len(i) > 512 for i in ids) or len(set(ids)) != len(ids):
                    return fallback
                task_snapshot = record['store'].current_task(session_id)
                if not isinstance(task_snapshot, dict) or task_snapshot.get('revision') != result.get('revision'):
                    return fallback
                task = task_snapshot.get('task')
                fields = task.get('_presentation_user_fields') if isinstance(task, dict) else None
                allowed_fields = {'prefer_recent', 'year_from', 'year_to'}
                if (not isinstance(fields, list) or any(type(field) is not str or field not in allowed_fields
                                                        for field in fields)):
                    return fallback
                fields = set(fields)
                request = result.get('request') or {}
                fields.update(key for key in allowed_fields if request.get(key) not in (None, False, ''))
                from memory_view import MemoryView
                view = MemoryView(store_path).present(papers, enabled=True, selected=True, explicit_fields=fields)
                self._refresh_saved_results(record)
                if (record.get('running') or self._read_runtime(record['directory']).get('state') == 'running'
                        or not record['results'] or record['results'][-1] is not result):
                    return fallback
                ordered = [paper.get('id') for paper in view['papers']]
                if view['status'] != 'applied' or len(ordered) != len(ids) or set(ordered) != set(ids):
                    return fallback
                return {'status': 'applied', 'paper_ids': ordered}
        except Exception:
            return fallback

    def turn_result(self, session_id: str, number: int) -> dict | None:
        record = self.sessions.get(session_id) or self._load_record(session_id)
        if record is None or number < 1:
            return None
        with record['lock']:
            result = next((item for item in record['results'] if item.get('turn') == number), None)
            return deepcopy(result) if result is not None else None

    def delete(self, session_id: str) -> dict | None:
        """Fail closed without loading a record or touching its storage."""
        _deletion_unavailable()

    def set_archived(self, session_id: str, archived: bool) -> dict | None:
        record = self.sessions.get(session_id) or self._load_record(session_id)
        if record is None:
            return None
        with record['lock']:
            if self._capabilities(record).get('running'):
                raise TurnRunning('运行中的会话不能归档。')
            meta = self._read_meta(record['directory'])
            meta['archived_at'] = utc_now() if archived else ''
            meta['archived'] = bool(archived)
            atomic_json(record['directory'] / 'meta.json', meta)
            record['archived_at'] = meta['archived_at']
        return self.snapshot(session_id)

    def rename(self, session_id: str, data: dict) -> dict | None:
        if not isinstance(data, dict) or set(data) != {'title'} or not isinstance(data['title'], str):
            raise GuideError('改名请求只接受 title 字符串。')
        title = ' '.join(data['title'].split())
        if len(title) > 80:
            raise GuideError('会话标题最多 80 字。')
        record = self.sessions.get(session_id) or self._load_record(session_id)
        if record is None:
            return None
        with record['lock']:
            if self._capabilities(record).get('running'):
                raise TurnRunning('请等待本轮完成后修改会话标题。')
            meta = self._read_meta(record['directory'])
            first = record['results'][0] if record['results'] else {}
            default = meta.get('default_title') or self._title({}, first)
            effective = title or default
            meta.update(default_title=default, display_title=effective, title_is_custom=bool(title))
            atomic_json(record['directory'] / 'meta.json', meta)
            record.update(display_title=effective, title_is_custom=bool(title))
        return self.snapshot(session_id)

    def events(self, session_id: str, after: int = 0) -> list[dict] | None:
        record = self.sessions.get(session_id) or self._load_record(session_id)
        if record is None:
            return None
        with record['lock']:
            path = Path(record['store'].path).resolve().as_uri() + '?mode=ro'
            with closing(sqlite3.connect(path, uri=True)) as reader:
                reader.row_factory = sqlite3.Row
                rows = reader.execute(
                    'SELECT * FROM events WHERE session_id = ? AND seq > ? ORDER BY seq',
                    (session_id, after)).fetchall()
            events = []
            for row in rows:
                event = dict(row)
                event['payload'] = json.loads(event.pop('payload_json') or '{}')
                events.append(event)
        out = []
        for event in events:
            if int(event.get('seq') or 0) > after:
                out.append({'seq': event['seq'], 'action': event['action'],
                            'revision': event.get('revision'),
                            'payload': event.get('payload') or {},
                            'created_at': event.get('created_at')})
        return out

    def close(self) -> None:
        with self.lock:
            for record in self.sessions.values():
                try:
                    record['store'].close()
                except Exception:
                    pass
            self.sessions.clear()

    def snapshot(self, session_id: str) -> dict | None:
        record = self.sessions.get(session_id) or self._load_record(session_id)
        if record is None:
            return None
        with record['lock']:
            self._refresh_saved_results(record)
            last = record['results'][-1] if record['results'] else {
                'status': 'active', 'revision': 0}
            runtime = self._runtime_view(record['directory'], record['results'], running=bool(record.get('running')))
            running = bool(record.get('running')) or runtime.get('state') == 'running'
            status = 'running' if running else last['status']
            error = record.get('error') or runtime.get('error') or ''
            if error and not running:
                status = 'failed'
            capabilities = self._capabilities(record)
            with closing(sqlite3.connect(Path(record['store'].path).resolve().as_uri() + '?mode=ro', uri=True)) as reader:
                task = self._current_task(reader, session_id)
            title = record.get('display_title') or self._title({}, record['results'][0] if record['results'] else {})
            return {'session_id': session_id, 'created_at': record['created_at'],
                    'running': running,
                    'status': status, 'question': last.get('question', ''),
                    'revision': last.get('revision', 0),
                    'error': error,
                    'error_code': record.get('error_code') or runtime.get('error_code') or '',
                    'interrupted': runtime.get('state') == 'interrupted',
                    'recovery': ({'can_resume': False, 'reason': 'unknown_external_outcome', 'message': error}
                                 if runtime.get('state') == 'interrupted' else None),
                    'cancel_requested': bool(getattr(record['chat'], 'cancel_requested', False)),
                    'cancellation_state': ('stopping' if record.get('running') and getattr(record['chat'], 'cancel_requested', False)
                        else ('stopped' if last.get('stop_reason') == 'user_cancelled' else '')),
                    'mode': record.get('mode', 'demo'),
                    'source': record.get('source', 'arxiv'),
                    'origin': record.get('origin', 'chat'),
                    'display_title': title, 'title': title,
                    'title_is_custom': bool(record.get('title_is_custom')),
                    **self._result_summary(last, task),
                    'provider': record.get('provider') or 'openai',
                    'runtime_provider': record.get('runtime_provider') or record.get('provider') or 'openai',
                    'profile_name': record.get('profile_name') or '',
                    'model': record.get('model') or '',
                    'protocol': record.get('protocol') or 'chat_completions',
                    'profile_id': record.get('profile_id') or 'env',
                    'archived_at': record.get('archived_at') or '',
                    'archived': bool(record.get('archived_at')),
                    'feedback_phases_left': capabilities['feedback_phases_left'],
                    'feedback_phases_cap': None,
                    'capabilities': capabilities,
                    'history': [self._public(r) for r in record['results']]}

    @staticmethod
    def _public(result: dict) -> dict:
        papers = [{'id': p.get('id'), 'title': p.get('title'), 'url': p.get('url', ''),
                   'authors': p.get('authors') or [], 'year': p.get('year'),
                   'venue': p.get('venue') or p.get('conference'),
                   'sources': p.get('sources') or ([p.get('source')] if p.get('source') else []),
                   'doi': p.get('doi') or '',
                   'open_access_url': p.get('open_access_url') or '',
                   'abstract_source': p.get('abstract_source') or '',
                   'abstract': p.get('abstract') or '',
                   'metadata_only': bool(p.get('metadata_only')),
                   'provenance_conflict': bool(p.get('provenance_conflict')),
                   'snippet': p.get('snippet') or '',
                   'category_status': p.get('category_status', 'unknown'),
                   'paper_type': p.get('paper_type', 'unknown'),
                   'publication': deepcopy(p.get('publication') or {}),
                   'fit_score': p.get('fit_score'),
                   'evidence_verified': bool(p.get('evidence_verified')),
                   'reason': p.get('reason', ''), 'evidence_quote': p.get('evidence_quote', '')}
                  for p in (result.get('papers') or [])]
        metrics = result.get('metrics') or {}
        reuse = metrics.get('reuse')
        if reuse is None:
            reuse = result.get('reuse')
        if isinstance(reuse, (int, float)):
            # Earlier web responses exposed only the rate. Keep those records
            # readable while giving the UI a stable object in every response.
            reuse = {'reuse_rate': reuse}
        if not isinstance(reuse, dict):
            reuse = {}
        diagnosis = result.get('diagnosis')
        if not diagnosis:
            # Old turn files predate diagnosis.  Derive an overlay at read time
            # instead of rewriting the evidence files in place.
            diagnosis = diagnose(
                request=result.get('request') or {}, gaps=result.get('gaps'),
                trace=result.get('trace'), candidates=result.get('candidates'),
                selected=result.get('papers'), stop_reason=result.get('stop_reason') or '')
        public = {'turn': result.get('turn') or 1,
                'request': {key: deepcopy(value) for key, value in (result.get('request') or {}).items()
                            if key in ('topic', 'purpose', 'reading_goal', 'target_count', 'minimum_count', 'maximum_count', 'category',
                                       'minimum_category_count', 'venue', 'year_from', 'year_to',
                                       'excluded_paper_ids', 'prefer_recent', 'venue_scope',
                                       'publication_type', 'accepted_only', 'presentation_types')},
                'research_brief': deepcopy(result.get('research_brief')),
                'research_coverage': deepcopy(result.get('research_coverage')),
                'unsupported': list(result.get('unsupported') or []),
                'status': result.get('status', 'unknown'),
                'stop_reason': result.get('stop_reason', ''),
                'message': result.get('user_input', ''),
                'imported_from': result.get('imported_from', ''),
                'question': result.get('question', ''), 'papers': papers,
                'gaps': result.get('gaps') or [],
                'cost_units': (metrics.get('cost_units') or {}).get('settled'),
                'cost': result.get('cost') or metrics.get('cost'),
                'reuse': {'reused': list(reuse.get('reused') or []),
                          'stale': list(reuse.get('stale') or []),
                          'reuse_rate': reuse.get('reuse_rate')},
                'diagnosis': diagnosis,
                'revision': result.get('revision', 0)}
        trace = result.get('trace')
        searches = [event for event in trace if isinstance(event, dict) and
                    event.get('action') in ('search', 'search_partial')] if isinstance(trace, list) else []
        last_search = searches[-1] if searches else {}
        payload = last_search.get('payload') if isinstance(last_search.get('payload'), dict) else {}
        partial_search = (last_search.get('action') == 'search_partial' or
                          last_search.get('status') == 'partial' or payload.get('status') == 'partial')
        if result.get('status') != 'complete' and partial_search:
            delivered = {p.get('id') or p.get('paper_id') for p in result.get('papers') or []
                         if isinstance(p, dict)}
            candidates = [p for p in public_candidates(result.get('candidates'))
                          if p['paper_id'] not in delivered]
            if candidates:
                public['candidates'] = candidates
        return public

    def _find_imported(self, run_id: str) -> str | None:
        if not self.output.exists():
            return None
        for path in self.output.glob('*/meta.json'):
            try:
                meta = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(meta, dict):
                continue
            if meta.get('origin') == 'search' and meta.get('run_id') == run_id:
                return str(meta.get('session_id') or path.parent.name)
        return None

    def import_run(self, result: dict, run_id: str) -> str | None:
        """Turn a completed Search run into a resumable Chat session.

        The search result is imported as turn 1; candidates, assessments,
        task state and the delivered papers are seeded so a follow-up reuses
        existing judgements instead of blindly re-searching.
        """
        existing = self._find_imported(run_id)
        if existing:
            return existing
        if not isinstance(result, dict) or not isinstance(result.get('request'), dict):
            raise GuideError('搜索尚未完成，不能创建会话。')
        mode = result.get('mode') if result.get('mode') in ('demo', 'live') else 'live'
        cfg = result.get('config') if isinstance(result.get('config'), dict) else {}
        if mode == 'demo':
            config = self.providers.config(Config()) if self.providers else Config()
            source = DemoSource('followup')
            llm = ChatDemoLLM(ask_first=False)
        else:
            config = self.providers.config(Config.from_env()) if self.providers else Config.from_env()
            source = llm = None
        if cfg.get('source'):
            config = replace(config, source=cfg['source'])
        config = replace(config, research_mode=bool(cfg.get('research_mode', False)) if mode == 'live' else False)
        if self.sources is not None and mode == 'live':
            config = self.sources.configure(config)
        session_id = uuid.uuid4().hex
        directory = self.output / session_id
        directory.mkdir(parents=True, exist_ok=True)
        try:
            store = StateStore(directory / 'state.sqlite3')
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        try:
            task = dict(result['request'])
            try:
                request = Request.from_dict(task)
            except GuideError:
                store.close()
                raise
            task.update(asdict(request))
            task.setdefault('unresolved', [])
            task.setdefault('requirement_origins', {})
            task.setdefault('assumptions', [])
            task.setdefault('unsupported', [])
            title = result.get('display_title') or task.get('topic') or ''
            if not result.get('display_title') and task.get('topic'):
                title = ('Live' if mode == 'live' else 'Demo') + ' · ' + str(task['topic'])
            title = ' '.join(str(title).split())[:80]
            message = title or str(result.get('user_input') or task.get('topic') or '')
            created_at = result.get('started_at') or utc_now()
            meta = {'session_id': session_id, 'created_at': created_at,
                    'mode': mode, 'scenario': 'followup', 'reuse': True,
                    'source': config.source, 'origin': 'search', 'research_mode': config.research_mode,
                    'provider': config.provider, 'model': config.model,
                    'profile_name': config.profile_name,
                    'runtime_provider': config.provider,
                    'protocol': config.protocol, 'profile_id': config.profile_id,
                    'input_price': config.input_price, 'output_price': config.output_price,
                    'currency': config.currency,
                    'run_id': run_id, 'display_title': title,
                    'default_title': title, 'title_is_custom': False}
            atomic_json(directory / 'meta.json', meta)
            chat = ChatSession(config, store, session_id=session_id, source=source,
                               llm=llm, reuse=True)
            store.save_task(session_id, 1, task, source_message=message,
                            change_summary='imported from Search')
            candidate_rows = result.get('candidates') or result.get('papers') or []
            by_id: dict[str, dict] = {}
            for paper in candidate_rows:
                if not isinstance(paper, dict):
                    continue
                paper_id = str(paper.get('paper_id') or paper.get('id') or '')
                if not paper_id:
                    continue
                ref = store.register_paper(paper)
                store.add_candidate(session_id, paper, str((result.get('queries') or [''])[0]))
                by_id[paper_id] = {'paper': paper, 'content': ref['content_hash']}
            provenance = result.get('assessment_provenance') or {}
            origin_model = str(provenance.get('model') or cfg.get('model') or 'unknown-import-model')
            origin_prompt = str(provenance.get('prompt_version') or (
                'assess-research-v2:unverified-import-origin' if config.research_mode else PROMPT_VERSION))
            from state import semantic_key
            semantic = semantic_key(request.topic, request.purpose, request.category,
                                    origin_model, origin_prompt, request.venue,
                                    request.year_from, request.year_to)
            assessments = result.get('assessments') or {}
            if isinstance(assessments, list):
                assessments = {str(item.get('paper_id') or ''): item for item in assessments
                               if isinstance(item, dict)}
            for paper_id, assessment in assessments.items():
                if not isinstance(assessment, dict):
                    continue
                paper_id = str(assessment.get('paper_id') or paper_id)
                ref = by_id.get(paper_id)
                if ref is None:
                    continue
                try:
                    store.put_assessment(
                        session_id, paper_id=paper_id, content=ref['content'],
                        semantic=semantic,
                        computed={'content_hash': ref['content'], 'semantic_key': semantic,
                                  'model': origin_model, 'prompt_version': origin_prompt},
                        relevance=int(assessment.get('relevance') or 0),
                        category_status=str(assessment.get('category_status') or 'unknown'),
                        paper_type=str(assessment.get('paper_type') or 'unknown'),
                        fit_score=assessment.get('fit_score', 50),
                        evidence_quote=str(assessment.get('evidence_quote') or ''),
                        evidence_verified=bool(assessment.get('evidence_verified')),
                        understanding_sentence=assessment.get('reason'),
                        unknown_reason=assessment.get('unknown_reason'))
                except Exception as exc:
                    result.setdefault('warnings', []).append(
                        f'导入会话时跳过一条无效判断：{type(exc).__name__}')
            for index, query in enumerate(result.get('queries') or [], start=1):
                if query:
                    store.record_query(session_id, 1, index, str(query), len(candidate_rows), len(candidate_rows))
            # Import the original Search cost ledger as audit evidence. Without this the
            # Chat session would look free even though the Search run settled real cost.
            costs = (result.get('metrics') or {}).get('cost_units') or {}
            by_action = costs.get('by_action') if isinstance(costs, dict) else None
            if isinstance(by_action, dict):
                for index, (action, values) in enumerate(by_action.items(), start=1):
                    if not isinstance(values, dict):
                        continue
                    reserved = float(values.get('reserved') or 0)
                    settled = float(values.get('settled') or 0)
                    if reserved < 0 or settled < 0 or settled > reserved:
                        continue
                    handle = store.reserve(session_id, index, 'search_import:' + str(action),
                                           reserved, {'imported_from_search': True})
                    store.settle(handle, settled)
            store.append_event(session_id, 'search_imported',
                               {'run_id': run_id, 'trace': result.get('trace', []),
                                'request': asdict(request)}, revision=1)
            store.append_event(session_id, 'task_initial',
                               {'request': asdict(request), 'message': message}, revision=1)
            papers = result.get('papers') or []
            if config.research_mode and isinstance(result.get('research_brief'), dict):
                from research_brief import validated_cached_brief
                cached = validated_cached_brief(result['research_brief'], result['request'],
                                                papers, research_model_identity(config))
                if cached is not None:
                    store.append_event(session_id, 'research_brief', {'brief': cached,
                        'imported_from_search': True}, revision=1)
            store.append_event(session_id, 'delivered', {'papers': [
                {'index': index, 'paper_id': paper.get('id') or paper.get('paper_id'), 'title': paper.get('title')}
                for index, paper in enumerate(papers, start=1)]}, revision=1)
            store.set_status(session_id, 'finished')
            store.con.execute(
                'UPDATE sessions SET created_at = ?, updated_at = ? WHERE session_id = ?',
                (created_at, created_at, session_id))
            store.commit()
            turn_result = deepcopy(result)
            turn_result['turn'] = 1
            turn_result['revision'] = 1
            turn_result['user_input'] = title or message
            turn_result['imported_from'] = 'search'
            record = {'chat': chat, 'store': store, 'directory': directory,
                      'created_at': created_at, 'results': [], 'lock': threading.Lock(),
                      'mode': mode, 'source': config.source, 'research_mode': config.research_mode,
                      'scenario': 'followup',
                      'reuse': True, 'origin': 'search', 'display_title': title,
                      'title_is_custom': False,
                      'provider': config.provider, 'model': config.model,
                      'profile_name': config.profile_name, 'runtime_provider': config.provider,
                      'protocol': config.protocol, 'profile_id': config.profile_id,
                      'input_price': config.input_price, 'output_price': config.output_price,
                      'currency': config.currency, 'archived_at': '',
                      'running': False, 'error': ''}
            with record['lock']:
                self._append(record, turn_result)
            with self.lock:
                self.sessions[session_id] = record
        except Exception:
            with self.lock:
                self.sessions.pop(session_id, None)
            try:
                store.close()
            except Exception:
                pass
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return session_id

    def _append(self, record: dict, result: dict) -> None:
        numbers = [int(item.get('turn') or 0) for item in record['results']]
        for directory in record['directory'].glob('turn-*'):
            match = re.fullmatch(r'turn-([1-9][0-9]*)', directory.name)
            if match and directory.is_dir():
                numbers.append(int(match.group(1)))
        turn = max(numbers, default=0) + 1
        result['turn'] = turn
        result['operation_id'] = record.get('operation_id')
        result['event_seq_start'] = record.get('start_seq', 0)
        result['event_seq_end'] = self._last_event_seq(record)
        # Session traces continue to grow across turns; a delivered turn is an
        # immutable snapshot, including exports served before a process restart.
        stored = deepcopy(result)
        record['results'].append(stored)
        try:
            save_run(stored, record['directory'] / f'turn-{turn}')
        except OSError:
            stored.setdefault('warnings', []).append('本次结果未能保存到磁盘。')
            record['result_persistence_failed'] = True


def _read_json(handler, limit: int = 16000, *, reject_duplicates=False) -> dict:
    content_type = handler.headers.get_content_type()
    length = int(handler.headers.get('Content-Length', '0'))
    if content_type != 'application/json':
        # Drain a small declared body before returning 415. On Windows the
        # client otherwise sees a reset when the server closes with unread bytes.
        if 0 < length <= limit:
            handler.rfile.read(length)
        raise GuideError('请求需要使用 application/json。')
    if not 0 < length <= limit:
        raise GuideError('请求过大或为空。')
    handler.connection.settimeout(10)
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON field.')
            result[key] = value
        return result
    return json.loads(handler.rfile.read(length), object_pairs_hook=unique_object if reject_duplicates else None)


def create_server(port: int = 8765, jobs: Jobs | None = None,
                  chat: ChatService | None = None,
                  activity: ActivityFeed | None = None,
                  providers: ProviderStore | None = None,
                  sources=None) -> ThreadingHTTPServer:
    from source_store import RetainedSourceResult, SourceStore
    providers = providers or ProviderStore()
    jobs = jobs or Jobs(providers=providers)
    runs_root = Path(jobs.output)
    chat = chat or ChatService(output=runs_root / 'web', providers=providers)
    activity = activity or ActivityFeed(runs=runs_root)
    if jobs.providers is None:
        jobs.providers = providers
    if chat.providers is None:
        chat.providers = providers
    if jobs.importer is None:
        jobs.importer = chat.import_run
    own_sources = sources is None
    product_runs = runs_root.resolve() == RUNS_DIR.resolve()
    sources = sources or SourceStore(None if product_runs else runs_root / '.sources.sqlite3')
    # The status/confirmation interface must address the same shared ledger as
    # every MCP transport, even when the run output directory is customized.
    from lune_quota import default_quota_path
    if sources.quota_path is not None and sources.quota_path.resolve() != default_quota_path().resolve():
        raise GuideError('Lune quota interface and transport must use the same ledger.')
    jobs.sources = chat.sources = sources
    jobs.session_service = chat
    product_api = ProductApi(jobs, chat, runs_root / 'api')
    version_files = sorted([*ROOT.glob('*.py'), *STATIC.glob('*.js'), *STATIC.glob('*.css'), *STATIC.glob('*.html')])
    build_id = hashlib.sha256(''.join(
        file.relative_to(ROOT).as_posix() + ':' + hashlib.sha256(file.read_bytes()).hexdigest()
        for file in version_files if file.is_file()).encode()).hexdigest()

    def source_config():
        base = providers.config(Config.from_env()) if providers else Config.from_env()
        return sources.configure(base)

    def memory_path():
        return Path(os.environ.get('SG_MEMORY_STORE') or Path.home() / '.scholarguide' / 'memory.sqlite3')

    def existing_memory(path):
        try:
            path.stat()
        except FileNotFoundError:
            return False
        # Never initialize an existing empty or unrelated database.
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=1)) as con:
            objects = con.execute("SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            if objects not in ([('preferences', 'table')], [('memory_metadata', 'table'), ('preferences', 'table')]):
                raise PreferenceError('Invalid store.')
        return True

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *args):
            pass

        def _allowed(self) -> bool:
            host = self.headers.get('Host', '')
            valid = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if host not in valid:
                self._json(403, {'error': '只允许从本机地址访问。'})
                return False
            origin = self.headers.get('Origin')
            if origin and origin != f'http://{host}':
                self._json(403, {'error': '请求来源不匹配。请在本机网页中操作。'})
                return False
            return True

        def _send(self, status: int, body: bytes, content_type: str, download: str | None = None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            if download:
                self.send_header('Content-Disposition', f'attachment; filename="{download}"')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (ConnectionError, OSError):
                # Browser aborts are routine (poll/refresh/close) and must not
                # print a server traceback or take down the request thread.
                pass

        def _json(self, status: int, data: dict):
            self._send(status, json.dumps(data, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8')

        def _memory_error(self, status, code, message):
            self._json(status, {'api_version': 'v1', 'error': {
                'code': code, 'message': message, 'retryable': False}})

        def _memory_route(self, path, method, raw=None):
            base = '/api/v1/memory/preferences'
            if path != base and not path.startswith(base + '/'):
                return False
            if method == 'GET' and path == base:
                try:
                    db = memory_path()
                    exists = existing_memory(db)
                    if not exists:
                        db.parent.mkdir(parents=True, exist_ok=True)
                    with MemoryStore(db, create=not exists) as store:
                        snapshot = store.snapshot()
                    self._json(200, {'api_version': 'v1', **snapshot})
                except Exception:
                    self._memory_error(500, 'memory_unavailable', 'Local preference operation failed; read preferences again before deciding what to do.')
                return True
            if method != 'POST' or path == base:
                return False
            match = re.fullmatch(re.escape(base) + r'/([^/]+)(?:/(revoke|delete))?', path)
            if not match:
                self._memory_error(400, 'invalid_request', 'Invalid preference request.')
                return True
            key, action = match.group(1), match.group(2)
            if type(raw) is dict and 'store_id' not in raw:
                self._memory_error(409, 'revision_conflict', 'Preference identity required; read preferences again before deciding what to do.')
                return True
            try:
                _key(key)
                fields = {'expected_revision', 'store_id'} if action else {'value', 'consent', 'expires_at', 'expected_revision', 'store_id'}
                if type(raw) is not dict or set(raw) != fields:
                    raise PreferenceError('Invalid request.')
                _revision(raw['expected_revision'])
                if type(raw['store_id']) is not str or not re.fullmatch(r'[0-9a-f]{64}', raw['store_id']):
                    raise PreferenceError('Invalid identity.')
                if not action:
                    _encode(key, raw['value'])
                    if raw['consent'] is not True or not _number(raw['expires_at']) or raw['expires_at'] <= time.time():
                        raise PreferenceError('Invalid grant.')
            except (PreferenceError, OverflowError, TypeError, ValueError):
                self._memory_error(400, 'invalid_request', 'Invalid preference request.')
                return True
            try:
                db = memory_path()
                if not existing_memory(db):
                    raise RevisionConflict('Conflict.')
                with MemoryStore(db, create=False) as store:
                    if action:
                        revision = getattr(store, action)(key, expected_revision=raw['expected_revision'], expected_store_id=raw['store_id'])
                    else:
                        revision = store.grant(key, raw['value'], consent=raw['consent'],
                                               expires_at=raw['expires_at'], expected_revision=raw['expected_revision'], expected_store_id=raw['store_id'])
                self._json(200, {'api_version': 'v1', 'revision': revision})
            except RevisionConflict:
                self._memory_error(409, 'revision_conflict', 'Preference changed; read preferences again before deciding what to do.')
            except Exception:
                self._memory_error(500, 'memory_unavailable', 'Local preference operation failed; read preferences again before deciding what to do.')
            return True

        def _cursor(self):
            query = parse_qs(urlsplit(self.path).query)
            try:
                return max(0, int(query.get('after', ['0'])[0]),
                           int(self.headers.get('Last-Event-ID') or 0))
            except (TypeError, ValueError):
                raise ApiError('Event cursor must be a nonnegative integer.', field='after') from None

        def _streaming(self):
            query = parse_qs(urlsplit(self.path).query)
            return 'text/event-stream' in self.headers.get('Accept', '') or query.get('stream', [''])[0] in ('1', 'true')

        def _event_response(self, loader, state, *, operation_id='', session_id='', turn=None):
            try:
                cursor = self._cursor()
            except ApiError as exc:
                self._json(400, error_payload(exc))
                return
            initial = loader(cursor)
            if initial is None:
                self._json(404, error_payload(ApiError('Operation or session not found.', code='not_found'), 404))
                return
            def normalized(rows):
                return [normalize_event(row, operation_id=operation_id, session_id=session_id,
                                        turn=turn, index=index) for index, row in enumerate(rows, 1)]
            if not self._streaming():
                rows = normalized(initial)
                self._json(200, {'events': rows, 'after': rows[-1]['seq'] if rows else cursor})
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache, no-transform')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_header('Connection', 'close')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            started, heartbeat = time.monotonic(), time.monotonic()
            rows = initial
            try:
                while True:
                    for event in normalized(rows or []):
                        if event['seq'] <= cursor:
                            continue
                        packet = json.dumps(event, ensure_ascii=False, separators=(',', ':'))
                        self.wfile.write(f'id: {event["seq"]}\nevent: trace\ndata: {packet}\n\n'.encode('utf-8'))
                        cursor = event['seq']
                    current = state() or {}
                    running = bool(current.get('running')) or (current.get('state') or current.get('status')) in ('running', 'queued', 'stopping', 'resuming')
                    if not running:
                        # Re-read the committed tail after observing terminal state:
                        # completion can race with the earlier cursor read.
                        tail = normalized(loader(cursor) or [])
                        for event in tail:
                            if event['seq'] > cursor:
                                packet = json.dumps(event, ensure_ascii=False, separators=(',', ':'))
                                self.wfile.write(f'id: {event["seq"]}\nevent: trace\ndata: {packet}\n\n'.encode('utf-8'))
                                cursor = event['seq']
                        # Persisted results, not a visual timer, determine completion.
                        done = {'after': cursor, 'state': current.get('state') or current.get('status') or 'completed'}
                        self.wfile.write(('event: done\ndata: ' + json.dumps(done) + '\n\n').encode())
                        self.wfile.flush()
                        return
                    if time.monotonic() - heartbeat >= 5:
                        self.wfile.write(b': heartbeat\n\n')
                        heartbeat = time.monotonic()
                    self.wfile.flush()
                    if time.monotonic() - started >= 45:
                        return  # EventSource reconnects with Last-Event-ID.
                    time.sleep(.15)
                    rows = loader(cursor)
            except (ConnectionError, OSError):
                return  # A disconnected observer never cancels or resubmits work.
            finally:
                self.close_connection = True

        def _sources_route(self, path, method, raw=None):
            path = path.replace('/api/v1/sources', '/api/sources', 1)
            if path == '/api/sources':
                try:
                    if method == 'GET':
                        self._json(200, sources.public_state())
                    elif method == 'POST':
                        if not isinstance(raw, dict):
                            raise GuideError('Source configuration must be an object.')
                        saved = sources.upsert(raw)
                        self._json(200, {'source': saved, **sources.public_state()})
                    else:
                        return False
                except GuideError as exc:
                    self._json(400, error_payload(exc))
                return True
            quota_match = re.fullmatch(r'/api/sources/lune/quota(?:/(reconcile))?', path)
            if quota_match:
                try:
                    if method == 'GET' and not quota_match.group(1):
                        value = sources.lune_quota_status()
                    elif method == 'POST' and quota_match.group(1) == 'reconcile':
                        value = sources.reconcile_lune_quota(raw)
                    elif method == 'POST' and not quota_match.group(1):
                        value = sources.confirm_lune_quota(raw)
                    else:
                        self._json(405, error_payload(ApiError('Unsupported method.', code='method_not_allowed'), 405))
                        return True
                    self._json(200, value)
                except GuideError as exc:
                    self._json(400, error_payload(exc))
                return True
            legacy_match = re.fullmatch(r'/api/sources/([a-z0-9_]+)/inflight/legacy', path)
            if legacy_match:
                try:
                    if method == 'GET':
                        value = sources.legacy_inflight_status(legacy_match.group(1))
                    elif method == 'POST':
                        value = sources.reconcile_legacy_inflight(legacy_match.group(1), raw)
                    else:
                        self._json(405, error_payload(ApiError('Unsupported method.', code='method_not_allowed'), 405))
                        return True
                    self._json(200, value)
                except GuideError as exc:
                    self._json(400, error_payload(exc))
                return True
            match = re.fullmatch(r'/api/sources/([a-z0-9_]+)/(?:(test|catalog|skills)(?:/([a-z0-9_]+))?)', path)
            if not match:
                return False
            name, action, skill = match.groups()
            try:
                config = source_config()
                if action == 'test' and method == 'POST':
                    if not isinstance(raw, dict):
                        raise GuideError('Probe must be an object.')
                    query = raw.get('query') or 'retrieval augmented generation'
                    if not isinstance(query, str) or len(query) > 128:
                        raise GuideError('Probe query must have at most 128 characters.')
                    value = sources.probe(name, config, query=query)
                elif action == 'catalog' and method == 'GET':
                    value = sources.catalog(name, config)
                elif action == 'skills' and method == 'GET' and not skill:
                    value = sources.skills(name, config)
                elif action == 'skills' and method == 'POST' and skill:
                    if not isinstance(raw, dict):
                        raise GuideError('Skill arguments must be an object.')
                    value = sources.invoke(name, skill, raw.get('arguments', raw), config)
                else:
                    self._json(405, error_payload(ApiError('Unsupported method.', code='method_not_allowed'), 405))
                    return True
                self._json(200, value)
            except GuideError as exc:
                payload = error_payload(exc)
                retained = getattr(exc, 'partial_source_result', None)
                if (action in ('catalog', 'skills') and isinstance(retained, RetainedSourceResult)
                        and retained.get('name') == name and retained.get('audit_status') == 'failed'
                        and getattr(exc, 'remote_outcome', None) == 'completed'
                        and getattr(exc, 'retry_safe', None) is False):
                    payload['partial_result'] = dict(retained)
                self._json(400, payload)
            return True

        def _v1_get(self, path):
            if path in ('/api/v1/openapi.json', '/openapi.json'):
                from api_schema import openapi_document
                self._json(200, openapi_document())
                return True
            match = re.fullmatch(r'/api/v1/operations/([a-f0-9]{32})(?:/(events|result))?', path)
            if not match:
                return False
            identity, resource = match.groups()
            value = product_api.snapshot(identity)
            if value is None:
                self._json(404, error_payload(ApiError('Operation not found.', code='not_found'), 404))
            elif resource == 'events':
                self._event_response(lambda after: product_api.events(identity, after),
                                     lambda: product_api.snapshot(identity), operation_id=identity,
                                     session_id=value.get('session_id') or '', turn=value.get('turn'))
            elif resource == 'result':
                self._json(200 if value.get('result') is not None else 409, value)
            else:
                self._json(200, value)
            return True

        def _v1_post(self, path, raw):
            if path in ('/api/v1/search', '/api/v1/chat'):
                try:
                    value = product_api.submit(path.rsplit('/', 1)[1], raw,
                                               key=self.headers.get('Idempotency-Key'))
                    self._json(202, value)
                except (GuideError, ValueError, TypeError) as exc:
                    status = getattr(exc, 'status', 409 if isinstance(exc, TurnRunning) else 400)
                    self._json(status, {'api_version': 'v1', 'error': execution_error(exc)}
                               if has_outcome(exc) else error_payload(exc, status))
                return True
            match = re.fullmatch(r'/api/v1/operations/([a-f0-9]{32})/cancel', path)
            if match:
                value = product_api.cancel(match.group(1))
                self._json(200, value) if value is not None else self._json(404, error_payload(ApiError('Operation not found.'), 404))
                return True
            return False

        def _artifact(self, result: dict, artifact: str, prefix: str):
            if artifact == 'report.md':
                content, kind = markdown(result), 'text/markdown'
            elif artifact == 'references.bib':
                content, kind = bibtex(result), 'application/x-bibtex'
            else:
                content, kind = json.dumps(result, ensure_ascii=False, indent=2), 'application/json'
            self._send(200, content.encode('utf-8'), kind + '; charset=utf-8', prefix + artifact)

        def do_GET(self):
            if not self._allowed():
                return
            location = urlsplit(self.path)
            path = location.path
            if self._memory_route(path, 'GET') or self._sources_route(path, 'GET') or self._v1_get(path):
                return
            # Keep bookmarked demo links usable after introducing the home page.
            # The new page never starts a search just by being opened.
            if path == '/' and 'demo' in parse_qs(location.query, keep_blank_values=True):
                self.send_response(302)
                self.send_header('Location', '/search?' + location.query)
                self.send_header('Content-Length', '0')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                return
            assets = {'/': ('landing.html', 'text/html'),
                      '/search': ('index.html', 'text/html'),
                      '/landing.css': ('landing.css', 'text/css'),
                      '/landing-demo.css': ('landing-demo.css', 'text/css'),
                      '/landing.js': ('landing.js', 'text/javascript'),
                      '/app.js': ('app.js', 'text/javascript'),
                      '/style.css': ('style.css', 'text/css'),
                      '/chat': ('chat.html', 'text/html'),
                      '/chat.js': ('chat.js', 'text/javascript'),
                      '/research.js': ('research.js', 'text/javascript'),
                      '/activity': ('activity.html', 'text/html'),
                      '/activity.js': ('activity.js', 'text/javascript'),
                      '/activity-story.js': ('activity-story.js', 'text/javascript'),
                      '/settings': ('settings.html', 'text/html'),
                      '/settings.js': ('settings.js', 'text/javascript'),
                      '/memory.js': ('memory.js', 'text/javascript'),
                      '/memory.css': ('memory.css', 'text/css'),
                      '/sources': ('sources.html', 'text/html'),
                      '/sources.js': ('sources.js', 'text/javascript'),
                      '/sources.css': ('sources.css', 'text/css'),
                      '/trace.js': ('trace.js', 'text/javascript'),
                      '/trace.css': ('trace.css', 'text/css'),
                      '/theme.js': ('theme.js', 'text/javascript')}
            if path in assets:
                name, kind = assets[path]
                self._send(200, (STATIC / name).read_bytes(), kind + '; charset=utf-8')
                return
            if path.startswith('/assets/logos/'):
                # Third-party source marks, served from the local copy only.
                name = path[len('/assets/logos/'):]
                target = STATIC / 'assets' / 'logos' / name
                if (not name or '/' in name or '\\' in name or '..' in name
                        or not target.is_file()):
                    self._send(404, b'not found', 'text/plain; charset=utf-8')
                    return
                kind = 'image/svg+xml' if name.endswith('.svg') else 'image/png'
                self._send(200, target.read_bytes(), kind)
                return
            if path == '/api/providers':
                self._json(200, providers.public_state())
                return
            if path == '/api/config':
                try:
                    config = providers.config(Config.from_env()) if providers else Config.from_env()
                    config.validate()
                    active = providers.active() if providers else None
                    # A custom profile is an explicit choice and may target a
                    # keyless compatible endpoint on another machine. Keep the
                    # missing-key setup hint for the environment fallback.
                    if (config.provider == 'openai' and not config.api_key
                            and (active is None or active.source == 'env')
                            and urlsplit(config.base_url).hostname not in ('localhost', '127.0.0.1')):
                        raise GuideError('Missing API key')
                    self._json(200, {'ready': True, 'build_id': build_id, 'version': '2026.10.28', 'provider': active.name if active else config.provider,
                                     'profile_id': config.profile_id, 'protocol': config.protocol,
                                     'model': config.model, 'source': config.source,
                                     'research_default_source': 'multi', 'research_mode': True,
                                     'task_timeout': config.task_timeout})
                except GuideError:
                    self._json(200, {'ready': False, 'build_id': build_id, 'version': '2026.10.28', 'model': None, 'source': 'arxiv',
                                     'research_default_source': 'multi', 'research_mode': True,
                                     'message': '真实检索需要完成本机 .env 配置；离线演示可直接使用。'})
                return
            if path == '/api/research/catalog':
                from research_policy import curated_venue_catalog
                self._json(200, curated_venue_catalog())
                return
            if path == '/api/activity':
                query = urlsplit(self.path).query
                q = ''
                for part in query.split('&'):
                    if part.startswith('q='):
                        from urllib.parse import unquote_plus
                        q = unquote_plus(part.split('=', 1)[1])
                self._json(200, activity.index(q))
                return
            run_match = re.fullmatch(r'/api/activity/runs/([a-f0-9]{32})', path)
            if run_match:
                detail = activity.run(run_match.group(1))
                self._json(200, detail) if detail else self._json(404, {'error': '运行不存在。'})
                return
            activity_match = re.fullmatch(r'/api/activity/sessions/([A-Za-z0-9_-]{1,80})', path)
            if activity_match:
                detail = activity.session(activity_match.group(1))
                self._json(200, detail) if detail else self._json(404, {'error': '会话不存在。'})
                return
            if path == '/api/chat/sessions':
                self._json(200, {'sessions': chat.list_sessions()})
                return
            export_match = re.fullmatch(
                r'/api/chat/sessions/([a-f0-9]{32})/turns/([0-9]+)/(report\.md|result\.json|references\.bib)', path)
            if export_match:
                result = chat.turn_result(export_match.group(1), int(export_match.group(2)))
                if result is None:
                    self._json(404, {'error': '这一轮尚未完成，或会话不存在。'})
                else:
                    self._artifact(result, export_match.group(3), 'scholarguide-turn-' + export_match.group(2) + '-')
                return
            job_events = re.fullmatch(r'/api/jobs/([a-f0-9]{32})/events', path)
            if job_events:
                identity = job_events.group(1)
                def job_rows(after):
                    value = jobs.snapshot(identity)
                    if value is None:
                        return None
                    return [row for index, row in enumerate(value.get('trace') or [], 1)
                            if int(row.get('seq') or row.get('step') or index) > after]
                self._event_response(job_rows, lambda: jobs.snapshot(identity), operation_id=identity)
                return
            event_match = re.fullmatch(r'/api/chat/sessions/([a-f0-9]{32})/events', path)
            if event_match:
                identity = event_match.group(1)
                self._event_response(lambda after: chat.events(identity, after),
                                     lambda: chat.snapshot(identity), session_id=identity)
                return
            session_match = re.fullmatch(r'/api/chat/sessions/([a-f0-9]{32})', path)
            if session_match:
                snapshot = chat.snapshot(session_match.group(1))
                if snapshot is None:
                    self._json(404, {'error': '会话不存在（可能已被清理或服务已重启）。'})
                else:
                    self._json(200, snapshot)
                return
            match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})(?:/(report\.md|result\.json|references\.bib))?', path)
            if not match:
                self._json(404, {'error': '页面或任务不存在。'})
                return
            job = jobs.snapshot(match.group(1))
            if job is None:
                self._json(404, {'error': '任务已过期或服务已重启，请重新运行。已保存报告仍在 runs 目录。'})
                return
            artifact = match.group(2)
            if not artifact:
                self._json(200, job)
                return
            if job['result'] is None:
                self._json(409, {'error': '任务还没有可下载的结果。'})
                return
            self._artifact(job['result'], artifact, 'scholarguide-')

        def do_DELETE(self):
            if not self._allowed():
                return
            path = urlsplit(self.path).path
            match = re.fullmatch(r'/api/providers/([a-z0-9_-]{1,32})', path)
            if match:
                try:
                    providers.delete(match.group(1))
                    self._json(200, {'deleted': match.group(1)})
                except GuideError as exc:
                    self._json(400, {'error': str(exc)})
                return
            session_match = re.fullmatch(r'/api/chat/sessions/([a-f0-9]{32})', path)
            if session_match:
                try:
                    payload = chat.delete(session_match.group(1))
                except DeletionUnavailable as exc:
                    self._json(503, {'error': str(exc), 'error_code': exc.code})
                    return
                except TurnRunning as exc:
                    self._json(409, {'error': str(exc), 'error_code': exc.code})
                    return
                except GuideError as exc:
                    self._json(500, {'error': str(exc)})
                    return
                if payload is None:
                    self._json(404, {'error': '会话不存在。'})
                else:
                    self._json(200, payload)
                return
            job_match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})', path)
            if job_match:
                try:
                    payload = jobs.delete(job_match.group(1))
                except DeletionUnavailable as exc:
                    self._json(503, {'error': str(exc), 'error_code': exc.code})
                    return
                except TurnRunning as exc:
                    self._json(409, {'error': str(exc), 'error_code': exc.code})
                    return
                except GuideError as exc:
                    self._json(500, {'error': str(exc)})
                    return
                if payload is None:
                    self._json(404, {'error': '运行记录不存在。'})
                else:
                    self._json(200, payload)
                return
            self._json(404, {'error': '接口不存在。'})

        def do_POST(self):
            if not self._allowed():
                return
            path = urlsplit(self.path).path
            try:
                raw = _read_json(self, reject_duplicates=(path.endswith('/presentation') or path == '/api/v1/memory/preferences' or
                                                         path.startswith('/api/v1/memory/preferences/')))
            except GuideError as exc:
                status = 415 if 'application/json' in str(exc) else 413
                self._json(status, {'error': str(exc)})
                return
            except (ValueError, TypeError, OSError):
                self._json(400, {'error': '请求格式无效。'})
                return
            if self._memory_route(path, 'POST', raw) or self._sources_route(path, 'POST', raw) or self._v1_post(path, raw):
                return
            presentation_match = re.fullmatch(r'/api/chat/sessions/([a-f0-9]{32})/turns/([1-9][0-9]{0,5})/presentation', path)
            if presentation_match:
                if type(raw) is not dict or set(raw) != {'selected'} or raw['selected'] is not True:
                    self._json(400, {'status': 'not_applied', 'paper_ids': []})
                else:
                    payload = chat.presentation(presentation_match.group(1),
                                                int(presentation_match.group(2)), memory_path())
                    self._json(200, payload)
                return
            cancel_match = re.fullmatch(r'/api/(jobs|chat/sessions)/([a-f0-9]{32})/cancel', path)
            if cancel_match:
                if not isinstance(raw, dict):
                    self._json(400, {'error': '请求必须是 JSON 对象。'})
                    return
                service = jobs if cancel_match.group(1) == 'jobs' else chat
                payload = service.cancel(cancel_match.group(2))
                if payload is None:
                    self._json(404, {'error': '任务或会话不存在。'})
                else:
                    self._json(200, payload)
                return
            resume_match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})/resume', path)
            if resume_match:
                if not isinstance(raw, dict) or raw:
                    self._json(400, {'error': '继续任务只接受空 JSON 对象，不能修改原任务。'})
                    return
                try:
                    job_id = jobs.resume(resume_match.group(1))
                except GuideError as exc:
                    self._json(409, {'error': str(exc)})
                    return
                self._json(202, {'id': job_id})
                return
            if path == '/api/providers':
                if not isinstance(raw, dict):
                    self._json(400, {'error': 'Profile must be a JSON object.'})
                    return
                try:
                    profile = providers.upsert(raw, activate=bool(raw.get('activate')))
                    self._json(200, {'active': providers.public_state()['active'],
                                     'profile': profile.public()})
                except GuideError as exc:
                    self._json(400, {'error': str(exc)})
                return
            if path == '/api/providers/test':
                if not isinstance(raw, dict):
                    self._json(400, {'error': 'Profile must be a JSON object.'})
                    return
                try:
                    self._json(200, providers.test(raw))
                except GuideError as exc:
                    self._json(400, {'error': str(exc)})
                return
            if path == '/api/providers/activate':
                if not isinstance(raw, dict):
                    self._json(400, {'error': 'Profile must be a JSON object.'})
                    return
                try:
                    profile = providers.activate(str(raw.get('id') or ''))
                    self._json(200, {'active': profile.public()['id'],
                                     'profile': profile.public()})
                except GuideError as exc:
                    self._json(400, {'error': str(exc)})
                return
            if path == '/api/jobs':
                try:
                    data = validate_input(raw)
                except GuideError as exc:
                    self._json(400, {'error': str(exc)})
                    return
                try:
                    job_id = jobs.submit(data)
                except GuideError as exc:
                    self._json(409, {'error': str(exc)})
                    return
                self._json(202, {'id': job_id})
                return
            if path == '/api/chat/sessions':
                try:
                    payload = chat.create(raw)
                except GuideError as exc:
                    self._json(400, {'error': str(exc)})
                    return
                self._json(201, payload)
                return
            fork_match = re.fullmatch(r'/api/chat/sessions/([a-f0-9]{32})/fork', path)
            if fork_match:
                source = raw.get('source') if isinstance(raw, dict) else ''
                try:
                    payload = chat.fork_source(fork_match.group(1), str(source or ''))
                except KeyError:
                    self._json(404, {'error': '会话不存在（可能已被清理或服务已重启）。'})
                    return
                except GuideError as exc:
                    self._json(400, {'error': str(exc)})
                    return
                self._json(201, payload)
                return
            rename_match = re.fullmatch(r'/api/chat/sessions/([a-f0-9]{32})/rename', path)
            if rename_match:
                try:
                    payload = chat.rename(rename_match.group(1), raw)
                except TurnRunning as exc:
                    self._json(409, {'error': str(exc), 'error_code': exc.code})
                    return
                except GuideError as exc:
                    self._json(400, {'error': str(exc)})
                    return
                if payload is None:
                    self._json(404, {'error': '会话不存在。'})
                else:
                    self._json(200, payload)
                return
            archive_match = re.fullmatch(
                r'/api/chat/sessions/([a-f0-9]{32})/(archive|restore)', path)
            if archive_match:
                try:
                    payload = chat.set_archived(archive_match.group(1),
                                                archive_match.group(2) == 'archive')
                except KeyError:
                    self._json(404, {'error': '会话不存在（可能已被清理或服务已重启）。'})
                    return
                except TurnRunning as exc:
                    self._json(409, {'error': str(exc), 'error_code': exc.code})
                    return
                if payload is None:
                    self._json(404, {'error': '会话不存在。'})
                else:
                    self._json(200, payload)
                return
            recovery_match = re.fullmatch(
                r'/api/chat/sessions/([a-f0-9]{32})/recover', path)
            if recovery_match:
                code = raw.get('action') if isinstance(raw, dict) else ''
                if code not in ('relax_year', 'relax_venue', 'lower_target'):
                    self._json(400, {'error': 'unsupported recovery action'})
                    return
                try:
                    payload = chat.recover(recovery_match.group(1), str(code), stream=raw.get('stream', False))
                except KeyError:
                    self._json(404, {'error': '会话不存在（可能已被清理或服务已重启）。'})
                    return
                except TurnRunning as exc:
                    self._json(409, {'error': str(exc), 'error_code': exc.code})
                    return
                except SessionArchived as exc:
                    self._json(409, {'error': str(exc), 'error_code': exc.code})
                    return
                except FeedbackBudgetExhausted as exc:
                    self._json(409, {'error': str(exc), 'error_code': exc.code,
                                     'can_start_new_session': True})
                    return
                except GuideError as exc:
                    self._json(400, {'error': str(exc)})
                    return
                self._json(200, payload)
                return
            match = re.fullmatch(r'/api/chat/sessions/([a-f0-9]{32})/messages', path)
            if match:
                try:
                    payload = chat.send(match.group(1), raw)
                except KeyError:
                    self._json(404, {'error': '会话不存在（可能已被清理或服务已重启）。'})
                    return
                except TurnRunning as exc:
                    self._json(409, {'error': str(exc), 'error_code': exc.code})
                    return
                except SessionArchived as exc:
                    self._json(409, {'error': str(exc), 'error_code': exc.code,
                                     'can_restore': True})
                    return
                except FeedbackBudgetExhausted as exc:
                    self._json(409, {'error': str(exc), 'error_code': exc.code,
                                     'can_start_new_session': True})
                    return
                except GuideError as exc:
                    self._json(400, {'error': str(exc)})
                    return
                self._json(200, payload)
                return
            self._json(404, {'error': '接口不存在。'})

    class ProductServer(ThreadingHTTPServer):
        def server_close(self):
            super().server_close()
            chat.close()
            if own_sources:
                sources.close()

    server = ProductServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.product_api = product_api
    server.sources = sources
    return server


def serve(port: int = 8765, open_browser: bool = False, runs: Path | None = None) -> None:
    if not 1 <= port <= 65535:
        raise GuideError('端口必须在 1–65535 之间。')
    try:
        if runs is not None:
            runs = Path(runs)
            providers = ProviderStore()
            server = create_server(port, jobs=Jobs(output=runs, providers=providers),
                                   chat=ChatService(output=runs / 'web', providers=providers),
                                   activity=ActivityFeed(runs), providers=providers)
        else:
            server = create_server(port)
    except OSError:
        raise GuideError(f'无法监听端口 {port}，可用 --port 选择另一个端口。') from None
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'ScholarGuide 网页已启动：{url}\n按 Ctrl+C 停止服务。', flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n网页服务已停止。', flush=True)
    finally:
        server.server_close()
