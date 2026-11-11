"""Versioned publication policy for the product research path.

The curated pool is an inspectable product preference, not an objective venue
ranking. Hard publication requirements must hold in one evidenced publication
record; facts from different editions or venues must never be combined.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from common import merge_publication_records, requested_venues, verified_retraction, year_matches


CATALOG_VERSION = 'ai-cs-curated-2026-10-v2'
CATALOG_NOTICE = ('A versioned AI/CS discovery pool selected for this product; '
                  'membership is not an objective ranking or proof of acceptance.')

# Names and exact aliases only. An acronym must never match a different venue
# by substring (ACL vs NAACL, ICML vs ECML, SIGMOD vs SIGMOD Record).
_VENUES = (
    ('NeurIPS', 'conference', ('NIPS', 'Conference on Neural Information Processing Systems',
                              'Advances in Neural Information Processing Systems')),
    ('ICML', 'conference', ('International Conference on Machine Learning',)),
    ('ICLR', 'conference', ('International Conference on Learning Representations',)),
    ('ACL', 'conference', ('Annual Meeting of the Association for Computational Linguistics',
                           'Proceedings of the conference - Association for Computational Linguistics. Meeting')),
    ('EMNLP', 'conference', ('Conference on Empirical Methods in Natural Language Processing',)),
    ('NAACL', 'conference', ('North American Chapter of the Association for Computational Linguistics',
                            'Conference of the North American Chapter of the Association for Computational Linguistics')),
    ('EACL', 'conference', ('Conference of the European Chapter of the Association for Computational Linguistics',)),
    ('CVPR', 'conference', ('IEEE/CVF Conference on Computer Vision and Pattern Recognition',
                           'IEEE Conference on Computer Vision and Pattern Recognition',
                           'IEEE Computer Society Conference on Computer Vision and Pattern Recognition',
                           'IEEE Computer Society Conference on Computer Vision and Pattern Recognition/Proceedings',
                           'IEEE Computer Society Conference on Computer Vision and Pattern Recognition/Proceedings - IEEE Computer Society Conference on Computer Vision and Pattern Recognition/Proceedings',
                           'Conference on Computer Vision and Pattern Recognition')),
    ('ICCV', 'conference', ('IEEE/CVF International Conference on Computer Vision',
                           'International Conference on Computer Vision')),
    ('ECCV', 'conference', ('European Conference on Computer Vision',)),
    ('AAAI', 'conference', ('AAAI Conference on Artificial Intelligence',)),
    ('IJCAI', 'conference', ('International Joint Conference on Artificial Intelligence',)),
    ('KDD', 'conference', ('ACM SIGKDD Conference on Knowledge Discovery and Data Mining',)),
    ('SIGIR', 'conference', ('International ACM SIGIR Conference on Research and Development in Information Retrieval',)),
    ('WWW', 'conference', ('The Web Conference', 'International World Wide Web Conference')),
    ('SIGMOD', 'conference', ('ACM SIGMOD International Conference on Management of Data',)),
    ('VLDB', 'conference', ('International Conference on Very Large Data Bases',)),
    ('OSDI', 'conference', ('USENIX Symposium on Operating Systems Design and Implementation',)),
    ('SOSP', 'conference', ('ACM Symposium on Operating Systems Principles',)),
    ('NSDI', 'conference', ('USENIX Symposium on Networked Systems Design and Implementation',)),
    ('USENIX Security', 'conference', ('USENIX Security Symposium',)),
    ('CCS', 'conference', ('ACM SIGSAC Conference on Computer and Communications Security',)),
    ('ICSE', 'conference', ('International Conference on Software Engineering',)),
    ('PLDI', 'conference', ('ACM SIGPLAN Conference on Programming Language Design and Implementation',)),
    ('POPL', 'conference', ('ACM SIGPLAN Symposium on Principles of Programming Languages',)),
    ('ICRA', 'conference', ('IEEE International Conference on Robotics and Automation',)),
    ('CoRL', 'conference', ('Conference on Robot Learning',)),
    ('RSS', 'conference', ('Robotics Science and Systems',)),
    ('JMLR', 'journal', ('Journal of Machine Learning Research',)),
    ('TMLR', 'journal', ('Transactions on Machine Learning Research',)),
    ('TACL', 'journal', ('Transactions of the Association for Computational Linguistics',)),
    ('TPAMI', 'journal', ('IEEE Transactions on Pattern Analysis and Machine Intelligence',)),
    ('TOIS', 'journal', ('ACM Transactions on Information Systems',)),
    ('TKDE', 'journal', ('IEEE Transactions on Knowledge and Data Engineering',)),
    ('ACM Computing Surveys', 'journal', ('ACM CSUR', 'CSUR')),
    ('Artificial Intelligence Review', 'journal', ('AI Review',)),
)

# Only IDs checked against an official publisher/index record belong here.
_JOURNAL_ISSNS = {
    # https://api.openalex.org/sources/S157921468 (checked 2026-10-20).
    'ACM Computing Surveys': ('03600300', '15577341'),
    # Springer https://link.springer.com/journal/10462 (checked 2026-10-20):
    # ceased print ISSN 0269-2821; current electronic ISSN 1573-7462.
    'Artificial Intelligence Review': ('02692821', '15737462'),
}


def _mapping(value: Any) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    return dict(vars(value)) if hasattr(value, '__dict__') else {}


def _name(value: Any) -> str:
    text = str(value or '').casefold()
    text = re.sub(r'\((?:volume\b|long papers\b|short papers\b|main conference\b)[^)]*\)', ' ', text)
    text = re.sub(r'\([a-z]{2,12}\)\s*$', '', text)
    text = re.sub(r'\b(?:19|20)\d{2}\b', ' ', text)
    text = re.sub(r'\b\d+(?:st|nd|rd|th)\b', ' ', text)
    text = re.sub(r'\b(?:oral|spotlight|poster)\b\s*$', '', text)
    text = re.sub(r'^\s*proceedings\s+of\s+(?:the\s+)?', '', text)
    return ' '.join(re.findall(r'[a-z0-9]+', text))


def normalize_issn(value: Any) -> str:
    text = re.sub(r'[\s-]', '', str(value or '')).upper()
    return text if re.fullmatch(r'\d{7}[\dX]', text) else ''


def venue_identity(value: Any) -> str:
    key = _name(value)
    # Exact aliases for the video benchmark; they are not curated defaults.
    if key in {'acm mm', 'acm multimedia', 'acm international conference on multimedia'}:
        return 'ACM MM'
    if key in {'siggraph', 'acm siggraph', 'acm siggraph conference papers'}:
        return 'SIGGRAPH'
    for canonical, _, aliases in _VENUES:
        if key in {_name(canonical), *(_name(alias) for alias in aliases)}:
            return canonical
    return key


def curated_venue_catalog() -> dict:
    return {'version': CATALOG_VERSION, 'notice': CATALOG_NOTICE,
            'venues': [{'name': name, 'type': kind, 'aliases': list(aliases),
                        'issn': list(_JOURNAL_ISSNS.get(name, ()))}
                       for name, kind, aliases in _VENUES]}


def journal_issns_for_venue(value: Any) -> list[str]:
    literal = normalize_issn(value)
    return [literal] if literal else list(_JOURNAL_ISSNS.get(venue_identity(value), ()))


def _evidenced(record: Mapping[str, Any]) -> bool:
    if record.get('verified') is not True or record.get('identity_verified') is False:
        return False
    for evidence in record.get('evidence') or []:
        if not isinstance(evidence, Mapping):
            continue
        try:
            parsed = urlsplit(str(evidence.get('url') or ''))
        except ValueError:
            continue
        hosts = {
            'openreview': {'openreview.net', 'api2.openreview.net', 'api.openreview.net'},
            'openalex': {'api.openalex.org', 'openalex.org'},
            'semantic_scholar': {'api.semanticscholar.org', 'www.semanticscholar.org', 'semanticscholar.org'},
            'acl_anthology': {'aclanthology.org'},
            'arxiv': {'arxiv.org', 'export.arxiv.org'},
            'lune': {'mcp.luneresearch.com', 'api.luneresearch.com'},
        }
        if (parsed.scheme == 'https' and parsed.hostname in hosts.get(evidence.get('source'), set())
                and not parsed.username and not parsed.password):
            return True
    return False


def publication_records(paper: Any, *, verified_only: bool = True) -> list[dict]:
    publication = _mapping(_mapping(paper).get('publication'))
    if verified_only and publication.get('identity_verified') is False:
        return []
    raw = publication.get('records')
    # A summary of several publications is never itself a publication event.
    records = raw if isinstance(raw, list) else [publication]
    out = [dict(record) for record in records if isinstance(record, Mapping)]
    if verified_only:
        # Imported histories and older state may not have passed through the
        # latest adapter/dedup path. Reconcile before any record can authorize
        # a hard condition; merge works on copies, preserving original audit data.
        out = merge_publication_records(out, publication.get('conflicts') or [])['records']
    return [record for record in out if _evidenced(record) and record.get('status') != 'retracted'] if verified_only else out


def _record_year(record: Mapping[str, Any]) -> int | None:
    match = re.match(r'((?:19|20)\d{2})(?:$|[-/T ])', str(record.get('date') or ''))
    return int(match.group(1)) if match else None


def _record_venue_matches(record: Mapping[str, Any], requested: str) -> bool:
    names = requested_venues(requested)
    if len(names) > 1:
        return any(_record_venue_matches(record, name) for name in names)
    if not str(requested or '').strip():
        return True
    issns = journal_issns_for_venue(requested)
    if issns:
        values = record.get('issn') or []
        values = [values] if isinstance(values, str) else values
        if set(issns) & {normalize_issn(value) for value in values}:
            return True
        if normalize_issn(requested):
            return False
    asked = venue_identity(requested)
    # Official edition IDs are exact identities even when a display heading is
    # absent. Never infer a workshop or Findings volume from the parent name.
    venue_id = str(record.get('venue_id') or '')
    match = re.fullmatch(r'(ICLR|NeurIPS|ICML)\.cc/(?:19|20)\d{2}/Conference', venue_id)
    if match and match.group(1) == asked:
        return True
    return bool(asked) and venue_identity(record.get('venue_name')) == asked


def matches_requested_venue(paper: Any, requested: str) -> bool:
    return any(_record_venue_matches(record, requested) for record in publication_records(paper))


def _curated(record: Mapping[str, Any]) -> bool:
    return any(record.get('type') == kind and _record_venue_matches(record, name)
               for name, kind, _ in _VENUES)


def _record_violations(record: Mapping[str, Any], task: Mapping[str, Any]) -> list[str]:
    reasons = []
    if not _record_venue_matches(record, str(task.get('venue') or '')):
        reasons.append('venue_unmet')
    kind = task.get('publication_type') or 'any'
    if kind != 'any' and record.get('type') != kind:
        reasons.append('publication_type_unmet')
    presentation = task.get('presentation_types') or []
    if (task.get('accepted_only') or presentation) and record.get('status') not in ('accepted', 'published'):
        reasons.append('acceptance_unverified')
    if presentation:
        openreview_evidence = _evidenced({**record, 'evidence': [
            item for item in record.get('evidence') or []
            if isinstance(item, Mapping) and item.get('source') == 'openreview']})
        if (record.get('presentation') not in presentation or record.get('status') != 'accepted'
                or record.get('type') != 'conference' or not openreview_evidence):
            reasons.append('presentation_unverified')
    if task.get('venue_scope') == 'preferred_only' and not _curated(record):
        reasons.append('curated_pool_unmet')
    if not year_matches(_record_year(record), task.get('year_from'), task.get('year_to')):
        reasons.append('publication_year_unmet')
    return reasons


def _has_publication_constraints(task: Mapping[str, Any]) -> bool:
    return bool(task.get('venue') or task.get('publication_type', 'any') != 'any'
                or task.get('accepted_only') or task.get('presentation_types')
                or task.get('venue_scope') == 'preferred_only')


def _ranking_records(paper: Any, task: Mapping[str, Any]) -> list[dict]:
    records = publication_records(paper)
    return [record for record in records if not _record_violations(record, task)] \
        if (_has_publication_constraints(task) or task.get('year_from') or task.get('year_to')) else records


def _record_preference(record: Mapping[str, Any], task: Mapping[str, Any]) -> int:
    accepted = record.get('status') in ('accepted', 'published')
    if accepted and task.get('venue_scope') in ('preferred', 'preferred_only') and _curated(record):
        return 3
    return 2 if accepted else 1


def matched_publication(paper: Any, request: Any) -> dict | None:
    """Return an independent display record that actually supports this request.

    This never promotes the aggregate summary to a new publication event and
    never changes the work's original version history or nested evidence.
    """
    task = _mapping(request)
    records = _ranking_records(paper, task)
    if not records:
        return None
    selected = max(records, key=lambda record: (
        _record_preference(record, task), _record_year(record) or 0,
        str(record.get('date') or ''), str(record.get('venue_id') or ''),
        str(record.get('venue_name') or '')))
    return deepcopy(selected)


def publication_violations(paper: Any, request: Any) -> list[str]:
    task = _mapping(request)
    if not publication_records(paper) and any(verified_retraction(record)
            for record in publication_records(paper, verified_only=False)):
        return ['publication_retracted']
    year_from, year_to = task.get('year_from'), task.get('year_to')
    hard = _has_publication_constraints(task)
    if not hard:
        return [] if year_matches(_mapping(paper).get('year'), year_from, year_to) else ['year_unmet']
    records = publication_records(paper)
    if not records:
        return ['publication_unverified']
    failures = []
    for record in records:
        reasons = _record_violations(record, task)
        if not reasons:
            return []
        failures.extend(reasons)
    return sorted(set(failures)) or ['publication_constraints_unmet']


def publication_matches(paper: Any, request: Any) -> bool:
    return not publication_violations(paper, request)


def publication_preference(paper: Any, request: Any) -> int:
    task = _mapping(request)
    records = _ranking_records(paper, task)
    return max((_record_preference(record, task) for record in records), default=0)


def research_selection_key(paper: Any, assessment: Mapping[str, Any], request: Any,
                           preferred_type: str = '') -> tuple:
    task, item = _mapping(request), _mapping(paper)
    fit = assessment.get('fit_score', 50)
    fit = fit if type(fit) in (int, float) and 0 <= fit <= 100 else 50
    relevance = assessment.get('relevance', 0)
    relevance = relevance if type(relevance) is int else 0
    type_match = bool(preferred_type and assessment.get('paper_type') == preferred_type)
    recent = bool(task.get('prefer_recent') or task.get('reading_goal') == 'frontier')
    years = [_record_year(record) for record in _ranking_records(paper, task)]
    year = max((value for value in years if value is not None), default=item.get('year') or 0)
    if task.get('venue_scope') == 'preferred':
        # A model's 95 vs 99 is not a calibrated four-point scientific quality
        # difference. For an explicit venue preference, compare coarse fit bands
        # before trustworthy publication identity, then recency and precise fit.
        return (-relevance, -(fit // 10), -int(type_match),
                -publication_preference(paper, task), -year if recent else 0,
                -fit, str(item.get('id') or ''))
    return (-relevance, -fit, -int(type_match), -publication_preference(paper, task),
            -year if recent else 0, str(item.get('id') or ''))
