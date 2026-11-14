from copy import deepcopy
import re
from typing import Any, Dict, Mapping, Optional, Tuple
import urllib.parse

import discord

from utils.cache import CACHE_MISS, TTLCache

_HEX_COLOR_PATTERN = re.compile(r'^#[0-9a-f]{6}$', re.IGNORECASE)
_HTML_TAG_PATTERN = re.compile(r'<[^>]*>')

_DEFAULT_CONFIG: Dict[str, Any] = {
    'now_playing': {
        'content': '',
        'title': '{title}',
        'description': '**[{title}]({url})**\n\n👤 {artist}\n⏱️ {duration}\n🎵 {source}\n🎧 {requester}',
        'color': '#5865f2',
        'image_url': '{bar}',
        'thumbnail_url': '{artwork}',
        'footer_text': '{server} • Vol: {volume}%',
        'footer_icon_url': '{server_icon}',
        'author': {
            'name': '{bot_name}',
            'url': '',
            'icon_url': '{bot_avatar}',
        },
        'fields': [],
    },
    'idle': {
        'content': '',
        'title': '🎵 Music Player',
        'description': (
            'No track is currently playing. Send a song title or link in {channel_mention} '
            'or use buttons below to start listening!'
        ),
        'color': '#5865f2',
        'image_url': '{banner}',
        'thumbnail_url': '{server_icon}',
        'footer_text': '{server}',
        'footer_icon_url': '{server_icon}',
        'author': {
            'name': '{bot_name}',
            'url': '',
            'icon_url': '{bot_avatar}',
        },
        'fields': [],
    },
}


_DISCORD_REFRESHED_URLS_CACHE: TTLCache[str, Tuple[str, float]] = TTLCache(
    ttl=21600, max_size=2000, name='music.attachment_urls'
)
_DISCORD_ATTACHMENT_DOMAINS = ('discord.com', 'discordapp.com', 'discordapp.net')


def is_discord_attachment_url(url: str) -> bool:
    """Return True if the URL points to a Discord CDN attachment."""
    try:
        parts = urllib.parse.urlsplit(str(url or '').strip())
        host = (parts.hostname or '').lower().rstrip('.')
        # Match the Discord domains themselves or their subdomains, never look-alikes such as evildiscord.com.
        if not any(host == domain or host.endswith('.' + domain) for domain in _DISCORD_ATTACHMENT_DOMAINS):
            return False
        return '/attachments/' in parts.path
    except Exception:
        return False


def extract_discord_attachment_expiry(url: str) -> float:
    """Extract unix timestamp from the `ex` query param of a Discord attachment URL."""
    try:
        parts = urllib.parse.urlsplit(str(url or '').strip())
        qs = urllib.parse.parse_qs(parts.query)
        ex_hex = qs.get('ex', ['0'])[0]
        return float(int(ex_hex, 16))
    except Exception:
        return 0.0


def is_discord_attachment_expired(url: str) -> bool:
    """Return True if the URL is a Discord CDN attachment link whose signature has expired."""
    if not is_discord_attachment_url(url):
        return False
    exp = extract_discord_attachment_expiry(url)
    if exp <= 0:
        return True
    import time

    return time.time() >= (exp - 60)


def get_cached_discord_attachment_url(url: str) -> Optional[str]:
    """Return a currently valid cached refreshed URL for this attachment, or None."""
    if not is_discord_attachment_url(url):
        return None
    import time

    clean = str(url or '').strip().split('?')[0]
    cached = _DISCORD_REFRESHED_URLS_CACHE.get(clean)
    if cached is not CACHE_MISS and cached[1] > time.time() + 60:
        return cached[0]
    return None


_REFRESH_URLS_ENDPOINT = 'https://discord.com/api/v10/attachments/refresh-urls'


async def _post_refresh_batch(session, token: str, batch: list[str]) -> Optional[dict]:
    """POST one batch to Discord's refresh endpoint; returns the JSON body or None on failure."""
    async with session.post(
        _REFRESH_URLS_ENDPOINT,
        json={'attachment_urls': batch},
        headers={'Authorization': f'Bot {token}'},
    ) as resp:
        if resp.status != 200:
            return None
        return await resp.json()


