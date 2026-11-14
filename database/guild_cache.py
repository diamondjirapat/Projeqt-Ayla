"""Opt-in process-local TTL cache for guild documents.

Every small ``GuildModel`` getter (volume, channels, toggles, embed config)
currently issues its own ``find_one`` against the ``guilds`` collection, so a
single command fans out into several database round trips. ``GuildDocCache``
can collapse those into one cached document per guild when TTL consistency is accepted, following the same
pattern as :class:`utils.prefix_manager.PrefixManager`: cache-through reads
(including caching ``None`` for unknown guilds), invalidation on writes, and
TTL expiry as the safety net for out-of-band edits. GuildModel defaults to fresh
database reads for consistency across processes and external writers.
"""

import copy
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import time
import weakref
from typing import Any, Awaitable, Callable, Dict, Optional

from utils.cache import CACHE_MISS, AsyncTTLCache

DEFAULT_GUILD_CACHE_TTL = 60.0
DEFAULT_GUILD_CACHE_MAX_SIZE = 2000
_guild_caches = weakref.WeakSet()
_read_scope = ContextVar('guild_read_scope', default=None)


@contextmanager
def guild_read_scope(*, clock=time.monotonic):
    """Share fresh reads between listeners for one event, never between events."""
    # Delayed background tasks inherit context too; they must not retain an
    # event's snapshot indefinitely.
    token = _read_scope.set((clock() + 1, {}, clock))
    try:
        yield
    finally:
        _read_scope.reset(token)


async def scoped_guild_read(collection, guild_id, loader):
    scope = _read_scope.get()
    if scope is None or scope[2]() >= scope[0]:
        return await loader()
    scope = scope[1]
    namespace = (id(collection.database), collection.name) if hasattr(collection, 'database') else id(collection)
    key = (namespace, normalize_guild_key(guild_id))
    task = scope.get(key)
    if task is None:
        task = asyncio.create_task(loader())
        scope[key] = task
        task.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
    try:
        return copy.deepcopy(await asyncio.shield(task))
    except Exception:
        if scope.get(key) is task:
            scope.pop(key, None)
        raise


def invalidate_guild_caches(guild_id: Any) -> None:
    """Invalidate every live repository's copy after a guild write."""
    for cache in tuple(_guild_caches):
        cache.invalidate(guild_id)
    scope = _read_scope.get()
    if scope is not None:
        scope = scope[1]
        for key in tuple(scope):
            if key[1] == normalize_guild_key(guild_id):
                scope.pop(key, None)


def clear_guild_caches() -> None:
    """Drop documents when the database connection changes or shuts down."""
    for cache in tuple(_guild_caches):
        cache.clear()


def normalize_guild_key(guild_id: Any) -> Any:
    """Normalize a guild id for use as a cache key.

    Discord ids arrive as ints, but callers occasionally pass through raw
    values, so coerce integer-like ids to ``int`` and leave anything else
    untouched.
    """
    try:
        return int(guild_id)
    except (TypeError, ValueError):
        return guild_id


class GuildDocCache:
    """Bounded TTL cache for raw guild documents keyed by guild id."""

    def __init__(
        self,
        *,
        ttl: float = DEFAULT_GUILD_CACHE_TTL,
        max_size: int = DEFAULT_GUILD_CACHE_MAX_SIZE,
        clock: Callable[[], float] = time.monotonic,
        name: str = 'guild.documents',
    ) -> None:
        if ttl <= 0:
            raise ValueError('ttl must be positive')
        if max_size <= 0:
            raise ValueError('max_size must be positive')
        self._cache: AsyncTTLCache[Any, Optional[Dict[str, Any]]] = AsyncTTLCache(
            ttl=ttl, max_size=max_size, clock=clock, name=name
        )
        self.revision = 0
        _guild_caches.add(self)

    @property
    def hits(self):
        return self._cache.cache_info()['hits']

    @property
    def misses(self):
        return self._cache.cache_info()['misses']

    def get(self, guild_id: Any) -> Any:
        """Return a private copy of the cached document, or ``CACHE_MISS``.

        Copies are returned so callers can freely mutate the result without
        poisoning the cached entry.
        """
        cached = self._cache.get(normalize_guild_key(guild_id))
        if cached is CACHE_MISS:
            return CACHE_MISS
        return copy.deepcopy(cached)

    async def get_or_load(self, guild_id: Any, loader: Callable[[], Awaitable[Any]]) -> Any:
        async def load():
            return copy.deepcopy(await loader())

        doc = await self._cache.get_or_load(normalize_guild_key(guild_id), load)
        return copy.deepcopy(doc)

    def set(self, guild_id: Any, doc: Optional[Dict[str, Any]]) -> None:
        """Store a private copy of a guild document (``None`` included)."""
        self._cache.set(normalize_guild_key(guild_id), copy.deepcopy(doc))

    def invalidate(self, guild_id: Any) -> None:
        self.revision += 1
        self._cache.pop(normalize_guild_key(guild_id))

    def clear(self) -> None:
        self.revision += 1
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)
