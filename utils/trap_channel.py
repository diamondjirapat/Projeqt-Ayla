"""Trap channel configuration: a channel where any message is deleted and its author penalised.

Spam bots and compromised accounts tend to post in every channel they can see, so a clearly
labelled "do not type here" channel catches them without catching people who read the warning.
"""

import re
from copy import deepcopy
from typing import Any, Dict, Mapping

from utils.greetings import is_http_url, render_greeting_template

TRAP_ACTIONS = ('timeout', 'kick', 'ban', 'delete')
DEFAULT_TIMEOUT_MINUTES = 30
# Discord caps member timeouts at 28 days.
MAX_TIMEOUT_MINUTES = 28 * 24 * 60
MAX_EXEMPT_ROLES = 25
TRAP_PLACEHOLDERS = ('{server}', '{channel}', '{penalty}', '{duration}')

_HEX_COLOR_PATTERN = re.compile(r'^#[0-9a-f]{6}$', re.IGNORECASE)
_HTML_TAG_PATTERN = re.compile(r'<[^>]*>')
_DISCORD_TOKEN_PATTERN = re.compile(r'<(?:@!?|@&|#)[0-9]+>|<a?:[A-Za-z0-9_]+:[0-9]+>|<t:-?[0-9]+(?::[tTdDfFR])?>')

_DEFAULT_EMBED: Dict[str, Any] = {
    'content': '',
    'title': '⚠️ Do not send messages in this channel',
    'description': (
        'This channel is a trap for spam bots and compromised accounts.\n\n'
        '**Any message sent here is deleted immediately and the sender is {penalty}.**\n\n'
        'If you are a real member of {server}, please do not type here.'
    ),
    'color': '#ef4444',
    'image_url': '',
    'thumbnail_url': '',
    'footer_text': '{server} • Automatic protection',
    'footer_icon_url': '',
    'author': {'name': '', 'url': '', 'icon_url': ''},
    'fields': [],
}

_DEFAULTS: Dict[str, Any] = {
    'enabled': False,
    'channel_id': None,
    'message_id': None,
    'action': 'timeout',
    'timeout_minutes': DEFAULT_TIMEOUT_MINUTES,
    'punish_bots': True,
    'log_case': True,
    'exempt_role_ids': [],
    'reason': 'Sent a message in the trap channel',
    'embed': _DEFAULT_EMBED,
}

_EMBED_LIMITS = {
    'content': 2000,
    'title': 256,
    'description': 4096,
    'image_url': 2048,
    'thumbnail_url': 2048,
    'footer_text': 2048,
    'footer_icon_url': 2048,
}


def _clean(value: Any, limit: int) -> str:
    text = str(value or '')
    # Discord mentions, custom emoji, and timestamps also use angle brackets.
    text = _HTML_TAG_PATTERN.sub(lambda match: match[0] if _DISCORD_TOKEN_PATTERN.fullmatch(match[0]) else '', text)
    return text.strip()[:limit]


def _parse_id(value: Any) -> int | None:
    if value is None or str(value).strip().lower() in ('', 'none', 'null'):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def default_trap_channel_config() -> Dict[str, Any]:
    return deepcopy(_DEFAULTS)


def normalize_trap_embed(raw: Any) -> Dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    embed = deepcopy(_DEFAULT_EMBED)
    for key, limit in _EMBED_LIMITS.items():
        if key in source:
            embed[key] = _clean(source[key], limit)
    color = str(source.get('color') or embed['color']).strip()
    embed['color'] = color.lower() if _HEX_COLOR_PATTERN.fullmatch(color) else _DEFAULT_EMBED['color']
    raw_author = source.get('author') if isinstance(source.get('author'), dict) else {}
    embed['author'] = {
        'name': _clean(raw_author.get('name'), 256),
        'url': str(raw_author.get('url') or '').strip()[:2048],
        'icon_url': str(raw_author.get('icon_url') or '').strip()[:2048],
    }
    fields = []
    for item in source.get('fields', []) if isinstance(source.get('fields'), list) else []:
        if not isinstance(item, dict) or not (item.get('name') or item.get('value')):
            continue
        fields.append(
            {
                'name': _clean(item.get('name') or '​', 256),
                'value': _clean(item.get('value') or '​', 1024),
                'inline': bool(item.get('inline')),
            }
        )
    embed['fields'] = fields[:25]
    return embed


def normalize_trap_channel_config(raw: Any) -> Dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    config = default_trap_channel_config()
    config['enabled'] = bool(source.get('enabled', config['enabled']))
    config['channel_id'] = _parse_id(source.get('channel_id'))
    config['message_id'] = _parse_id(source.get('message_id'))

    action = str(source.get('action') or config['action']).strip().lower()
    config['action'] = action if action in TRAP_ACTIONS else 'timeout'

    try:
        minutes = int(source.get('timeout_minutes', DEFAULT_TIMEOUT_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_TIMEOUT_MINUTES
    config['timeout_minutes'] = max(1, min(MAX_TIMEOUT_MINUTES, minutes))

    config['punish_bots'] = bool(source.get('punish_bots', True))
    config['log_case'] = bool(source.get('log_case', True))
    reason = _clean(source.get('reason', config['reason']), 400)
    config['reason'] = reason or _DEFAULTS['reason']

    exempt = []
    for item in source.get('exempt_role_ids', []) if isinstance(source.get('exempt_role_ids'), list) else []:
        role_id = _parse_id(item)
        if role_id and role_id not in exempt:
            exempt.append(role_id)
    config['exempt_role_ids'] = exempt[:MAX_EXEMPT_ROLES]
    config['embed'] = normalize_trap_embed(source.get('embed'))
    return config


def serialize_trap_channel_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    """Discord ids leave the API as strings so JavaScript never rounds them."""
    data = deepcopy(dict(config))
    for key in ('channel_id', 'message_id'):
        data[key] = str(data[key]) if data.get(key) else None
    data['exempt_role_ids'] = [str(item) for item in data.get('exempt_role_ids', [])]
    return data


def format_duration(minutes: int) -> str:
    """Compact English duration such as ``30 minutes`` or ``1 day 2 hours``."""
    minutes = max(1, int(minutes))
    parts = []
    for size, name in ((1440, 'day'), (60, 'hour'), (1, 'minute')):
        amount, minutes = divmod(minutes, size)
        if amount:
            parts.append(f'{amount} {name}{"s" if amount != 1 else ""}')
    return ' '.join(parts)


def render_trap_embed_parts(config: Mapping[str, Any], values: Mapping[str, str]) -> Dict[str, Any]:
    """Render placeholders into a plain dict the cog turns into a ``discord.Embed``."""
    embed = normalize_trap_embed(config.get('embed'))

    def render(text: str) -> str:
        return render_greeting_template(text, values)

    def url(text: str) -> str | None:
        rendered = render(text)
        return rendered if is_http_url(rendered) else None

    return {
        'content': render(embed['content']),
        'title': render(embed['title']),
        'description': render(embed['description']),
        'color': int(embed['color'].lstrip('#'), 16),
        'image_url': url(embed['image_url']),
        'thumbnail_url': url(embed['thumbnail_url']),
        'footer_text': render(embed['footer_text']),
        'footer_icon_url': url(embed['footer_icon_url']),
        'author': {
            'name': render(embed['author']['name']),
            'url': url(embed['author']['url']),
            'icon_url': url(embed['author']['icon_url']),
        },
        'fields': [
            {'name': render(item['name']), 'value': render(item['value']), 'inline': item['inline']}
            for item in embed['fields']
        ],
    }