async def refresh_discord_attachment_urls(urls: list[str], bot_token: Optional[str] = None) -> dict[str, str]:
    """Refresh Discord attachment URLs using Discord's /attachments/refresh-urls API."""
    import time

    result: dict[str, str] = {}
    need_refresh: list[str] = []

    for u in urls:
        raw_u = str(u or '').strip()
        if not raw_u:
            continue
        if not is_discord_attachment_url(raw_u):
            result[raw_u] = raw_u
            continue
        clean = raw_u.split('?')[0]
        cached = _DISCORD_REFRESHED_URLS_CACHE.get(clean)
        if cached is not CACHE_MISS and cached[1] > time.time() + 60:
            result[raw_u] = cached[0]
            continue
        if not is_discord_attachment_expired(raw_u) and extract_discord_attachment_expiry(raw_u) > 0:
            result[raw_u] = raw_u
            _DISCORD_REFRESHED_URLS_CACHE.set(
                clean,
                (raw_u, extract_discord_attachment_expiry(raw_u) - 300),
                ttl=max(1, extract_discord_attachment_expiry(raw_u) - time.time() - 300),
            )
            continue
        need_refresh.append(clean)

    if not need_refresh:
        return result

    token = bot_token
    if not token:
        try:
            from config import Config

            tokens = getattr(Config, 'DISCORD_TOKENS', [])
            if tokens:
                token = tokens[0]
            if not token:
                import os

                token = os.getenv('DISCORD_TOKEN')
        except Exception:
            pass

    if not token:
        return result

    try:
        import aiohttp

        # Process in batches of up to 50
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            for i in range(0, len(need_refresh), 50):
                batch = need_refresh[i : i + 50]
                data = await _post_refresh_batch(session, token, batch)
                if data is not None:
                    for item in data.get('refreshed_urls', []):
                        refreshed = item.get('refreshed', '')
                        orig = item.get('original', '')
                        clean_orig = orig.split('?')[0]
                        clean_refreshed = refreshed.split('?')[0]
                        exp = extract_discord_attachment_expiry(refreshed)
                        cache_exp = exp - 300 if exp > time.time() else time.time() + 21600
                        _DISCORD_REFRESHED_URLS_CACHE.set(
                            clean_orig, (refreshed, cache_exp), ttl=max(1, cache_exp - time.time())
                        )
                        _DISCORD_REFRESHED_URLS_CACHE.set(
                            clean_refreshed, (refreshed, cache_exp), ttl=max(1, cache_exp - time.time())
                        )
                        for u in urls:
                            raw_u = str(u or '').strip()
                            if raw_u.split('?')[0] == clean_orig:
                                result[raw_u] = refreshed
    except Exception:
        pass

    return result


async def refresh_discord_attachment_url(url: str, bot_token: Optional[str] = None) -> str:
    """Refresh a single Discord attachment URL; returns refreshed URL or original/fallback."""
    raw = str(url or '').strip()
    if not raw:
        return ''
    mapping = await refresh_discord_attachment_urls([raw], bot_token=bot_token)
    return mapping.get(raw, raw)


def is_http_url(value: str) -> bool:
    """Return True if value has an http/https scheme and a valid hostname."""
    parts = urllib.parse.urlsplit(str(value or '').strip())
    return parts.scheme in ('http', 'https') and bool(parts.hostname)


def resolve_music_url(raw_url: str | None = None, fallback: str = '') -> str:
    """Return the normalized URL if valid and not expired, otherwise fallback."""
    url = str(raw_url or '').strip()
    if not url:
        return fallback
    if is_discord_attachment_url(url):
        cached = get_cached_discord_attachment_url(url)
        if cached:
            return cached
    if is_discord_attachment_expired(url):
        return fallback
    if is_http_url(url):
        return url
    if url.startswith('/'):
        return url
    if url.startswith('static/'):
        return f'/{url[7:]}'
    if url.startswith('@static/'):
        return f'/{url[8:]}'
    if re.match(r'^[a-zA-Z0-9_\-.]+\.(png|jpe?g|svg|webp|gif|ico)$', url, re.IGNORECASE):
        return f'/{url}'
    return fallback


def resolve_music_banner_url(raw_url: str | None = None) -> str:
    return resolve_music_url(raw_url, fallback='')


def resolve_music_bar_url(raw_url: str | None = None) -> str:
    return resolve_music_url(raw_url, fallback='')


async def resolve_music_url_async(raw_url: str | None = None, fallback: str = '') -> str:
    """Async URL resolution that refreshes Discord attachments via API if needed."""
    url = str(raw_url or '').strip()
    if not url:
        return fallback
    if is_discord_attachment_url(url):
        refreshed = await refresh_discord_attachment_url(url)
        if refreshed and not is_discord_attachment_expired(refreshed):
            return refreshed
        return fallback
    return resolve_music_url(url, fallback=fallback)


