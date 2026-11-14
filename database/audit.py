"""Read-only duplicate/index audit. Never invokes DatabaseManager.connect()."""

from datetime import UTC, datetime

from pymongo.collation import Collation

from database.indexes import INDEX_SPECS, is_legacy_level_index


def duplicate_pipeline(spec, samples: int) -> list[dict]:
    # Missing and null values conflict under a full unique index; group them
    # together rather than reporting them as separate clean identities.
    return [
        {
            '$group': {
                '_id': {f'key_{index}': {'$ifNull': [f'${field}', None]} for index, (field, _) in enumerate(spec.keys)},
                'count': {'$sum': 1},
            }
        },
        {'$match': {'count': {'$gt': 1}}},
        {
            '$facet': {
                'totals': [
                    {
                        '$group': {
                            '_id': None,
                            'groups': {'$sum': 1},
                            'extra_documents': {'$sum': {'$subtract': ['$count', 1]}},
                        }
                    }
                ],
                'samples': [{'$sort': {'count': -1, '_id': 1}}, {'$limit': samples}],
            }
        },
    ]


def audit_database(db, *, samples: int = 10, max_time_ms: int = 30_000) -> dict:
    if not 1 <= samples <= 100:
        raise ValueError('samples must be between 1 and 100')
    report = {
        'generated_at': datetime.now(UTC).isoformat(),
        'database': db.name,
        'read_only': True,
        'status': 'clean',
        'indexes': [],
        'duplicates': [],
        'legacy_indexes': [],
        'errors': [],
    }
    metadata = {}
    for collection_name in sorted({spec.collection for spec in INDEX_SPECS}):
        try:
            metadata[collection_name] = db[collection_name].index_information()
        except Exception as error:
            report['errors'].append({'collection': collection_name, 'operation': 'index_information', 'error': type(error).__name__})
    for spec in INDEX_SPECS:
        indexes = metadata.get(spec.collection)
        if indexes is not None:
            report['indexes'].append(
                {
                    'collection': spec.collection,
                    'keys': list(spec.keys),
                    'unique': spec.unique,
                    'expire_after_seconds': spec.expire_after_seconds,
                    'required': spec.required,
                    'matching_names': [name for name, value in indexes.items() if spec.matches(value)],
                    'same_key_indexes': [
                        {
                            'name': name,
                            'unique': bool(value.get('unique', False)),
                            'sparse': bool(value.get('sparse', False)),
                            'partial': 'partialFilterExpression' in value,
                            'collation': value.get('collation', {}).get('locale', 'simple'),
                            'expire_after_seconds': value.get('expireAfterSeconds'),
                        }
                        for name, value in indexes.items()
                        if tuple(tuple(key) for key in value.get('key', [])) == spec.keys
                    ],
                }
            )
        if not spec.unique:
            continue
        try:
            results = list(
                db[spec.collection].aggregate(
                    duplicate_pipeline(spec, samples),
                    allowDiskUse=True,
                    maxTimeMS=max_time_ms,
                    collation=Collation(locale='simple'),
                )
            )
            result = results[0] if results else {'totals': [], 'samples': []}
            totals = result['totals'][0] if result['totals'] else {'groups': 0, 'extra_documents': 0}
            report['duplicates'].append(
                {
                    'collection': spec.collection,
                    'keys': [key for key, _ in spec.keys],
                    'groups': totals['groups'],
                    'extra_documents': totals['extra_documents'],
                    'samples': [
                        {
                            'values': {field: group['_id'][f'key_{index}'] for index, (field, _) in enumerate(spec.keys)},
                            'count': group['count'],
                        }
                        for group in result['samples']
                    ],
                }
            )
        except Exception as error:
            report['errors'].append({'collection': spec.collection, 'operation': 'duplicates', 'error': type(error).__name__})
    for name, value in metadata.get('user_levels', {}).items():
        if is_legacy_level_index(value):
            report['legacy_indexes'].append({'collection': 'user_levels', 'name': name})
    report['summary'] = {
        'missing_required_indexes': sum(item['required'] and not item['matching_names'] for item in report['indexes']),
        'missing_optional_indexes': sum(not item['required'] and not item['matching_names'] for item in report['indexes']),
        'duplicate_groups': sum(item['groups'] for item in report['duplicates']),
        'extra_documents': sum(item['extra_documents'] for item in report['duplicates']),
        'legacy_indexes': len(report['legacy_indexes']),
        'errors': len(report['errors']),
    }
    if report['errors']:
        report['status'] = 'incomplete'
    elif any(report['summary'].values()):
        report['status'] = 'needs_attention'
    return report
