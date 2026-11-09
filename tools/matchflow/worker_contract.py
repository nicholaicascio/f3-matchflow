"""Operation contracts shared by schema discovery and host admission."""
OPERATIONS = {
    'status': [], 'schema': [], 'help_ack': ['request_ids'],
    'symbols': ['text'], 'diff': ['check_id'], 'target': [],
    'read': [], 'list': [], 'search': ['text'], 'write': ['path', 'text'],
    'reference': ['name'], 'twin': [],
    'check': ['source', 'stage'],
    'permute': ['source'], 'checks': [],
    'gate': ['decision', 'reason'], 'register_wall': ['evidence'],
    'escalate': ['reason', 'need'], 'promote': [],
    'checkpoint': ['phase', 'note'], 'close': ['operation'], 'report': [],
}


def describe(schema):
    properties = schema['properties']['data']['properties']
    return dict(input=schema, operations={op: dict(type='object', required=keys,
                properties=properties, additionalProperties=False) for op, keys in OPERATIONS.items()},
                conditional_requirements={
                    'read': 'path required except area=packet',
                    'check': 'stage baseline|attempt|final; why required; standard/full also change/expect; observed after first check; full also regions',
                    'permute': 'stage attempt|final (default attempt); same notes as check for verification',
                    'reference': 'fnv requires public relative path',
                    'close': 'giveup needs structured notes and exactly one source/check_id/no_source_reason; submit uses promoted artifact; flag-proof needs notes.note',
                    'symbols': 'text 2..160 characters; limit 1..100',
                    'diff': 'offset is one-based, limit 1..2000; own recorded check only',
                })


def validate(op, data):
    if op == 'completion':  # Internal host protocol, not advertised as a worker operation.
        return
    if op not in OPERATIONS:
        raise ValueError('operation not available to workers; call schema for allowed operations')
    missing = [k for k in OPERATIONS[op] if k not in data]
    if missing:
        raise ValueError('%s requires %s; call schema for operation contracts' % (op, ', '.join(missing)))
    if op == 'read' and data.get('area') != 'packet' and 'path' not in data:
        raise ValueError('read requires path except for area=packet')
    if op == 'check' and data.get('stage') not in ('baseline', 'attempt', 'final'):
        raise ValueError('check stage must be baseline, attempt or final')
    if op == 'permute' and data.get('stage') not in (None, 'attempt', 'final'):
        raise ValueError('permute stage must be attempt or final (default attempt)')
