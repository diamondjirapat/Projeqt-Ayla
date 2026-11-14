"""Audit configured MongoDB without creating indexes or changing records.

Run from the repository root: python -m scripts.audit_database
Exit codes: 0 clean, 1 findings, 2 unavailable/incomplete.
"""

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from pymongo import MongoClient

from database.audit import audit_database
from database.identity_audit import audit_identifiers


def json_safe(value):
    if isinstance(value, int) and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('reports/database-audit/latest.json'))
    parser.add_argument('--samples', type=int, choices=range(1, 101), default=10)
    parser.add_argument(
        '--identifiers', action='store_true', help='Audit legacy identifier precision instead of indexes'
    )
    args = parser.parse_args(argv)
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    client = None
    try:
        uri = os.getenv('MONGODB_URI')
        if not uri:
            raise ValueError('Missing database configuration')
        client = MongoClient(uri, serverSelectionTimeoutMS=5_000, connectTimeoutMS=5_000)
        client.admin.command('ping')
        # Match the application's current database selection exactly.
        database_name = urlparse(uri).path.lstrip('/') or 'discord_bot'
        audit = audit_identifiers if args.identifiers else audit_database
        report = audit(client[database_name], samples=args.samples)
    except Exception as error:
        report = {
            'generated_at': datetime.now(UTC).isoformat(),
            'read_only': True,
            'status': 'unavailable',
            'errors': [{'error': type(error).__name__}],
        }
    finally:
        if client is not None:
            client.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(json_safe(report), indent=2, default=str) + '\n', encoding='utf-8')
    print(f'Database audit: {report["status"]}; report: {args.output}')
    if 'summary' in report:
        print(json.dumps(report['summary']))
    return (
        0 if report['status'] == 'clean' else 1 if report['status'] in ('needs_attention', 'needs_verification') else 2
    )


if __name__ == '__main__':
    raise SystemExit(main())
