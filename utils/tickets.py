"""Ticket configuration, validation, and transcript helpers."""

from datetime import UTC, datetime
from html import escape
import json
import re
import secrets
from typing import Any


TICKET_STATUSES = {'open', 'closed', 'deleted'}
TICKET_PRIORITIES = {'low', 'normal', 'high', 'urgent'}


def _integer(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _snowflakes(values: Any, limit: int = 20) -> list[int]:
    result: list[int] = []
    for value in values if isinstance(values, list) else []:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0 and parsed not in result:
            result.append(parsed)
    return result[:limit]


def normalize_ticket_panel(data: dict[str, Any] | None) -> dict[str, Any]:
    """Return a bounded, storage-safe panel configuration."""
    source = data if isinstance(data, dict) else {}
    questions = []
    for item in source.get('questions', []) if isinstance(source.get('questions'), list) else []:
        if not isinstance(item, dict) or not str(item.get('label', '')).strip():
            continue
        questions.append(
            {
                'id': re.sub(r'[^a-z0-9_-]', '', str(item.get('id', '')).lower())[:40] or secrets.token_hex(4),
                'label': str(item['label']).strip()[:45],
                'placeholder': str(item.get('placeholder', '')).strip()[:100],
                'required': bool(item.get('required', True)),
                'paragraph': bool(item.get('paragraph', False)),
            }
        )
    title = str(source.get('title') or source.get('name') or 'Support tickets').strip()[:100]
    return {
        'name': str(source.get('name') or title).strip()[:80],
        'title': title,
        'description': str(source.get('description') or 'Open a private ticket with the team.').strip()[:2000],
        'button_label': str(source.get('button_label') or 'Open ticket').strip()[:80],
        'button_emoji': str(source.get('button_emoji') or '🎫').strip()[:32],
        'category_name': str(source.get('category_name') or 'Support').strip()[:80],
        'discord_category_id': (int(source['discord_category_id']) if str(source.get('discord_category_id', '')).isdigit() else None),
        'staff_role_ids': _snowflakes(source.get('staff_role_ids')),
        'questions': questions[:4],
        'evidence_enabled': bool(source.get('evidence_enabled', True)),
        'anonymous': bool(source.get('anonymous', False)),
        'max_open_per_member': _integer(source.get('max_open_per_member'), 1, 1, 10),
        'cooldown_seconds': _integer(source.get('cooldown_seconds'), 300, 0, 604800),
        'inactivity_reminder_hours': _integer(source.get('inactivity_reminder_hours'), 48, 0, 720),
        'inactivity_close_hours': _integer(source.get('inactivity_close_hours'), 72, 0, 2160),
        'retention_days': _integer(source.get('retention_days'), 365, 1, 3650),
        'enabled': bool(source.get('enabled', True)),
    }


def safe_channel_name(ticket_id: int, category_name: str, member_name: str = '') -> str:
    suffix = re.sub(r'[^a-z0-9-]+', '-', member_name.lower()).strip('-')[:40]
    category = re.sub(r'[^a-z0-9-]+', '-', category_name.lower()).strip('-')[:30] or 'ticket'
    return f'{category}-{ticket_id}' + (f'-{suffix}' if suffix else '')


def transcript_payload(ticket: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        'ticket': {key: value for key, value in ticket.items() if key != '_id'},
        'messages': messages,
        'generated_at': datetime.now(UTC).isoformat(),
    }


def render_transcript(ticket: dict[str, Any], messages: list[dict[str, Any]], format_name: str) -> str:
    """Render a portable transcript without trusting Discord-authored HTML."""
    if format_name == 'json':
        payload = transcript_payload(ticket, messages)
        return json.dumps(payload, default=str, ensure_ascii=False, indent=2)

    lines = []
    for message in messages:
        timestamp = str(message.get('created_at') or '')
        author = str(message.get('author_name') or message.get('author_id') or 'Unknown')
        content = str(message.get('content') or '')
        attachments = [str(item) for item in message.get('attachments', [])]
        if format_name == 'markdown':
            lines.append(f'**{author}** · {timestamp}\n\n{content}')
            lines.extend(f'- Attachment: {url}' for url in attachments)
            lines.append('')
        else:
            links = ''.join(f'<li><a href="{escape(url, quote=True)}">{escape(url)}</a></li>' for url in attachments)
            lines.append('<article><header><strong>' + escape(author) + '</strong> <time>' + escape(timestamp) + '</time></header><p>' + escape(content).replace('\n', '<br>') + '</p>' + (f'<ul>{links}</ul>' if links else '') + '</article>')
    if format_name == 'markdown':
        return f'# Ticket #{ticket.get("ticket_id", "?")} transcript\n\n' + '\n'.join(lines)
    if format_name != 'html':
        raise ValueError('Transcript format must be html, markdown, or json.')
    title = f'Ticket #{ticket.get("ticket_id", "?")} transcript'
    return (
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f'<title>{escape(title)}</title><style>body{{font:16px system-ui;max-width:900px;margin:40px auto;'
        'padding:0 20px;background:#111827;color:#e5e7eb}article{border-bottom:1px solid #374151;padding:16px 0}'
        'header,time{color:#9ca3af}p{white-space:normal}a{color:#93c5fd}</style></head><body>'
        f'<h1>{escape(title)}</h1>{"".join(lines)}</body></html>'
    )
