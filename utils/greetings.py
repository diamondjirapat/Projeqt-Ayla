import re
import urllib.parse
from copy import deepcopy
from typing import Any, Dict, Mapping

from utils.level_alert_images import normalize_level_alert_visual


GREETING_KINDS = ('welcome', 'goodbye')


def _image_text_layer(
    layer_id: str,
    name: str,
    text: str,
    y: int,
    size: int,
    weight: int = 700,
    color: str = '#ffffff',
) -> Dict[str, Any]:
    return {
        'id': layer_id,
        'name': name,
        'text': text,
        'x': 50,
        'y': y,
        'font_family': 'outfit',
        'font_size': size,
        'font_weight': weight,
        'color': color,
        'align': 'center',
    }


def _default_image_card(kind: str) -> Dict[str, Any]:
    is_welcome = kind == 'welcome'
    return {
        'width': 1200,
        'height': 630,
        'background_color': '#0b0f14',
        'overlay': 38,
        'images': [
            {
                'id': 'image-1',
                'name': 'Background',
                'source': 'url',
                'url': '',
                'x': 50,
                'y': 50,
                'width': 100,
                'height': 100,
                'opacity': 100,
                'fit': 'cover',
            }
        ],
        'order': ['text:title', 'text:member', 'text:message', 'image:image-1'],
        'texts': [
            _image_text_layer('title', 'Title', 'WELCOME' if is_welcome else 'GOODBYE', 28, 76, 800),
            _image_text_layer('member', 'Member', '{display_name}', 51, 52, 700, '#c084fc'),
            _image_text_layer(
                'message',
                'Message',
                'Welcome to {server}' if is_welcome else 'Thanks for being part of {server}',
                72,
                32,
                600,
            ),
        ],
        'bars': [],
    }


_DEFAULTS: Dict[str, Dict[str, Any]] = {
    'welcome': {
        'enabled': False,
        'channel_id': None,
        'content': '',
        'title': 'Welcome to {server}!',
        'description': "Hey {mention}, you're member #{member_count}. Make yourself at home.",
        'color': '#8b5cf6',
        'image_url': '',
        'thumbnail_url': '{avatar}',
        'footer_text': '{server}',
        'footer_icon_url': '{server_icon}',
        'author': {'name': '', 'url': '', 'icon_url': ''},
        'fields': [],
        'mode': 'embed',
        'image_card': _default_image_card('welcome'),
    },
    'goodbye': {
        'enabled': False,
        'channel_id': None,
        'content': '',
        'title': 'Goodbye, {username}',
        'description': 'Thanks for being part of {server}. We hope to see you again.',
        'color': '#64748b',
        'image_url': '',
        'thumbnail_url': '{avatar}',
        'footer_text': '{server}',
        'footer_icon_url': '{server_icon}',
        'author': {'name': '', 'url': '', 'icon_url': ''},
        'fields': [],
        'mode': 'embed',
        'image_card': _default_image_card('goodbye'),
    },
}

_STRING_LIMITS = {
    'content': 2000,
    'title': 256,
    'description': 4096,
    'color': 7,
    'image_url': 2048,
    'thumbnail_url': 2048,
    'footer_text': 2048,
    'footer_icon_url': 2048,
}
_HTML_TAG_PATTERN = re.compile(r'<[^>]*>')
_DISCORD_TOKEN_PATTERN = re.compile(r'<(?:@!?|@&|#)[0-9]+>|<a?:[A-Za-z0-9_]+:[0-9]+>|<t:-?[0-9]+(?::[tTdDfFR])?>')
_HEX_COLOR_PATTERN = re.compile(r'^#[0-9a-f]{6}$', re.IGNORECASE)


def _strip_html_tags(value: str) -> str:
    # Discord mentions, custom emoji, and timestamps also use angle brackets.
    return _HTML_TAG_PATTERN.sub(
        lambda match: match[0] if _DISCORD_TOKEN_PATTERN.fullmatch(match[0]) else '', value
    ).strip()


def get_default_greeting_config(kind: str) -> Dict[str, Any]:
    if kind not in GREETING_KINDS:
        raise ValueError(f'Unsupported greeting kind: {kind}')
    config = deepcopy(_DEFAULTS[kind])
    # Fresh guilds default to ON (stays silent until a channel is bound).
    config['enabled'] = True
    return config


def normalize_greeting_config(kind: str, raw_config: Any) -> Dict[str, Any]:
    config = get_default_greeting_config(kind)
    if isinstance(raw_config, dict):
        for key in config:
            if key in raw_config:
                config[key] = raw_config[key]

    config['enabled'] = bool(config.get('enabled', True))

    raw_channel_id = config.get('channel_id')
    if raw_channel_id is None or str(raw_channel_id).strip().lower() in ('', 'none', 'null'):
        config['channel_id'] = None
    else:
        try:
            channel_id = int(raw_channel_id)
            config['channel_id'] = channel_id if channel_id > 0 else None
        except (TypeError, ValueError):
            config['channel_id'] = None

    for key, limit in _STRING_LIMITS.items():
        value = _strip_html_tags(str(config.get(key) or ''))
        config[key] = value[:limit]

    raw_author = config.get('author') if isinstance(config.get('author'), dict) else {}
    config['author'] = {
        'name': _strip_html_tags(str(raw_author.get('name') or ''))[:256],
        'url': str(raw_author.get('url') or '').strip()[:2048],
        'icon_url': str(raw_author.get('icon_url') or '').strip()[:2048],
    }
    fields = []
    for raw in config.get('fields', []) if isinstance(config.get('fields'), list) else []:
        if not isinstance(raw, dict) or not (raw.get('name') or raw.get('value')):
            continue
        fields.append(
            {
                'name': _strip_html_tags(str(raw.get('name') or '\u200b'))[:256],
                'value': _strip_html_tags(str(raw.get('value') or '\u200b'))[:1024],
                'inline': bool(raw.get('inline')),
            }
        )
    config['fields'] = fields[:25]

    image_card = config.get('image_card')
    if not isinstance(image_card, dict):
        image_card = _default_image_card(kind)
    visual = normalize_level_alert_visual({'mode': config.get('mode'), 'image_card': image_card})
    config['mode'] = visual['mode']
    config['image_card'] = visual['image_card']
    config['image_card']['bars'] = []
    config['image_card']['order'] = [key for key in config['image_card']['order'] if not key.startswith('bar:')]

    if not _HEX_COLOR_PATTERN.fullmatch(config['color']):
        config['color'] = _DEFAULTS[kind]['color']
    else:
        config['color'] = config['color'].lower()

    return config


def normalize_greetings(raw_greetings: Any) -> Dict[str, Dict[str, Any]]:
    source = raw_greetings if isinstance(raw_greetings, dict) else {}
    return {kind: normalize_greeting_config(kind, source.get(kind)) for kind in GREETING_KINDS}


def render_greeting_template(template: str, values: Mapping[str, str]) -> str:
    rendered = str(template or '')
    for name, value in values.items():
        rendered = rendered.replace(f'{{{name}}}', str(value))
    return rendered


def is_http_url(value: str) -> bool:
    parts = urllib.parse.urlsplit(str(value or '').strip())
    return parts.scheme in ('http', 'https') and bool(parts.hostname)
