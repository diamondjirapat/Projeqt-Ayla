"""Read-only identifier inventory; binary representability cannot establish ownership."""

import math
import re
from collections import Counter
from datetime import UTC, datetime

ID_FIELDS = {
    'user_id',
    'guild_id',
    'channel_id',
    'role_id',
    'author_id',
    'creator_id',
    'content_author_id',
    'target_user_id',
    'moderator_id',
    'message_id',
    'discord_event_id',
    'music_channel_id',
    'log_channel_id',
    'welcome_channel_id',
    'goodbye_channel_id',
}


def identifier_findings(value, path=''):
    """Yield suspect ID paths without returning unrelated document contents."""
    if isinstance(value, dict):
        for key, item in value.items():
            nested = f'{path}.{key}' if path else key
            if key in ID_FIELDS and item is not None:
                reason = None
                if isinstance(item, bool):
                    reason = 'invalid_type'
                elif isinstance(item, float):
                    reason = 'unsafe_double' if not math.isfinite(item) or abs(item) > 2**53 - 1 else 'double_id'
                elif isinstance(item, int):
                    if not 0 < item <= 2**63 - 1:
                        reason = 'invalid_id'
                    elif item > 2**53 - 1 and int(float(item)) == item:
                        reason = 'precision_ambiguous'
                elif isinstance(item, str):
                    if not re.fullmatch(r'[1-9]\d{0,18}', item) or int(item) > 2**63 - 1:
                        reason = 'invalid_id'
                    elif int(item) > 2**53 - 1 and int(float(int(item))) == int(item):
                        reason = 'precision_ambiguous'
                else:
                    reason = 'invalid_type'
                if reason:
                    yield {'path': nested, 'reason': reason, 'value': str(item)}
            yield from identifier_findings(item, nested)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from identifier_findings(item, f'{path}.{index}')


def audit_identifiers(db, *, samples=20):
    if not 1 <= samples <= 100:
        raise ValueError('samples must be between 1 and 100')
    report = {
        'generated_at': datetime.now(UTC).isoformat(),
        'read_only': True,
        'status': 'clean',
        'collections': [],
        'errors': [],
        'limitations': [
            'Representable large integers can be legitimate snowflakes or rounded legacy IDs.',
            'Exact stored integers do not prove historical ownership; independently verify candidates.',
            'Client-side signed cookies are not stored in MongoDB; unsafe numeric cookies now require OAuth login.',
            'The inventory covers known named ID fields; embedded ID map keys and external backups need separate review.',
        ],
    }
    try:
        names = db.list_collection_names()
    except Exception as error:
        report['errors'].append({'operation': 'list_collections', 'error': type(error).__name__})
        names = []
    for name in sorted(names):
        totals, examples, documents = Counter(), [], 0
        try:
            with db[name].find({}).max_time_ms(30_000) as cursor:
                for document in cursor:
                    documents += 1
                    for finding in identifier_findings(document):
                        totals[finding['reason']] += 1
                        if len(examples) < samples:
                            examples.append({'document_id': str(document.get('_id')), **finding})
        except Exception as error:
            report['errors'].append({'collection': name, 'error': type(error).__name__})
        report['collections'].append({'name': name, 'documents': documents, 'findings': dict(totals), 'samples': examples})
    report['summary'] = {
        'collections': len(names),
        'documents': sum(item['documents'] for item in report['collections']),
        'findings': sum(sum(item['findings'].values()) for item in report['collections']),
        'errors': len(report['errors']),
    }
    if report['errors']:
        report['status'] = 'incomplete'
    elif report['summary']['findings']:
        report['status'] = 'needs_verification'
    return report
