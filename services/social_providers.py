"""Official social APIs. Endpoints are fixed; provider error bodies and credentials never escape this module."""

import asyncio
import copy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
import re
import time
from urllib.parse import quote, urlencode, urlsplit

import aiohttp

from config import Config
from utils.cache import CACHE_MISS, AsyncTTLCache, TTLCache
from utils.http import safe_http_request


class ProviderError(RuntimeError):
    def __init__(self, code: str, retry_after: int = 300):
        self.code = code
        self.retry_after = min(86400, max(60, retry_after))
        super().__init__(code)


@dataclass(frozen=True)
class SocialEvent:
    id: str
    kind: str
    url: str
    title: str = ''
    creator: str = ''
    text: str = ''
    game: str = ''
    published_at: str = ''
    image: str = ''

    @property
    def key(self) -> str:
        return f'{self.kind}:{self.id}'

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PollResult:
    events: list[SocialEvent]
    metadata: dict = field(default_factory=dict)
    warning: str = ''


@dataclass(frozen=True)
class _PollFailure:
    # Cache safe error data without retaining exception tracebacks and request frames.
    code: str
    retry_after: int


def account_handle(platform: str, value: str) -> str:
    """Accept explicit handles/IDs, never interpret a user value as a request URL."""
    handle = str(value or '').strip().lstrip('@')
    pattern = r'[\w.\-]{1,100}' if platform == 'youtube' else r'[A-Za-z0-9_.]{1,64}'
    if not re.fullmatch(pattern, handle):
        raise ProviderError('invalid_handle', 3600)
    return handle if platform == 'youtube' and handle.startswith('UC') else handle.casefold()


def provider_capabilities(config=Config) -> dict:
    return {
        'twitch': {'configured': bool(config.TWITCH_CLIENT_ID and config.TWITCH_CLIENT_SECRET), 'events': ['live']},
        'youtube': {'configured': bool(config.YOUTUBE_API_KEY), 'events': ['video', 'live']},
        'x': {'configured': bool(config.X_BEARER_TOKEN), 'events': ['post']},
    }


def setup_problem(entry: dict, config=Config) -> str:
    if not config.SOCIAL_ALERTS_ENABLED:
        return 'worker_disabled'
    try:
        account_handle(entry['platform'], entry.get('handle', ''))
    except ProviderError as error:
        return error.code
    capability = provider_capabilities(config)[entry['platform']]
    if not any(event in capability['events'] for event in entry['events']):
        return 'unsupported_event'
    if not capability['configured']:
        return 'missing_credentials'
    if not entry.get('channelId'):
        return 'missing_channel'
    return ''


def timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)


