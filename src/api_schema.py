"""Machine-readable public contract, intentionally independent of the Web UI."""
from copy import deepcopy

from common import REMOTE_OUTCOMES


def openapi_document():
    error = {'type': 'object', 'required': ['error'], 'properties': {
        'api_version': {'type': 'string', 'const': 'v1'},
        'error': {'type': 'object', 'required': ['code', 'message', 'retryable'],
                  'properties': {'code': {'type': 'string'}, 'message': {'type': 'string'},
                                 'field': {'type': ['string', 'null']}, 'retryable': {'type': 'boolean'},
                                 'remote_outcome': {'enum': list(REMOTE_OUTCOMES)},
                                 'transaction_outcome': {'enum': ['committed', 'unknown', 'rolled_back_or_unknown', 'not_started']},
                                 'retry_safe': {'const': False}}}}}
    filters = {'type': 'object', 'additionalProperties': False, 'properties': {
        'target_count': {'type': 'integer', 'minimum': 1, 'maximum': 10},
        'minimum_count': {'type': ['integer', 'null'], 'minimum': 1, 'maximum': 10},
        'maximum_count': {'type': ['integer', 'null'], 'minimum': 1, 'maximum': 10},
        'category': {'type': 'string', 'enum': ['none', 'evaluation', 'survey', 'research']},
        'minimum_category_count': {'type': 'integer', 'minimum': 0, 'maximum': 10},
        'venue': {'type': 'string'}, 'year_from': {'type': ['integer', 'null']},
        'year_to': {'type': ['integer', 'null']}, 'purpose': {'type': 'string'},
        'reading_goal': {'type': 'string', 'enum': ['', 'auto', 'general', 'orientation', 'comparison', 'frontier']},
        'ranking': {'type': 'string', 'enum': ['relevance', 'citations']},
        'publication_type': {'type': 'string', 'enum': ['any', 'conference', 'journal', 'preprint']},
        'venue_scope': {'type': 'string', 'enum': ['any', 'preferred', 'preferred_only']},
        'accepted_only': {'type': 'boolean'}, 'prefer_recent': {'type': 'boolean'},
        'presentation_types': {'type': 'array', 'items': {'type': 'string', 'enum': ['oral', 'spotlight', 'poster']}},
        'excluded_paper_ids': {'type': 'array', 'items': {'type': 'string'}}}}
    request = {'type': 'object', 'additionalProperties': False, 'properties': {
        'query': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
        'message': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
        'request': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
        'count': {'type': 'integer', 'minimum': 1, 'maximum': 10, 'default': 5},
        'source': {'type': 'string', 'description': 'Source name, authorized comma list, or multi. Sources capabilities are available from /api/v1/sources.'},
        'providers': {'type': 'array', 'minItems': 1, 'items': {'type': 'string'}},
        'filters': {'$ref': '#/components/schemas/Filters'},
        'request_data': {'type': 'object', 'description': 'Validated structured task for creation; includes topic and Filters fields.'},
        'mode': {'type': 'string', 'enum': ['live', 'demo'], 'default': 'live'},
        'stream': {'type': 'boolean', 'default': True, 'description': 'Execute asynchronously. Event transport is selected on GET events.'},
        'cache': {'type': 'boolean', 'default': True},
        'research_mode': {'type': 'boolean', 'default': True},
        'reading_goal': deepcopy(filters['properties']['reading_goal']),
        'scenario': {'type': 'string', 'enum': ['followup', 'satisfied', 'insufficient', 'ask']},
        'client_request_id': {'type': 'string', 'minLength': 1, 'maxLength': 160},
        'session_id': {'type': 'string', 'pattern': '^[a-f0-9]{32}$', 'description': 'Chat only: continue this workspace.'}},
        'anyOf': [{'required': [field]} for field in ('query', 'message', 'request', 'request_data')]}
    paper = {'type': 'object', 'required': ['rank', 'paper_id', 'title'], 'properties': {
        'rank': {'type': 'integer', 'minimum': 1}, 'paper_id': {'type': 'string'},
        'id': {'type': 'string'}, 'title': {'type': 'string'}, 'url': {'type': 'string'},
        'doi': {'type': 'string'}, 'abstract': {'type': 'string'}, 'reason': {'type': 'string'},
        'citation_count': {'type': ['integer', 'null'], 'minimum': 0},
        'citation_source': {'type': 'string'}, 'citation_observed_at': {'type': 'string'},
        'sources': {'type': 'array', 'items': {'type': 'string'}},
        'publication': {'type': 'object'}, 'evidence': {'type': 'array', 'items': {'type': 'object'}}}}
    result = {'type': 'object', 'properties': {
        'status': {'type': 'string'}, 'stop_reason': {'type': 'string'}, 'request': {'type': 'object'},
        'returned_count': {'type': 'integer', 'minimum': 0, 'maximum': 10},
        'papers': {'type': 'array', 'maxItems': 10, 'items': {'$ref': '#/components/schemas/Paper'}},
        'candidates': {'type': 'array', 'maxItems': 20, 'description': 'Discovery candidates; not necessarily assessed or recommended.',
                       'items': {'type': 'object', 'required': ['paper_id', 'title'],
                                 'properties': {'paper_id': {'type': 'string'}, 'title': {'type': 'string'},
                                                'url': {'type': 'string'}, 'source': {'type': 'string'},
                                                'sources': {'type': 'array', 'items': {'type': 'string'}}}}},
        'gaps': {'type': 'array'}, 'warnings': {'type': 'array'}, 'question': {'type': 'string'},
        'usage': {'type': 'object'}, 'cost': {'type': ['object', 'null']},
        'research_brief': {'type': ['object', 'null']}, 'diagnosis': {'type': 'object'}}}
    operation = {'type': 'object', 'required': ['api_version', 'operation_id', 'state', 'links'], 'properties': {
        'api_version': {'type': 'string', 'const': 'v1'}, 'operation_id': {'type': 'string'},
        'session_id': {'type': 'string'}, 'turn': {'type': 'integer'}, 'kind': {'type': 'string'},
        'state': {'type': 'string', 'enum': ['running', 'queued', 'completed', 'failed', 'interrupted', 'admission_unknown']},
        'links': {'type': 'object'}, 'result': {'anyOf': [{'$ref': '#/components/schemas/Result'}, {'type': 'null'}]},
        'error': {'type': ['object', 'null']}}}
    event = {'type': 'object', 'required': ['seq', 'event_id', 'action', 'payload'], 'properties': {
        'seq': {'type': 'integer'}, 'event_id': {'type': 'string'}, 'operation_id': {'type': 'string'},
        'session_id': {'type': 'string'}, 'turn': {'type': ['integer', 'null']},
        'at': {'type': 'string'}, 'action': {'type': 'string'}, 'payload': {'type': 'object'}}}
    response = {'description': 'Operation snapshot', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Operation'}}}}
    identity = {'name': 'id', 'in': 'path', 'required': True, 'schema': {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}}
    paths = {}
    for name in ('search', 'chat'):
        paths[f'/api/v1/{name}'] = {'post': {
            'summary': 'Create a research workspace' if name == 'search' else 'Create or continue a Chat workspace',
            'operationId': name,
            'parameters': [{'name': 'Idempotency-Key', 'in': 'header', 'schema': {'type': 'string', 'maxLength': 160}}],
            'requestBody': {'required': True, 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/ResearchRequest'}}}},
            'responses': {'202': deepcopy(response), '400': {'$ref': '#/components/responses/InvalidRequest'},
                          '409': {'description': 'Busy session, conflicting idempotency key or unsupported transition'}}}}
    memory_key = {'name': 'key', 'in': 'path', 'required': True,
                  'schema': {'type': 'string', 'enum': ['reading_goal', 'prefer_recent']}}
    revision = {'type': 'integer', 'minimum': 0, 'maximum': 9223372036854775806}
    memory_record = {'type': 'object', 'additionalProperties': False,
                     'required': ['revision', 'state', 'value', 'consent', 'expires_at'],
                     'properties': {'revision': {'type': 'integer', 'minimum': 1},
                                    'state': {'enum': ['granted', 'revoked', 'deleted', 'expired']},
                                    'value': {'type': ['string', 'boolean', 'null']},
                                    'consent': {'type': 'boolean'}, 'expires_at': {'type': ['number', 'null']}}}
    preference_values = {'type': 'object', 'additionalProperties': False, 'properties': {
        'reading_goal': {'enum': ['orientation', 'comparison', 'frontier', 'general']},
        'prefer_recent': {'type': 'boolean'}}}
    store_id = {'type': 'string', 'pattern': '^[0-9a-f]{64}$',
                'description': 'Opaque store identity from the latest snapshot; not an authentication token.'}
    memory_snapshot = {'type': 'object', 'additionalProperties': False,
                       'required': ['api_version', 'preferences', 'active', 'store_id'], 'properties': {
                           'api_version': {'const': 'v1'}, 'store_id': deepcopy(store_id),
                           'preferences': {'type': 'object', 'additionalProperties': False,
                                           'properties': {key: deepcopy(memory_record) for key in ('reading_goal', 'prefer_recent')}},
                           'active': preference_values}}
    receipt = {'type': 'object', 'additionalProperties': False, 'required': ['api_version', 'revision'],
               'properties': {'api_version': {'const': 'v1'}, 'revision': {'type': 'integer', 'minimum': 1}}}
    def json_response(description, schema):
        return {'description': description, 'content': {'application/json': {'schema': schema}}}
    memory_errors = {'400': {'$ref': '#/components/responses/InvalidRequest'},
                     '409': json_response('Revision conflict; read again', {'$ref': '#/components/schemas/Error'}),
                     '500': json_response('Local failure or unknown commit; read again, never auto-retry', {'$ref': '#/components/schemas/Error'})}
    paths['/api/v1/memory/preferences'] = {'get': {
        'summary': 'Read local preferences; initialize an empty store if absent and persist observed expiration',
        'responses': {'200': json_response('Preference snapshot', memory_snapshot), '500': memory_errors['500']}}}
    grant = {'type': 'object', 'additionalProperties': False,
             'required': ['value', 'consent', 'expires_at', 'expected_revision', 'store_id'],
             'properties': {'store_id': deepcopy(store_id),
                            'value': {'oneOf': [preference_values['properties']['reading_goal'], {'type': 'boolean'}]},
                            'consent': {'const': True}, 'expires_at': {'type': 'number', 'description': 'Finite future Unix timestamp'},
                            'expected_revision': revision}}
    for suffix, body, summary in (
        ('', grant, 'Explicitly consent to a preference; value must match key'),
        ('/revoke', {'type': 'object', 'additionalProperties': False, 'required': ['expected_revision', 'store_id'],
                     'properties': {'expected_revision': revision, 'store_id': deepcopy(store_id)}}, 'Revoke a local preference'),
        ('/delete', {'type': 'object', 'additionalProperties': False, 'required': ['expected_revision', 'store_id'],
                     'properties': {'expected_revision': revision, 'store_id': deepcopy(store_id)}}, 'Clear the local value and leave a tombstone')):
        paths['/api/v1/memory/preferences/{key}' + suffix] = {'post': {
            'summary': summary, 'parameters': [memory_key],
            'requestBody': {'required': True, 'content': {'application/json': {'schema': body}}},
            'responses': {'200': json_response('Committed revision; read snapshot separately', receipt), **deepcopy(memory_errors)}}}
    paths['/api/v1/operations/{id}'] = {'get': {'summary': 'Read persisted state and result', 'parameters': [identity], 'responses': {'200': deepcopy(response), '404': {'description': 'Not found'}}}}
    paths['/api/v1/operations/{id}/result'] = {'get': {'summary': 'Read completed result (409 while unavailable)', 'parameters': [identity], 'responses': {'200': deepcopy(response), '409': {'description': 'No committed result yet'}}}}
    paths['/api/v1/operations/{id}/events'] = {'get': {'summary': 'Read real execution events or subscribe with Accept: text/event-stream',
        'parameters': [identity, {'name': 'after', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 0}},
                       {'name': 'Last-Event-ID', 'in': 'header', 'schema': {'type': 'integer'}}],
        'responses': {'200': {'description': 'SSE trace/done events; supports reconnect. Or JSON events/after snapshot.',
                              'content': {'text/event-stream': {'schema': {'type': 'string'}}, 'application/json': {'schema': {'type': 'object'}}}}}}}
    paths['/api/v1/operations/{id}/cancel'] = {'post': {'summary': 'Request cooperative cancellation, retaining verified progress', 'parameters': [identity], 'responses': {'200': {'description': 'Cancellation receipt'}}}}
    paths['/api/v1/sources'] = {'get': {'summary': 'Read source capabilities/configuration/observed health; no implicit probe', 'responses': {'200': {'description': 'Sources snapshot'}}},
                              'post': {'summary': 'Save server-side source configuration', 'responses': {'200': {'description': 'Redacted configuration receipt'}}}}
    paths['/api/v1/sources/lune/quota'] = {
        'get': {'summary': 'Read the local Lune quota snapshot without network access',
                'responses': {'200': {'description': 'Local quota status; external account usage remains unknown'}}},
        'post': {'summary': 'Record an explicitly acknowledged account quota snapshot',
                 'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                     'type': 'object', 'additionalProperties': False,
                     'required': ['remaining', 'expires_at', 'acknowledged'],
                     'properties': {'remaining': {'type': 'integer', 'minimum': 0, 'maximum': 300},
                                    'expires_at': {'type': 'number', 'description': 'Snapshot expiration, Unix seconds'},
                                    'acknowledged': {'type': 'boolean', 'const': True}}}}}},
                 'responses': {'200': {'description': 'Local quota snapshot'}, '400': {'description': 'Invalid or stale confirmation'}}}}
    paths['/api/v1/sources/lune/quota/reconcile'] = {
        'post': {'summary': 'Mark an acknowledged stopped reservation as unknown without refund',
                 'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                     'type': 'object', 'additionalProperties': False,
                     'required': ['id', 'acknowledged'],
                     'properties': {'id': {'type': 'string'}, 'acknowledged': {'type': 'boolean', 'const': True}}}}}},
                 'responses': {'200': {'description': 'Local quota snapshot'}, '400': {'description': 'Invalid reconciliation'}}}}
    paths['/api/v1/sources/{name}/inflight/legacy'] = {
        'get': {'summary': 'Count ownerless legacy source leases without network access',
                'parameters': [{'name': 'name', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'responses': {'200': {'description': 'Legacy lease count; remote outcomes remain unknown'}}},
        'post': {'summary': 'Explicitly reconcile ownerless legacy leases after all legacy clients stop',
                 'parameters': [{'name': 'name', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                 'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                     'type': 'object', 'additionalProperties': False,
                     'required': ['acknowledged', 'legacy_clients_stopped'],
                     'properties': {'acknowledged': {'type': 'boolean', 'const': True},
                                    'legacy_clients_stopped': {'type': 'boolean', 'const': True}}}}}},
                 'responses': {'200': {'description': 'Reconciliation result; remote outcomes remain unknown'},
                               '400': {'description': 'Explicit confirmations required'}}}}
    for resource, method, summary in (('test', 'post', 'Perform a bounded real source probe'), ('catalog', 'get', 'Read actual indexed conference coverage'), ('skills', 'get', 'List available grounded source operations')):
        paths[f'/api/v1/sources/{{name}}/{resource}'] = {method: {'summary': summary, 'parameters': [{'name': 'name', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}], 'responses': {'200': {'description': 'Actual source result or explicit structured failure'}}}}
    return {'openapi': '3.1.0', 'info': {'title': 'ScholarGuide Product API', 'version': '1.0.0',
             'description': 'Local API sharing the Web application engine. No dataset, human annotation or research scoring dependency. Quote verification establishes provenance, not semantic proof.'},
            'servers': [{'url': '/'}], 'paths': paths,
            'components': {'schemas': {'ResearchRequest': request, 'Filters': filters, 'Paper': paper, 'Result': result, 'Operation': operation, 'Event': event, 'Error': error},
                           'responses': {'InvalidRequest': {'description': 'Validated error before dispatch', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}}}}}
