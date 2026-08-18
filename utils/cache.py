"""Small bounded caches used by hot message-processing paths."""

from collections import OrderedDict
import time
from typing import Callable, Generic, TypeVar

KeyT = TypeVar('KeyT')
ValueT = TypeVar('ValueT')

CACHE_MISS = object()


class TTLCache(Generic[KeyT, ValueT]):
    """A bounded least-recently-used cache with per-entry expiration."""

    def __init__(
        self,
        *,
        ttl: float,
        max_size: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        if max_size <= 0:
            raise ValueError("max_size must be positive")

        self._ttl = ttl
        self._max_size = max_size
        self._clock = clock
        self._values: OrderedDict[KeyT, tuple[ValueT, float]] = OrderedDict()

    def get(self, key: KeyT):
        try:
            value, expiry = self._values.pop(key)
        except KeyError:
            return CACHE_MISS

        if self._clock() >= expiry:
            return CACHE_MISS

        self._values[key] = (value, expiry)
        return value

    def set(self, key: KeyT, value: ValueT, *, ttl: float | None = None) -> None:
        entry_ttl = self._ttl if ttl is None else ttl
        if entry_ttl <= 0:
            raise ValueError("ttl must be positive")
        self._values.pop(key, None)
        while len(self._values) >= self._max_size:
            self._values.popitem(last=False)
        self._values[key] = (value, self._clock() + entry_ttl)

    def pop(self, key: KeyT) -> None:
        self._values.pop(key, None)

    def clear(self) -> None:
        self._values.clear()

    def __len__(self) -> int:
        return len(self._values)
