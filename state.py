"""SQLite state for the stateful multi-turn workflow.

Implements the storage layer described in docs/ARCHITECTURE.md §4.E / §5 and the
invariants in docs/DESIGN_INVARIANTS.md. Stdlib only; no network access.

Design notes
------------
* ``task_versions.revision`` is an audit trail and never decides reuse.
* Reuse is decided by ``assessment_key = (paper_id, content_hash, semantic_key)``
  where ``semantic_key`` covers every field that can change a judgement.
* A judgement with ``relevance >= 1`` must carry a verified verbatim quote (I1).
* Cost is settled as reserve -> settle, so ``settled + refunded == reserved``
  holds after every statement (I5).
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid
from typing import Any, Iterator, Mapping, Sequence

from common import GuideError, merge_publication_records, normalize_venue, utc_now

SCHEMA_PATH = Path(__file__).with_name('state_schema.sql')
MODEL_VERSION = 'v2'


class InvariantError(GuideError):
    """A documented invariant was violated; the caller must not persist the write."""


# --------------------------------------------------------------------------
# Keys and invariants
# --------------------------------------------------------------------------

def normalize_text(value: str | None) -> str:
    """Collapse whitespace only; identity must not depend on formatting."""
    return ' '.join((value or '').split())


def content_hash(title: str, abstract: str) -> str:
    payload = json.dumps(
        ['content', normalize_text(title).casefold(), normalize_text(abstract).casefold()],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def semantic_key(topic: str, purpose: str = '', category: str = 'none',
                 model: str = '', prompt_version: str = '', venue: str = '',
                 year_from: int | None = None, year_to: int | None = None) -> str:
    """Everything that can change a relevance/category judgement, and nothing else."""
    payload = json.dumps(
        ['semantic', normalize_text(topic).casefold(), normalize_text(purpose).casefold(),
         (category or 'none').casefold(), model or '', prompt_version or '',
         normalize_venue(venue), year_from, year_to],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def assessment_key(paper_id: str, content: str, semantic: str) -> tuple[str, str, str]:
    return (paper_id, content, semantic)


def assert_assessment_evidence(relevance: int, evidence_verified: bool,
                              evidence_quote: str) -> None:
    """I1: a non-zero relevance needs a verified, non-empty quote."""
    if relevance not in (0, 1, 2):
        raise InvariantError('relevance must be 0, 1 or 2.')
    if relevance >= 1:
        if not evidence_verified:
            raise InvariantError('I1: relevance >= 1 requires evidence_verified=true.')
        if len(normalize_text(evidence_quote)) < 12:
            raise InvariantError('I1: relevance >= 1 requires a verbatim quote of >= 12 characters.')


def assert_reuse_key(stored: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    """I2: every component of computed_with must match before a judgement is reused."""
    for field in ('content_hash', 'semantic_key', 'model', 'prompt_version'):
        if (stored.get(field) or '') != (expected.get(field) or ''):
            raise InvariantError(f'I2: computed_with.{field} differs; recompute instead of reusing.')


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

class StateStore:
    """Thin repository over one SQLite database.

    Every multi-step mutation should run inside ``with store.tx():`` so that the
    state change, its event and its budget settlement commit together.
    """

    def __init__(self, path: str | Path, *, init: bool = True) -> None:
        self.path = str(path)
        # The web service creates a session in the request thread and closes it on
        # shutdown; access is serialised by the caller's lock.
        self.con = sqlite3.connect(self.path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute('PRAGMA foreign_keys = ON')
        self._tx_depth = 0
        if init:
            self.init_schema()

    def init_schema(self) -> None:
        self.con.executescript(SCHEMA_PATH.read_text(encoding='utf-8'))
        columns = {row[1] for row in self.con.execute('PRAGMA table_info(papers)')}
        migrations = {
            'authors_json': "TEXT NOT NULL DEFAULT '[]'",
            'doi': "TEXT NOT NULL DEFAULT ''",
            'arxiv_id': "TEXT NOT NULL DEFAULT ''",
            'open_access_url': "TEXT NOT NULL DEFAULT ''",
            'abstract_source': "TEXT NOT NULL DEFAULT ''",
            'metadata_only': 'INTEGER NOT NULL DEFAULT 0',
            'sources_json': "TEXT NOT NULL DEFAULT '[]'",
            'authority': 'REAL NOT NULL DEFAULT 0',
            'snippet': "TEXT NOT NULL DEFAULT ''",
            'provenance_conflict': 'INTEGER NOT NULL DEFAULT 0',
            'publication_json': "TEXT NOT NULL DEFAULT '{}'",
            'citation_count': 'INTEGER',
            'citation_source': "TEXT NOT NULL DEFAULT ''",
            'citation_observed_at': "TEXT NOT NULL DEFAULT ''",
        }
        for column, ddl in migrations.items():
            if column not in columns:
                self.con.execute(f'ALTER TABLE papers ADD COLUMN {column} {ddl}')
        a_columns = {row[1] for row in self.con.execute('PRAGMA table_info(assessments)')}
        if 'paper_type' not in a_columns:
            self.con.execute("ALTER TABLE assessments ADD COLUMN paper_type TEXT NOT NULL DEFAULT 'unknown'")
        if 'fit_score' not in a_columns:
            self.con.execute('ALTER TABLE assessments ADD COLUMN fit_score INTEGER NOT NULL DEFAULT 50')
        self.con.commit()

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self.con.close()

    def __del__(self):
        try:
            self.con.close()
        except Exception:
            pass

    def __enter__(self) -> 'StateStore':
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def commit(self) -> None:
        """Commit unless an outer tx() owns the transaction."""
        if self._tx_depth == 0:
            self.con.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Group writes. Nested calls join the outermost transaction."""
        self._tx_depth += 1
        try:
            yield self.con
        except Exception:
            if self._tx_depth == 1:
                self.con.rollback()
            raise
        finally:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self.con.commit()

    # -- sessions ----------------------------------------------------------
    def create_session(self, budget: Mapping[str, Any] | None = None,
                       session_id: str | None = None) -> str:
        sid = session_id or 'sess_' + uuid.uuid4().hex[:12]
        now = utc_now()
        self.con.execute(
            'INSERT OR IGNORE INTO sessions(session_id, created_at, updated_at, status, budget_json) '
            'VALUES (?, ?, ?, ?, ?)',
            (sid, now, now, 'active', json.dumps(dict(budget or {}), ensure_ascii=False)),
        )
        self.commit()
        return sid

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.con.execute('SELECT * FROM sessions WHERE session_id = ?', (session_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data['budget'] = json.loads(data.pop('budget_json') or '{}')
        return data

    def set_status(self, session_id: str, status: str) -> None:
        if status not in ('active', 'awaiting_user', 'finished', 'failed'):
            raise InvariantError(f'unknown session status: {status}')
        self.con.execute('UPDATE sessions SET status = ?, updated_at = ? WHERE session_id = ?',
                         (status, utc_now(), session_id))
        self.commit()

    # -- task versions -----------------------------------------------------
    def save_task(self, session_id: str, revision: int, task: Mapping[str, Any], *,
                  source_message: str | None = None,
                  change_summary: str | None = None) -> int:
        if revision < 1:
            raise InvariantError('revision must be >= 1')
        current = self.current_revision(session_id)
        if current is not None and revision < current:
            raise InvariantError(f'revision {revision} is older than current {current}')
        now = utc_now()
        payload = json.dumps(dict(task), ensure_ascii=False, sort_keys=True)
        with self.tx():
            self.con.execute(
                'INSERT INTO task_versions(session_id, revision, task_json, updated_at, source_message, change_summary) '
                'VALUES (?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(session_id, revision) DO UPDATE SET '
                'task_json=excluded.task_json, updated_at=excluded.updated_at, '
                'source_message=excluded.source_message, change_summary=excluded.change_summary',
                (session_id, revision, payload, now, source_message, change_summary),
            )
            self.con.execute(
                'INSERT INTO session_task(session_id, revision) VALUES (?, ?) '
                'ON CONFLICT(session_id) DO UPDATE SET revision=excluded.revision',
                (session_id, revision),
            )
            self.con.execute('UPDATE sessions SET updated_at = ? WHERE session_id = ?', (now, session_id))
        return revision

    def current_revision(self, session_id: str) -> int | None:
        row = self.con.execute('SELECT revision FROM session_task WHERE session_id = ?',
                               (session_id,)).fetchone()
        return None if row is None else int(row['revision'])

    def current_task(self, session_id: str) -> dict[str, Any] | None:
        row = self.con.execute(
            'SELECT t.revision, t.task_json FROM session_task st '
            'JOIN task_versions t ON t.session_id = st.session_id AND t.revision = st.revision '
            'WHERE st.session_id = ?', (session_id,)).fetchone()
        if row is None:
            return None
        return {'revision': int(row['revision']), 'task': json.loads(row['task_json'])}

    # -- papers and candidates --------------------------------------------
    def register_paper(self, paper: Mapping[str, Any]) -> dict[str, str]:
        paper_id = str(paper.get('paper_id') or paper.get('id') or '').strip()
        if not paper_id:
            raise InvariantError('paper_id is required')
        title = normalize_text(str(paper.get('title') or ''))
        abstract = normalize_text(str(paper.get('abstract') or ''))
        digest = content_hash(title, abstract)
        authors = json.dumps(list(paper.get('authors') or []), ensure_ascii=False)
        provenance = paper.get('sources') or [paper.get('source') or 'arxiv']
        sources = [str(x) for x in provenance if x]
        source_label = ','.join(sources) or 'arxiv'
        publication = paper.get('publication') if isinstance(paper.get('publication'), dict) else {}
        previous = self.con.execute('SELECT publication_json, sources_json FROM papers WHERE paper_id = ? AND content_hash = ?',
                                    (paper_id, digest)).fetchone()
        if previous:
            try:
                old_sources = json.loads(previous['sources_json'] or '[]')
            except (ValueError, TypeError):
                old_sources = []
            if isinstance(old_sources, list):
                sources = list(dict.fromkeys([str(x) for x in old_sources if x] + sources))
                source_label = ','.join(sources) or 'arxiv'
        if previous and publication:
            try:
                old = json.loads(previous['publication_json'] or '{}')
            except (ValueError, TypeError):
                old = {}
            if isinstance(old, dict) and old:
                records = lambda p: p.get('records') if isinstance(p.get('records'), list) else [p]
                publication = merge_publication_records(records(old) + records(publication),
                    list(old.get('conflicts') or []) + list(publication.get('conflicts') or []))
        if not publication and previous:
            try:
                publication = json.loads(previous['publication_json'] or '{}')
            except (ValueError, TypeError):
                publication = {}
        if not isinstance(publication, dict):
            publication = {}
        venue, year = paper.get('venue'), paper.get('year')
        if isinstance(publication, dict) and publication.get('verified') and publication.get('status') in ('accepted', 'published'):
            venue = publication.get('venue_name') or venue
            date = str(publication.get('date') or '')
            if len(date) >= 4 and date[:4].isdigit():
                year = int(date[:4])
        self.con.execute(
            'INSERT INTO papers(paper_id, content_hash, title, abstract, authors_json, url, year, venue, source, fetched_at, '
            'doi, arxiv_id, open_access_url, abstract_source, metadata_only, sources_json, authority, snippet, provenance_conflict, publication_json, citation_count, citation_source, citation_observed_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(paper_id, content_hash) DO UPDATE SET title=excluded.title, '
            "abstract=excluded.abstract, authors_json=CASE WHEN excluded.authors_json = '[]' THEN papers.authors_json ELSE excluded.authors_json END, "
            "url=CASE WHEN COALESCE(excluded.url, '') = '' THEN papers.url ELSE excluded.url END, "
            'year=COALESCE(excluded.year, papers.year), '
            "venue=CASE WHEN COALESCE(excluded.venue, '') = '' THEN papers.venue ELSE excluded.venue END, "
            'source=excluded.source, fetched_at=excluded.fetched_at, '
            "doi=CASE WHEN excluded.doi = '' THEN papers.doi ELSE excluded.doi END, "
            "arxiv_id=CASE WHEN excluded.arxiv_id = '' THEN papers.arxiv_id ELSE excluded.arxiv_id END, "
            "open_access_url=CASE WHEN excluded.open_access_url = '' THEN papers.open_access_url ELSE excluded.open_access_url END, "
            "abstract_source=CASE WHEN excluded.abstract_source = '' THEN papers.abstract_source ELSE excluded.abstract_source END, "
            'metadata_only=MIN(papers.metadata_only, excluded.metadata_only), '
            'sources_json=excluded.sources_json, authority=MAX(papers.authority, excluded.authority), '
            "snippet=CASE WHEN excluded.snippet = '' THEN papers.snippet ELSE excluded.snippet END, "
            'provenance_conflict=MAX(papers.provenance_conflict, excluded.provenance_conflict), '
            "publication_json=CASE WHEN excluded.publication_json = '{}' THEN papers.publication_json ELSE excluded.publication_json END, "
            "citation_count=CASE WHEN excluded.citation_count IS NULL OR (papers.citation_source='openalex' AND excluded.citation_source!='openalex') THEN papers.citation_count ELSE excluded.citation_count END, "
            "citation_source=CASE WHEN excluded.citation_count IS NULL OR (papers.citation_source='openalex' AND excluded.citation_source!='openalex') THEN papers.citation_source ELSE excluded.citation_source END, "
            "citation_observed_at=CASE WHEN excluded.citation_count IS NULL OR (papers.citation_source='openalex' AND excluded.citation_source!='openalex') THEN papers.citation_observed_at ELSE excluded.citation_observed_at END",
            (paper_id, digest, title, abstract, authors, paper.get('url'), year,
              venue, source_label, utc_now(), paper.get('doi') or '',
             paper.get('arxiv_id') or '', paper.get('open_access_url') or '',
             paper.get('abstract_source') or '', int(bool(paper.get('metadata_only'))),
             json.dumps(sources, ensure_ascii=False), float(paper.get('authority') or 0),
              paper.get('snippet') or '', int(bool(paper.get('provenance_conflict')) or bool(publication.get('conflicts'))),
              json.dumps(publication, ensure_ascii=False, sort_keys=True),
              paper.get('citation_count') if type(paper.get('citation_count')) is int and paper['citation_count'] >= 0 else None,
              paper.get('citation_source') or '', paper.get('citation_observed_at') or ''),
        )
        self.commit()
        return {'paper_id': paper_id, 'content_hash': digest}

    def add_candidate(self, session_id: str, paper: Mapping[str, Any], query: str = '') -> None:
        ref = self.register_paper(paper)
        self.con.execute(
            'INSERT INTO candidates(session_id, paper_id, content_hash, first_query, first_seen_at) '
            'VALUES (?, ?, ?, ?, ?) '
            'ON CONFLICT(session_id, paper_id) DO UPDATE SET content_hash=excluded.content_hash',
            (session_id, ref['paper_id'], ref['content_hash'], query, utc_now()),
        )
        self.commit()

    def record_query(self, session_id: str, revision: int, action_seq: int, query: str,
                     returned_count: int, new_count: int) -> None:
        self.con.execute(
            'INSERT INTO queries(session_id, revision, action_seq, query, returned_count, new_count, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (session_id, revision, action_seq, query, returned_count, new_count, utc_now()),
        )
        self.commit()

    # -- assessments -------------------------------------------------------
    def put_assessment(self, session_id: str, *, paper_id: str, content: str, semantic: str,
                       computed: Mapping[str, Any], relevance: int, category_status: str,
                       paper_type: str = 'unknown', fit_score: int = 50,
                       evidence_quote: str = '', evidence_verified: bool = False,
                       understanding_sentence: str | None = None,
                       unknown_reason: str | None = None) -> None:
        if type(fit_score) is not int or not 0 <= fit_score <= 100:
            raise InvariantError('fit_score must be an integer from 0 to 100.')
        assert_assessment_evidence(relevance, evidence_verified, evidence_quote)
        if relevance >= 1:
            paper = self.con.execute(
                'SELECT abstract FROM papers WHERE paper_id = ? AND content_hash = ?',
                (paper_id, content)).fetchone()
            if paper is None:
                raise InvariantError('I1: paper snapshot is missing; register the paper first.')
            if normalize_text(evidence_quote) not in normalize_text(paper['abstract']):
                raise InvariantError('I1: the quote is not a contiguous substring of this abstract.')
        for field in ('content_hash', 'semantic_key', 'model', 'prompt_version'):
            if field not in computed:
                raise InvariantError(f'computed_with requires {field}')
        if computed['content_hash'] != content or computed['semantic_key'] != semantic:
            raise InvariantError('computed_with does not match the assessment key')
        self.con.execute(
            'INSERT INTO assessments(paper_id, content_hash, semantic_key, computed_with, relevance, '
            'category_status, paper_type, understanding_sentence, evidence_quote, evidence_verified, unknown_reason, '
            'model, prompt_version, computed_at, fit_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(paper_id, content_hash, semantic_key) DO UPDATE SET '
            'computed_with=excluded.computed_with, relevance=excluded.relevance, '
            'category_status=excluded.category_status, paper_type=excluded.paper_type, '
            'understanding_sentence=excluded.understanding_sentence, '
            'evidence_quote=excluded.evidence_quote, evidence_verified=excluded.evidence_verified, '
            'unknown_reason=excluded.unknown_reason, model=excluded.model, '
            'prompt_version=excluded.prompt_version, computed_at=excluded.computed_at, fit_score=excluded.fit_score',
            (paper_id, content, semantic, json.dumps(dict(computed), ensure_ascii=False, sort_keys=True),
             int(relevance), category_status, paper_type or 'unknown', understanding_sentence,
             normalize_text(evidence_quote), int(bool(evidence_verified)), unknown_reason, computed.get('model'),
             computed.get('prompt_version'), utc_now(), fit_score),
        )
        self.commit()

    def get_assessment(self, paper_id: str, content: str, semantic: str) -> dict[str, Any] | None:
        row = self.con.execute(
            'SELECT * FROM assessments WHERE paper_id = ? AND content_hash = ? AND semantic_key = ?',
            (paper_id, content, semantic)).fetchone()
        return None if row is None else dict(row)

    def valid_assessments(self, session_id: str, paper_ids: Sequence[str], *, task: Mapping[str, Any],
                          model: str, prompt_version: str) -> dict[str, dict[str, Any]]:
        """Return paper_id -> row for judgements reusable under the current task."""
        semantic = semantic_key(str(task.get('topic') or ''), str(task.get('purpose') or ''),
                                str(task.get('category') or 'none'), model, prompt_version,
                                str(task.get('venue') or ''), task.get('year_from'),
                                task.get('year_to'))
        out: dict[str, dict[str, Any]] = {}
        for paper_id in paper_ids:
            row = self.con.execute(
                'SELECT a.*, p.title, p.abstract FROM candidates c '
                'JOIN papers p ON p.paper_id = c.paper_id AND p.content_hash = c.content_hash '
                'JOIN assessments a ON a.paper_id = c.paper_id AND a.content_hash = c.content_hash '
                'WHERE c.session_id = ? AND c.paper_id = ? AND a.semantic_key = ?',
                (session_id, paper_id, semantic)).fetchone()
            if row is None:
                continue
            data = dict(row)
            if int(data.get('relevance') or 0) >= 1:
                quote = normalize_text(str(data.get('evidence_quote') or ''))
                if (not data.get('evidence_verified') or len(quote) < 12
                        or quote not in normalize_text(str(data.get('abstract') or ''))):
                    continue
            try:
                assert_reuse_key({
                    'content_hash': data['content_hash'], 'semantic_key': data['semantic_key'],
                    'model': data['model'], 'prompt_version': data['prompt_version'],
                }, {'content_hash': data['content_hash'], 'semantic_key': semantic,
                    'model': model, 'prompt_version': prompt_version})
            except InvariantError:
                continue
            out[paper_id] = data
        return out

    def stale_paper_ids(self, session_id: str, paper_ids: Sequence[str], *, task: Mapping[str, Any],
                        model: str, prompt_version: str) -> list[str]:
        valid = self.valid_assessments(session_id, paper_ids, task=task, model=model,
                                       prompt_version=prompt_version)
        return [p for p in paper_ids if p not in valid]

    # -- events ------------------------------------------------------------
    def append_event(self, session_id: str, action: str, payload: Mapping[str, Any] | None = None,
                     revision: int | None = None) -> int:
        row = self.con.execute('SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM events WHERE session_id = ?',
                               (session_id,)).fetchone()
        seq = int(row['next'])
        self.con.execute(
            'INSERT INTO events(session_id, seq, revision, action, payload_json, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (session_id, seq, revision, action,
             json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True), utc_now()),
        )
        return seq

    def events(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.con.execute('SELECT * FROM events WHERE session_id = ? ORDER BY seq', (session_id,),)
        out = []
        for row in rows:
            item = dict(row)
            item['payload'] = json.loads(item.pop('payload_json') or '{}')
            out.append(item)
        return out

    # -- budget ledger -----------------------------------------------------
    def reserve(self, session_id: str, action_seq: int, action: str, reserved: float,
                detail: Mapping[str, Any] | None = None) -> int:
        if reserved < 0:
            raise InvariantError('reserved must be >= 0')
        cur = self.con.execute(
            'INSERT INTO budget_ledger(session_id, action_seq, action, reserved, settled, refunded, '
            'detail_json, created_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?)',
            (session_id, action_seq, action, reserved, reserved,
             json.dumps(dict(detail or {}), ensure_ascii=False), utc_now()),
        )
        self.commit()
        return int(cur.lastrowid)

    def settle(self, ledger_id: int, settled: float) -> None:
        row = self.con.execute('SELECT reserved FROM budget_ledger WHERE id = ?', (ledger_id,)).fetchone()
        if row is None:
            raise InvariantError(f'unknown ledger row {ledger_id}')
        reserved = float(row['reserved'])
        if settled < 0 or settled > reserved:
            raise InvariantError('I5: settled must satisfy 0 <= settled <= reserved')
        refunded = reserved - settled
        self.con.execute('UPDATE budget_ledger SET settled = ?, refunded = ? WHERE id = ?',
                         (settled, refunded, ledger_id))
        self.commit()

    def session_cost(self, session_id: str) -> dict[str, Any]:
        row = self.con.execute(
            'SELECT COALESCE(SUM(reserved),0) AS reserved, COALESCE(SUM(settled),0) AS settled, '
            'COALESCE(SUM(refunded),0) AS refunded FROM budget_ledger WHERE session_id = ?',
            (session_id,)).fetchone()
        by_action: dict[str, dict[str, float]] = {}
        for item in self.con.execute(
                'SELECT action, SUM(reserved) AS r, SUM(settled) AS s, SUM(refunded) AS f '
                'FROM budget_ledger WHERE session_id = ? GROUP BY action', (session_id,)):
            by_action[item['action']] = {'reserved': float(item['r']), 'settled': float(item['s']),
                                         'refunded': float(item['f'])}
        return {'reserved': float(row['reserved']), 'settled': float(row['settled']),
                'refunded': float(row['refunded']), 'by_action': by_action}

    # -- inspection helpers used by reuse planning -------------------------
    def candidate_refs(self, session_id: str) -> list[dict[str, str]]:
        rows = self.con.execute(
            'SELECT paper_id, content_hash FROM candidates WHERE session_id = ? ORDER BY paper_id',
            (session_id,))
        return [dict(row) for row in rows]

    def assessments_for_paper(self, paper_id: str) -> list[dict[str, Any]]:
        rows = self.con.execute(
            'SELECT * FROM assessments WHERE paper_id = ? ORDER BY computed_at DESC', (paper_id,))
        return [dict(row) for row in rows]

    def update_budget(self, session_id: str, budget: Mapping[str, Any]) -> None:
        self.con.execute('UPDATE sessions SET budget_json = ?, updated_at = ? WHERE session_id = ?',
                         (json.dumps(dict(budget), ensure_ascii=False, sort_keys=True),
                          utc_now(), session_id))
        self.commit()

    def candidate_papers(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.con.execute(
            'SELECT p.paper_id, p.content_hash, p.title, p.abstract, p.authors_json, p.url, '
            'p.year, p.venue, p.source, p.fetched_at, p.doi, p.arxiv_id, p.open_access_url, '
            'p.abstract_source, p.metadata_only, p.sources_json, p.authority, p.snippet, '
            'p.provenance_conflict, p.publication_json, p.citation_count, p.citation_source, p.citation_observed_at, c.first_query FROM candidates c '
            'JOIN papers p ON p.paper_id = c.paper_id AND p.content_hash = c.content_hash '
            'WHERE c.session_id = ? ORDER BY c.first_seen_at, p.paper_id', (session_id,))
        return [{**dict(row), 'publication': json.loads(row['publication_json'] or '{}')} for row in rows]

    def query_history(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.con.execute(
            'SELECT id, revision, action_seq, query, returned_count, new_count, created_at '
            'FROM queries WHERE session_id = ? ORDER BY id', (session_id,))
        return [dict(row) for row in rows]