class SocialProviders:
    def __init__(self, config=Config, request=safe_http_request, *, cache_max_size=2000, clock=time.monotonic):
        self.config = config
        self.request = request
        self._twitch_token = ''
        self._twitch_token_until = 0.0
        self._twitch_lock = asyncio.Lock()
        self._cache = AsyncTTLCache(ttl=60, max_size=cache_max_size, clock=clock, name='social.polls')
        self._metadata = TTLCache(ttl=86400, max_size=cache_max_size, clock=clock, name='social.metadata')

    def interval(self, platform: str) -> int:
        return getattr(self.config, f'SOCIAL_{platform.upper()}_POLL_SECONDS')

    async def _json(self, method: str, base: str, *, params=None, headers=None, body=None, form=None) -> dict:
        url = base + ('?' + urlencode(params) if params else '')
        request_headers = dict(headers or {})
        if form is not None:
            request_headers['Content-Type'] = 'application/x-www-form-urlencoded'
        try:
            status, response_headers, payload, _ = await self.request(
                method,
                url,
                allowed_domains=[urlsplit(base).hostname],
                headers=request_headers,
                json_body=body,
                body=urlencode(form).encode() if form is not None else None,
                timeout_seconds=10,
            )
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as error:
            raise ProviderError('provider_unavailable') from error
        if status == 429:
            lower_headers = {key.lower(): value for key, value in response_headers.items()}
            delay = 900
            try:
                delay = int(lower_headers.get('retry-after', 0)) or max(
                    60,
                    int(lower_headers.get('x-rate-limit-reset') or lower_headers.get('ratelimit-reset', 0))
                    - int(time.time()),
                )
            except (ValueError, TypeError):
                pass
            raise ProviderError('rate_limited', delay)
        if status in {401, 403}:
            raise ProviderError('provider_auth_or_quota', 3600)
        if status == 404:
            raise ProviderError('account_not_found', 3600)
        if status != 200:
            raise ProviderError('provider_unavailable')
        try:
            result = json.loads(payload)
            if not isinstance(result, dict):
                raise ValueError
        except (ValueError, TypeError):
            raise ProviderError('invalid_provider_response') from None
        return result

    async def poll(self, platform: str, handle: str, metadata: dict | None = None, *, live: bool = True) -> PollResult:
        handle = account_handle(platform, handle)
        key = f'{platform}:{handle}:{live}'

        async def load():
            state = self._metadata.get(key)
            state = copy.deepcopy(metadata or {} if state is CACHE_MISS else state)
            try:
                result = await getattr(self, f'_{platform}')(handle, state, live)
            except ProviderError as error:
                return _PollFailure(error.code, error.retry_after)
            except (KeyError, TypeError, ValueError, IndexError):
                return _PollFailure('invalid_provider_response', 300)
            self._metadata.set(key, copy.deepcopy(result.metadata))
            return result

        result = await self._cache.get_or_load(
            key,
            load,
            ttl=lambda value: value.retry_after if isinstance(value, _PollFailure) else self.interval(platform),
        )
        if isinstance(result, _PollFailure):
            raise ProviderError(result.code, result.retry_after)
        return copy.deepcopy(result)

    async def _twitch(self, handle: str, metadata: dict, live: bool) -> PollResult:
        async with self._twitch_lock:
            if time.monotonic() >= self._twitch_token_until:
                token = await self._json(
                    'POST',
                    'https://id.twitch.tv/oauth2/token',
                    form={
                        'client_id': self.config.TWITCH_CLIENT_ID,
                        'client_secret': self.config.TWITCH_CLIENT_SECRET,
                        'grant_type': 'client_credentials',
                    },
                )
                self._twitch_token = token.get('access_token', '')
                if not self._twitch_token:
                    raise ProviderError('provider_auth_or_quota', 3600)
                self._twitch_token_until = time.monotonic() + max(1, int(token.get('expires_in', 3600)) - 60)
        try:
            data = await self._json(
                'GET',
                'https://api.twitch.tv/helix/streams',
                params={'user_login': handle},
                headers={'Client-Id': self.config.TWITCH_CLIENT_ID, 'Authorization': f'Bearer {self._twitch_token}'},
            )
        except ProviderError as error:
            if error.code == 'provider_auth_or_quota':
                self._twitch_token_until = 0
            raise
        events = [
            SocialEvent(
                str(item['id']),
                'live',
                f'https://www.twitch.tv/{handle}',
                title=item.get('title', ''),
                creator=item.get('user_name', handle),
                game=item.get('game_name', ''),
                published_at=item.get('started_at', ''),
                image=item.get('thumbnail_url', '').replace('{width}', '640').replace('{height}', '360'),
            )
            for item in data.get('data', [])
        ]
        return PollResult(events)

    async def _youtube(self, handle: str, metadata: dict, live: bool) -> PollResult:
        state = dict(metadata)

        async def youtube(resource: str, **params):
            return await self._json(
                'GET',
                f'https://www.googleapis.com/youtube/v3/{resource}',
                params={'key': self.config.YOUTUBE_API_KEY, **params},
            )

        if not state.get('uploads'):
            result = await youtube(
                'channels',
                part='snippet,contentDetails',
                **({'id': handle} if handle.startswith('UC') else {'forHandle': handle}),
            )
            if not result.get('items'):
                raise ProviderError('account_not_found', 3600)
            channel = result['items'][0]
            state.update(
                channel_id=channel['id'],
                creator=channel['snippet']['title'],
                uploads=channel['contentDetails']['relatedPlaylists']['uploads'],
            )
        uploads = await youtube('playlistItems', part='contentDetails', playlistId=state['uploads'], maxResults=50)
        video_ids = [item['contentDetails']['videoId'] for item in uploads.get('items', [])]
        warning = ''
        discovery_at = timestamp(state.get('live_discovered_at', ''))
        if (
            live
            and (datetime.now(UTC) - discovery_at).total_seconds() >= self.config.SOCIAL_YOUTUBE_LIVE_DISCOVERY_SECONDS
        ):
            try:
                streams = await youtube(
                    'search', part='id', channelId=state['channel_id'], type='video', eventType='live', maxResults=50
                )
                state['live_ids'] = [item['id']['videoId'] for item in streams.get('items', [])]
                state['live_discovered_at'] = datetime.now(UTC).isoformat()
            except ProviderError as error:
                # Upload alerts remain operational when only live discovery hits a quota.
                warning = error.code
                state['live_discovered_at'] = datetime.now(UTC).isoformat()
        video_ids = list(dict.fromkeys(video_ids + (state.get('live_ids', []) if live else [])))[:100]
        events = []
        remaining_live = []
        for offset in range(0, len(video_ids), 50):
            details = await youtube(
                'videos', part='snippet,liveStreamingDetails', id=','.join(video_ids[offset : offset + 50])
            )
            for item in details.get('items', []):
                snippet = item['snippet']
                streaming = item.get('liveStreamingDetails', {})
                is_live = snippet.get('liveBroadcastContent') == 'live' and not streaming.get('actualEndTime')
                if snippet.get('liveBroadcastContent') == 'upcoming':
                    remaining_live.append(item['id'])
                    continue
                if is_live:
                    remaining_live.append(item['id'])
                # Archived streams are not new upload events.
                if not is_live and streaming:
                    continue
                thumbnails = snippet.get('thumbnails', {})
                events.append(
                    SocialEvent(
                        item['id'],
                        'live' if is_live else 'video',
                        f'https://www.youtube.com/watch?v={item["id"]}',
                        title=snippet['title'],
                        creator=snippet.get('channelTitle', state['creator']),
                        published_at=streaming.get('actualStartTime', snippet.get('publishedAt', '')),
                        image=(thumbnails.get('high') or thumbnails.get('default') or {}).get('url', ''),
                    )
                )
        state['live_ids'] = remaining_live
        return PollResult(events, state, warning)

    async def _x(self, handle: str, metadata: dict, live: bool) -> PollResult:
        state = dict(metadata)
        headers = {'Authorization': f'Bearer {self.config.X_BEARER_TOKEN}'}
        if not state.get('user_id'):
            result = await self._json(
                'GET', f'https://api.x.com/2/users/by/username/{quote(handle, safe="")}', headers=headers
            )
            if not result.get('data'):
                raise ProviderError('account_not_found', 3600)
            state.update(user_id=result['data']['id'], creator=result['data'].get('name', handle))
        result = await self._json(
            'GET',
            f'https://api.x.com/2/users/{state["user_id"]}/tweets',
            headers=headers,
            params={'max_results': 100, 'tweet.fields': 'created_at', 'exclude': 'retweets,replies'},
        )
        if result.get('errors'):
            raise ProviderError('provider_auth_or_quota', 3600)
        events = [
            SocialEvent(
                item['id'],
                'post',
                f'https://x.com/{handle}/status/{item["id"]}',
                title=f'New post from @{handle}',
                text=item.get('text', ''),
                creator=state['creator'],
                published_at=item.get('created_at', ''),
            )
            for item in result.get('data', [])
        ]
        return PollResult(events, state)