async def resolve_music_banner_url_async(raw_url: str | None = None) -> str:
    return await resolve_music_url_async(raw_url, fallback='/music-banner.png')


async def resolve_music_bar_url_async(raw_url: str | None = None) -> str:
    return await resolve_music_url_async(raw_url, fallback='/bar444.png')


def extract_discord_attachment_urls_from_config(config: Dict[str, Any]) -> list[str]:
    """Scan a music embed configuration dictionary for any Discord attachment URLs."""
    urls: list[str] = []
    if not isinstance(config, dict):
        return urls

    def _check(val: Any) -> None:
        if isinstance(val, str) and is_discord_attachment_url(val):
            urls.append(val.strip())

    for pane_name in ('now_playing', 'idle'):
        pane = config.get(pane_name)
        if not isinstance(pane, dict):
            continue
        _check(pane.get('image_url'))
        _check(pane.get('thumbnail_url'))
        _check(pane.get('footer_icon_url'))
        author = pane.get('author')
        if isinstance(author, dict):
            _check(author.get('icon_url'))
            _check(author.get('url'))

    return urls


async def refresh_music_embed_config_attachments(config: Dict[str, Any], bot_token: Optional[str] = None) -> None:
    """Pre-refresh all Discord attachment URLs present in a music embed configuration."""
    urls = extract_discord_attachment_urls_from_config(config)
    if urls:
        await refresh_discord_attachment_urls(urls, bot_token=bot_token)


def normalize_http_url(url: str) -> str:
    """Convert an HTTP URL or site-relative asset path to a fully qualified HTTP URL.

    Site-relative paths (``/x.png``, ``static/x.png``, ``@static/x.png`` and bare
    image filenames such as ``x.png``) resolve against ``WEB_URL``, mirroring
    :func:`resolve_music_url`. Anything else resolves to ``''`` (omitted).
    """
    cleaned = str(url or '').strip()
    if not cleaned:
        return ''
    if is_discord_attachment_url(cleaned):
        cached = get_cached_discord_attachment_url(cleaned)
        if cached:
            return cached
        if is_discord_attachment_expired(cleaned):
            return ''
    elif is_discord_attachment_expired(cleaned):
        return ''
    if is_http_url(cleaned):
        return cleaned
    relative = cleaned
    if relative.startswith('@static/'):
        relative = '/' + relative[len('@static/') :]
    elif relative.startswith('static/'):
        relative = '/' + relative[len('static/') :]
    elif re.match(r'^[a-zA-Z0-9_\-.]+\.(png|jpe?g|svg|webp|gif|ico)$', relative, re.IGNORECASE):
        relative = '/' + relative
    if relative.startswith('/'):
        try:
            from config import Config

            web_url = getattr(Config, 'WEB_URL', '')
            if web_url:
                candidate = f'{web_url.rstrip("/")}{relative}'
                if is_http_url(candidate):
                    return candidate
        except Exception:
            pass
    return ''


def get_default_music_embed_config() -> Dict[str, Any]:
    """Return a deepcopy of the default music embed appearance configuration."""
    return deepcopy(_DEFAULT_CONFIG)


def get_default_music_channel_embed_config() -> Dict[str, Any]:
    """The music channel has one directory design, independent of voice players."""
    pane = deepcopy(_DEFAULT_CONFIG['idle'])
    pane.update(
        title='{directory_title}',
        description='{directory_description}\n\n{players}',
        image_url='',
        thumbnail_url='',
        footer_text='',
        footer_icon_url='',
        author={'name': '', 'url': '', 'icon_url': ''},
    )
    return {'idle': pane}


def normalize_music_channel_embed_config(raw_config: Any) -> Dict[str, Any]:
    default = get_default_music_channel_embed_config()
    raw = raw_config.get('idle') if isinstance(raw_config, dict) else None
    return {'idle': _normalize_pane(raw, default['idle'])}


def build_music_channel_embed(
    guild_id: int, config: Dict[str, Any], placeholders: Mapping[str, str]
) -> Tuple[discord.Embed, str | None]:
    return build_custom_music_embed(
        guild_id,
        'idle',
        normalize_music_channel_embed_config(config),
        placeholders,
        default_config=get_default_music_channel_embed_config(),
    )


