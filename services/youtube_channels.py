"""Resolve user-pasted YouTube identities to a verified channel preview."""

import asyncio
import json
import re
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from config import Config
from utils.autoplay import YOUTUBE_CHANNEL_ID
from utils.cache import AsyncTTLCache
from utils.http import safe_http_request


class ChannelLookupError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def parse_channel_input(value: str) -> tuple[str, str]:
    value = value.strip()
    if not value or len(value) > 2048:
        raise ChannelLookupError('invalid_input')
    if YOUTUBE_CHANNEL_ID.fullmatch(value):
        return 'id', value
    if value.startswith('@') and re.fullmatch(r'@[^\s/?#\\]{1,100}', value):
        return 'handle', value
    if '://' not in value:
        value = 'https://' + value
    try:
        url = urlsplit(value)
        if url.scheme not in ('http', 'https') or url.username or url.password or url.port not in (None, 80, 443):
            raise ValueError
        host = (url.hostname or '').lower()
        parts = [unquote(part) for part in url.path.split('/') if part]
        if host == 'youtu.be' and len(parts) == 1:
            kind, target = 'video', parts[0]
        elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
            if parts == ['watch']:
                kind, target = 'video', parse_qs(url.query).get('v', [''])[0]
            elif len(parts) == 2 and parts[0] in ('shorts', 'live', 'embed', 'v'):
                kind, target = 'video', parts[1]
            elif len(parts) in (2, 3) and parts[0] in ('channel', 'user', 'c'):
                if len(parts) == 3 and parts[2] not in ('videos', 'shorts', 'streams', 'featured', 'about'):
                    raise ValueError
                kind, target = {'channel': 'id', 'user': 'username', 'c': 'custom'}[parts[0]], parts[1]
            elif parts and parts[0].startswith('@') and len(parts) <= 2:
                if len(parts) == 2 and parts[1] not in ('videos', 'shorts', 'streams', 'featured', 'about'):
                    raise ValueError
                kind, target = 'handle', parts[0]
            else:
                raise ValueError
        else:
            raise ValueError
        if kind == 'id' and not YOUTUBE_CHANNEL_ID.fullmatch(target):
            raise ValueError
        if kind == 'video' and not re.fullmatch(r'[A-Za-z0-9_-]{11}', target):
            raise ValueError
        if kind in ('handle', 'username', 'custom'):
            pattern = r'@[^\s/?#\\]{1,100}' if kind == 'handle' else r'[^\s/?#\\]{1,100}'
            if not re.fullmatch(pattern, target) or target in ('.', '..'):
                raise ValueError
        return kind, target
    except ValueError:
        raise ChannelLookupError('invalid_input') from None


def _preview(channel_id, name, avatar='') -> dict:
    if (
        not isinstance(channel_id, str)
        or not YOUTUBE_CHANNEL_ID.fullmatch(channel_id)
        or not isinstance(name, str)
        or not name.strip()
    ):
        raise ChannelLookupError('lookup_unavailable')
    image = urlsplit(avatar if isinstance(avatar, str) else '')
    if image.scheme != 'https' or not any(
        (image.hostname or '') == host or (image.hostname or '').endswith('.' + host)
        for host in ('googleusercontent.com', 'ggpht.com', 'ytimg.com')
    ):
        avatar = ''
    return {
        'id': channel_id,
        'name': name[:200],
        'url': f'https://www.youtube.com/channel/{channel_id}',
        'avatar': avatar,
    }


def _page_json(page: str, variable: str) -> dict:
    # Decode the named page object, never an arbitrary channelId from recommendations.
    for match in re.finditer(r'(?:var\s+)?' + re.escape(variable) + r'\s*=\s*', page):
        try:
            result, _ = json.JSONDecoder().raw_decode(page[match.end() :])
            if isinstance(result, dict):
                return result
        except ValueError:
            continue
    raise ChannelLookupError('lookup_unavailable')


async def _fetch(url: str) -> bytes:
    status, _, body, _ = await safe_http_request(
        'GET',
        url,
        allowed_domains=['youtube.com', 'googleapis.com'],
        timeout_seconds=5,
        max_response_bytes=8 * 1024 * 1024,
        headers={'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'en-US,en;q=0.9'},
    )
    if status == 404:
        raise ChannelLookupError('not_found')
    if status != 200:
        raise ChannelLookupError('lookup_unavailable')
    return body


async def _api(resource: str, **params) -> list:
    body = await _fetch(
        'https://www.googleapis.com/youtube/v3/'
        + resource
        + '?'
        + urlencode({'key': Config.YOUTUBE_API_KEY, 'part': 'snippet', **params})
    )
    return json.loads(body).get('items', [])


async def _load(kind: str, target: str) -> dict:
    if Config.YOUTUBE_API_KEY and kind != 'custom':
        try:
            if kind == 'video':
                videos = await _api('videos', id=target)
                if not videos:
                    raise ChannelLookupError('not_found')
                kind, target = 'id', videos[0]['snippet']['channelId']
            items = await _api(
                'channels', **{{'id': 'id', 'handle': 'forHandle', 'username': 'forUsername'}[kind]: target}
            )
            if not items:
                raise ChannelLookupError('not_found')
            item = items[0]
            snippet = item['snippet']
            return _preview(
                item['id'], snippet['title'], snippet.get('thumbnails', {}).get('default', {}).get('url', '')
            )
        except ChannelLookupError as error:
            if error.code == 'not_found':
                raise
            # Public page metadata remains usable if the optional API key is unavailable or over quota.

    if kind == 'video':
        page = (await _fetch('https://www.youtube.com/watch?' + urlencode({'v': target, 'hl': 'en'}))).decode()
        details = _page_json(page, 'ytInitialPlayerResponse').get('videoDetails', {})
        if details.get('videoId') != target or not YOUTUBE_CHANNEL_ID.fullmatch(details.get('channelId', '')):
            raise ChannelLookupError('not_found' if 'playabilityStatus' in page else 'lookup_unavailable')
        kind, target = 'id', details['channelId']
    path = {'id': 'channel/', 'handle': '', 'username': 'user/', 'custom': 'c/'}[kind] + quote(target, safe='@')
    page = (await _fetch(f'https://www.youtube.com/{path}/about?hl=en')).decode()
    metadata = _page_json(page, 'ytInitialData').get('metadata', {}).get('channelMetadataRenderer', {})
    channel_id = metadata.get('externalId')
    if kind == 'id' and channel_id != target:
        raise ChannelLookupError('not_found')
    thumbnails = metadata.get('avatar', {}).get('thumbnails', [])
    return _preview(channel_id, metadata.get('title'), thumbnails[0].get('url', '') if thumbnails else '')


_cache = AsyncTTLCache(max_size=256, ttl=300, name='youtube_channel_lookup')


async def resolve_channel(value: str) -> dict:
    kind, target = parse_channel_input(value)

    async def load():
        try:
            async with asyncio.timeout(12):
                return await _load(kind, target)
        except ChannelLookupError:
            raise
        except Exception:
            raise ChannelLookupError('lookup_unavailable') from None

    return dict(await _cache.get_or_load((kind, target), load))
