"""Locally reviewed input policies for the existing Lune product routes.

These are host-side bounds, not a claim that the remote tool catalogue has been
freshly discovered. Server output and descriptions never grant permissions.
"""

import hmac
from urllib.parse import urlsplit

from common import GuideError


def lune_override_headers(headers: dict, target: str, configured_origin: str) -> dict:
    """Do not carry arbitrary inherited headers to a different origin."""
    try:
        actual, canonical = urlsplit(target), urlsplit(configured_origin)
        def origin(url):
            return (url.scheme.lower(), (url.hostname or '').lower(),
                    url.port or (443 if url.scheme.lower() == 'https' else 80))
        if not actual.hostname or not canonical.hostname or origin(actual) != origin(canonical):
            return {}
        return dict(headers)
    except (TypeError, ValueError):
        return {}


def saved_key_matches_snapshot(key: str, endpoint: str, row: dict) -> bool:
    """Authorize only the exact saved key and endpoint actually being sent."""
    saved = row.get('api_key')
    stored_endpoint = row.get('endpoint') or ''
    if not isinstance(key, str) or not isinstance(saved, str) or not key or not saved:
        return False
    if not isinstance(endpoint, str) or endpoint.rstrip('/') != stored_endpoint.rstrip('/'):
        return False
    return hmac.compare_digest(key.encode('utf-8'), saved.encode('utf-8'))


def validate_lune_auth_target(target: str, configured_origin: str, *, require_same_origin: bool) -> None:
    """Do not send credentials over HTTP or implicit credentials to a new origin."""
    try:
        actual, canonical = urlsplit(target), urlsplit(configured_origin)
        if actual.scheme.lower() != 'https' or not actual.hostname:
            raise ValueError('insecure endpoint')
        origin = (actual.scheme.lower(), actual.hostname.lower(), actual.port or 443)
        reference = (canonical.scheme.lower(), (canonical.hostname or '').lower(),
                     canonical.port or (443 if canonical.scheme.lower() == 'https' else 80))
        if require_same_origin and origin != reference:
            raise ValueError('OAuth origin changed')
    except (TypeError, ValueError):
        error = GuideError('Lune credential target is not authorized; implicit credentials require the configured HTTPS origin.')
        error.code = 'unauthorized_credential_target'
        raise error from None


_TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 2000}
_LIMIT = {'type': 'integer', 'minimum': 1, 'maximum': 10}
_OFFSET = {'type': 'integer', 'minimum': 0, 'maximum': 10000}
_YEAR = {'type': 'integer', 'minimum': 1900, 'maximum': 2100}
_VENUES = {'type': 'array', 'minItems': 1, 'maxItems': 10,
           'items': {'type': 'string', 'minLength': 1, 'maxLength': 200}}


def _object(properties, required=()):
    return {'type': 'object', 'properties': properties,
            'required': list(required), 'additionalProperties': False}


LUNE_TOOL_SCHEMAS = {
    'search_papers': _object({
        'query': _TEXT, 'limit': _LIMIT, 'offset': _OFFSET, 'venues': _VENUES,
        'year_min': _YEAR, 'year_max': _YEAR,
        'year': _YEAR, 'conference': _TEXT,
        'sort_by': _TEXT, 'detail': {'type': ['string', 'boolean']},
    }, ('query',)),
    'search_papers_many': _object({
        'queries': {'type': 'array', 'minItems': 1, 'maxItems': 10, 'items': _TEXT},
        'limit': _LIMIT, 'detail': {'type': ['string', 'boolean']},
        'venues': _VENUES, 'year_min': _YEAR, 'year_max': _YEAR,
        'year': _YEAR, 'conference': _TEXT,
    }, ('queries', 'limit', 'detail')),
    'search_related_papers': _object({'paper_id': _TEXT, 'limit': _LIMIT}, ('paper_id',)),
    'get_paper_fulltext': _object({
        'paper_id': _TEXT, 'format': _TEXT,
        'sections': {'type': 'array', 'maxItems': 20, 'items': _TEXT},
    }, ('paper_id',)),
    'get_paper_citations': _object({
        'paper_id': _TEXT, 'limit': _LIMIT, 'offset': _OFFSET,
    }, ('paper_id',)),
    'list_conferences': _object({}),
}

LUNE_PROMPT_SCHEMAS = {
    'literature_review': _object({'topic': _TEXT, 'venues': {'type': 'string', 'maxLength': 2000},
                                  'since_year': {'type': 'string', 'maxLength': 2000}}, ('topic',)),
    'find_related_work': _object({'abstract': _TEXT, 'venues': {'type': 'string', 'maxLength': 2000}}, ('abstract',)),
    'compare_papers': _object({'topic': _TEXT, 'columns': {'type': 'string', 'maxLength': 2000}}, ('topic',)),
    'verify_draft': _object({'draft': _TEXT}, ('draft',)),
    'trace_citations': _object({'paper': _TEXT}, ('paper',)),
    'research_methodology': _object({'question': _TEXT}, ('question',)),
}