def _normalize_pane(raw: Any, default_pane: Dict[str, Any]) -> Dict[str, Any]:
    pane = deepcopy(default_pane)
    if not isinstance(raw, dict):
        return pane

    for key, limit in [
        ('content', 2000),
        ('title', 256),
        ('description', 4096),
        ('image_url', 2048),
        ('thumbnail_url', 2048),
        ('footer_text', 2048),
        ('footer_icon_url', 2048),
    ]:
        if key in raw:
            val = _HTML_TAG_PATTERN.sub('', str(raw.get(key) or '')).strip()
            pane[key] = val[:limit]

    if 'color' in raw:
        color = str(raw.get('color') or '').strip()
        if _HEX_COLOR_PATTERN.fullmatch(color):
            pane['color'] = color.lower()
        else:
            pane['color'] = default_pane['color']

    if 'author' in raw and isinstance(raw.get('author'), dict):
        raw_author = raw['author']
        pane['author'] = {
            'name': _HTML_TAG_PATTERN.sub('', str(raw_author.get('name') or '')).strip()[:256],
            'url': str(raw_author.get('url') or '').strip()[:2048],
            'icon_url': str(raw_author.get('icon_url') or '').strip()[:2048],
        }

    if 'fields' in raw and isinstance(raw.get('fields'), list):
        fields = []
        for f in raw['fields']:
            if not isinstance(f, dict):
                continue
            fname = _HTML_TAG_PATTERN.sub('', str(f.get('name') or '\u200b')).strip()[:256]
            fval = _HTML_TAG_PATTERN.sub('', str(f.get('value') or '\u200b')).strip()[:1024]
            if fname or fval:
                fields.append(
                    {
                        'name': fname or '\u200b',
                        'value': fval or '\u200b',
                        'inline': bool(f.get('inline')),
                    }
                )
        pane['fields'] = fields[:25]

    return pane


def normalize_music_embed_config(raw_config: Any) -> Dict[str, Any]:
    """Normalize raw music embed appearance configuration against limits and schemas."""
    default = get_default_music_embed_config()
    if not isinstance(raw_config, dict):
        return default

    # Note: legacy configs may still carry an `enabled` flag from when the
    # custom embed was opt-in; it is ignored now that custom embeds always apply.
    return {
        'now_playing': _normalize_pane(raw_config.get('now_playing'), default['now_playing']),
        'idle': _normalize_pane(raw_config.get('idle'), default['idle']),
    }


def render_music_embed_template(template: str, placeholders: Mapping[str, str]) -> str:
    """Replace {variable} tokens in template with values from placeholders case-insensitively."""
    rendered = str(template or '')
    for key, val in (placeholders or {}).items():
        pattern = re.compile(rf'\{{{re.escape(str(key))}\}}', re.IGNORECASE)
        val_str = str(val if val is not None else '')
        rendered = pattern.sub(lambda _, s=val_str: s, rendered)
    return rendered


def _url_template_or_default(
    pane: Mapping[str, Any], default_pane: Mapping[str, Any], key: str, *, fallback_to_default: bool = True
) -> str:
    """Return the URL template for key, optionally falling back to the default pane.

    Only the main ``image_url`` falls back: an empty image means "not customized"
    (e.g. a blank web-form field), so it resolves to the default (such as
    ``{banner}``/``{bar}``) and the configured ``MUSIC_BANNER_URL``/``BAR_URL``
    still loads. An empty ``thumbnail_url``/``footer_icon_url`` means "hidden"
    (the dashboard thumbnail toggle's off state), so no fallback applies and the
    preview matches the sent embed. A non-empty but invalid URL is respected
    as-is and later omitted, so ``none``/garbage still means no image.
    """
    raw = pane.get(key) if isinstance(pane, Mapping) else None
    if raw is not None and str(raw).strip() != '':
        return str(raw)
    if not fallback_to_default:
        return ''
    default_raw = default_pane.get(key) if isinstance(default_pane, Mapping) else None
    return str(default_raw or '')


