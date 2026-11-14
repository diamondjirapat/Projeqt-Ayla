"""Small bounded caches used by hot message-processing paths."""

from collections import OrderedDict
import asyncio
import time
import weakref
from typing import Awaitable, Callable, Generic, TypeVar

KeyT = TypeVar('KeyT')
ValueT = TypeVar('ValueT')

CACHE_MISS = object()
_caches = weakref.WeakSet()


def cache_metrics() -> list[dict]:
    """Process-local snapshots, grouped by static names; never include keys or values."""
    groups = {}
    for cache in tuple(_caches):
        info = cache.cache_info()
        name = info.pop('name')
        group = groups.setdefault(name, {'name': name, 'instances': 0})
        group['instances'] += 1
        for key, value in info.items():
            group[key] = group.get(key, 0) + value
    for group in groups.values():
        requests = group['hits'] + group['misses']
        group['hit_rate'] = group['hits'] / requests if requests else 0.0
        group['average_load_seconds'] = group['load_seconds'] / group['loads'] if group['loads'] else 0.0
    return [groups[name] for name in sorted(groups)]


class TTLCache(Generic[KeyT, ValueT]):
    """A bounded least-recently-used cache with per-entry expiration."""

    def __init__(
        self,
        *,
        ttl: float,
        max_size: int,
        clock: Callable[[], float] = time.monotonic,
        name: str = 'cache',
    ) -> None:
        if ttl <= 0:
            raise ValueError('ttl must be positive')
        if max_size <= 0:
            raise ValueError('max_size must be positive')

        self._ttl = ttl
        self._max_size = max_size
        self._clock = clock
        self.name = name
        self._stats = dict.fromkeys(
            ('hits', 'misses', 'evictions', 'expirations', 'loads', 'load_errors', 'coalesced'), 0
        )
        self._stats['load_seconds'] = 0.0
        self._values: OrderedDict[KeyT, tuple[ValueT, float]] = OrderedDict()
        _caches.add(self)

    def cache_info(self) -> dict:
        self.prune()
        return {
            'name': self.name,
            **self._stats,
            'size': len(self),
            'max_size': self._max_size,
            'pending': len(getattr(self, '_pending', {})),
        }

    def get(self, key: KeyT):
        try:
            value, expiry = self._values.pop(key)
        except KeyError:
            self._stats['misses'] += 1
            return CACHE_MISS

        if self._clock() >= expiry:
            self._stats['misses'] += 1
            self._stats['expirations'] += 1
            return CACHE_MISS

        self._values[key] = (value, expiry)
        self._stats['hits'] += 1
        return value

    def set(self, key: KeyT, value: ValueT, *, ttl: float | None = None) -> None:
        entry_ttl = self._ttl if ttl is None else ttl
        if entry_ttl <= 0:
            raise ValueError('ttl must be positive')
        self._values.pop(key, None)
        if len(self._values) >= self._max_size:
            self.prune()
        while len(self._values) >= self._max_size:
            self._values.popitem(last=False)
            self._stats['evictions'] += 1
        self._values[key] = (value, self._clock() + entry_ttl)

    def pop(self, key: KeyT) -> None:
        self._values.pop(key, None)

    def clear(self) -> None:
        self._values.clear()

    def prune(self) -> None:
        now = self._clock()
        for key, (_, expiry) in list(self._values.items()):
            if expiry <= now:
                self._values.pop(key, None)
                self._stats['expirations'] += 1

    def __contains__(self, key: KeyT) -> bool:
        return self.get(key) is not CACHE_MISS

    def __getitem__(self, key: KeyT) -> ValueT:
        value = self.get(key)
        if value is CACHE_MISS:
            raise KeyError(key)
        return value

    def __len__(self) -> int:
        return len(self._values)


class AsyncTTLCache(TTLCache[KeyT, ValueT]):
    """Share concurrent loads; writes detach older loads without cancelling their callers.

    Intended for one asyncio event loop. Pending entries live only for the duration
    of their loads, and cancelled waiters cannot cancel a load shared by others.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._pending: dict[KeyT, asyncio.Task[ValueT]] = {}

    async def get_or_load(
        self,
        key: KeyT,
        loader: Callable[[], Awaitable[ValueT]],
        *,
        ttl: Callable[[ValueT], float] | None = None,
    ) -> ValueT:
        cached = self.get(key)
        if cached is not CACHE_MISS:
            return cached
        task = self._pending.get(key)
        if task is None:

            async def load():
                started = self._clock()
                self._stats['loads'] += 1
                try:
                    value = await loader()
                    if self._pending.get(key) is asyncio.current_task():
                        super(AsyncTTLCache, self).set(key, value, ttl=ttl(value) if ttl else None)
                    return value
                except BaseException:
                    self._stats['load_errors'] += 1
                    raise
                finally:
                    self._stats['load_seconds'] += max(0.0, self._clock() - started)

            task = asyncio.create_task(load())
            self._pending[key] = task

            def finished(completed):
                if self._pending.get(key) is completed:
                    self._pending.pop(key, None)
                # Retrieve exceptions even when all waiters have been cancelled.
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(finished)
        else:
            self._stats['coalesced'] += 1
        return await asyncio.shield(task)

    def set(self, key: KeyT, value: ValueT, *, ttl: float | None = None) -> None:
        super().set(key, value, ttl=ttl)
        self._pending.pop(key, None)

    def pop(self, key: KeyT) -> None:
        super().pop(key)
        self._pending.pop(key, None)

    def clear(self) -> None:
        super().clear()
        self._pending.clear()