def _author_url_template_or_default(
    pane: Mapping[str, Any], default_pane: Mapping[str, Any], key: str, *, fallback_to_default: bool = False
) -> str:
    """Return the author URL template for key.

    An empty author ``url``/``icon_url`` means "hidden", so no fallback applies
    and the preview matches the sent embed. The dashboard author toggle's off
    state clears the author name, which already hides the whole author block.
    """
    pane_author = pane.get('author') if isinstance(pane.get('author'), dict) else {}
    default_author = default_pane.get('author') if isinstance(default_pane.get('author'), dict) else {}
    raw = pane_author.get(key) if isinstance(pane_author, dict) else None
    if raw is not None and str(raw).strip() != '':
        return str(raw)
    if not fallback_to_default:
        return ''
    default_raw = default_author.get(key) if isinstance(default_author, dict) else None
    return str(default_raw or '')


def build_custom_music_embed(
    guild_id: int,
    state: str,
    config: Dict[str, Any],
    placeholders: Mapping[str, str],
    *,
    default_config: Optional[Dict[str, Any]] = None,
) -> Tuple[discord.Embed, str | None]:
    """Build a discord.Embed and optional content string from custom config and placeholders."""
    if not isinstance(config, dict):
        config = _DEFAULT_CONFIG

    defaults = default_config or _DEFAULT_CONFIG
    default_pane = defaults.get(state) or defaults.get('now_playing') or _DEFAULT_CONFIG['now_playing']
    pane = config.get(state)
    if not isinstance(pane, dict):
        pane = config.get('now_playing')
    if not isinstance(pane, dict):
        pane = default_pane

    raw_color = str(pane.get('color') or '#5865f2').strip()
    if not _HEX_COLOR_PATTERN.fullmatch(raw_color):
        raw_color = '#5865f2'
    color_int = int(raw_color.lstrip('#'), 16)
    embed = discord.Embed(color=discord.Color(color_int))

    title = render_music_embed_template(pane.get('title', ''), placeholders).strip()
    if title:
        embed.title = title[:256]

    description = render_music_embed_template(pane.get('description', ''), placeholders).strip()
    if description:
        embed.description = description[:4096]

    raw_image = render_music_embed_template(
        _url_template_or_default(pane, default_pane, 'image_url'), placeholders
    ).strip()
    norm_image = normalize_http_url(raw_image)
    if is_http_url(norm_image):
        embed.set_image(url=norm_image[:2048])

    raw_thumb = render_music_embed_template(
        _url_template_or_default(pane, default_pane, 'thumbnail_url', fallback_to_default=False), placeholders
    ).strip()
    norm_thumb = normalize_http_url(raw_thumb)
    if is_http_url(norm_thumb):
        embed.set_thumbnail(url=norm_thumb[:2048])

    raw_footer = render_music_embed_template(pane.get('footer_text', ''), placeholders).strip()
    raw_footer_icon = render_music_embed_template(
        _url_template_or_default(pane, default_pane, 'footer_icon_url', fallback_to_default=False), placeholders
    ).strip()
    norm_footer_icon = normalize_http_url(raw_footer_icon)
    if raw_footer or is_http_url(norm_footer_icon):  # text optional if icon is a URL or {variable}
        embed.set_footer(
            text=raw_footer[:2048] or None,
            icon_url=norm_footer_icon[:2048] if is_http_url(norm_footer_icon) else None,
        )

    author = pane.get('author') if isinstance(pane.get('author'), dict) else {}
    raw_author_name = render_music_embed_template(author.get('name', ''), placeholders).strip()
    if raw_author_name:
        raw_author_url = render_music_embed_template(
            _author_url_template_or_default(pane, default_pane, 'url'), placeholders
        ).strip()
        norm_author_url = normalize_http_url(raw_author_url)
        raw_author_icon = render_music_embed_template(
            _author_url_template_or_default(pane, default_pane, 'icon_url'), placeholders
        ).strip()
        norm_author_icon = normalize_http_url(raw_author_icon)
        embed.set_author(
            name=raw_author_name[:256],
            url=norm_author_url[:2048] if is_http_url(norm_author_url) else None,
            icon_url=norm_author_icon[:2048] if is_http_url(norm_author_icon) else None,
        )

    for field in pane.get('fields', []) if isinstance(pane.get('fields'), list) else []:
        if not isinstance(field, dict):
            continue
        f_name = render_music_embed_template(field.get('name', ''), placeholders).strip() or '\u200b'
        f_val = render_music_embed_template(field.get('value', ''), placeholders).strip() or '\u200b'
        embed.add_field(name=f_name[:256], value=f_val[:1024], inline=bool(field.get('inline')))

    raw_content = render_music_embed_template(pane.get('content', ''), placeholders).strip()
    content = raw_content[:2000] if raw_content else None

    return embed, content
