from __future__ import annotations

import random
from enum import Enum
from typing import Any, Iterable, List, Optional
import discord
from discord.utils import MISSING
import wavelink


class LoopMode(Enum):
    OFF = 0
    NONE = 0
    TRACK = 1
    QUEUE = 2


class QueueException(Exception):
    """Base exception for queue operations."""
    pass


class QueueEmpty(QueueException):
    """Exception raised when getting an item from an empty queue."""
    pass


class QueueFull(QueueException):
    """Exception raised when putting an item into a full queue."""
    pass


class CustomQueue:
    """
    Standalone custom Queue system.
    Maintains intuitive behavior for LoopMode.OFF, LoopMode.TRACK, and LoopMode.QUEUE.
    """

    def __init__(self, max_size: Optional[int] = None, *, overflow: bool = True):
        self.max_size: Optional[int] = max_size
        self._overflow: bool = overflow
        self._queue: List[Any] = []
        self._current: Optional[Any] = None
        self._loop_mode: LoopMode = LoopMode.OFF

    @property
    def current(self) -> Optional[Any]:
        """Return the currently playing track."""
        return self._current

    @property
    def _current_item(self) -> Optional[Any]:
        """Backward compatibility property for internal access to current item."""
        return self._current

    @_current_item.setter
    def _current_item(self, item: Optional[Any]) -> None:
        self._current = item

    @property
    def loop_mode(self) -> LoopMode:
        """Return the current loop mode."""
        return self._loop_mode

    @property
    def is_looping(self) -> bool:
        """Check if any loop mode is enabled."""
        return self._loop_mode != LoopMode.OFF and self._loop_mode != LoopMode.NONE

    @property
    def count(self) -> int:
        """Return the number of upcoming items in the queue."""
        return len(self._queue)

    @property
    def is_empty(self) -> bool:
        """Check if the queue has no upcoming items."""
        if self._loop_mode == LoopMode.QUEUE:
            return len(self._queue) == 0 and self._current is None
        return len(self._queue) == 0

    @property
    def is_full(self) -> bool:
        """Check if the queue is full."""
        if self.max_size is None:
            return False
        return len(self._queue) >= self.max_size

    def _ensure_room(self) -> None:
        if not self.is_full:
            return

        if not self._overflow:
            raise QueueFull(f"Queue max_size of {self.max_size} has been reached.")

        if self._queue:
            self._queue.pop()

    def set_current(self, item: Any) -> None:
        """Set the currently playing track."""
        self._current = item

    def get(self, force_next: bool = False) -> Any:
        """
        Retrieve and return the next track in queue according to current LoopMode.
        If force_next is True (e.g. manual skip), advances to the next song even if LoopMode.TRACK is active.
        """
        if self._loop_mode == LoopMode.TRACK and not force_next:
            if self._current is not None:
                return self._current

        if self._loop_mode == LoopMode.QUEUE:
            if self._current is not None:
                self._queue.append(self._current)

        if not self._queue:
            if self._loop_mode == LoopMode.QUEUE and self._current is not None:
                return self._current
            self._current = None
            raise QueueEmpty("The queue is empty.")

        next_track = self._queue.pop(0)
        self._current = next_track
        return next_track

    def put(self, item: Any) -> None:
        """Add an item to the end of the queue."""
        self._ensure_room()
        self._queue.append(item)

    def put_at_index(self, index: int, item: Any) -> None:
        """Insert an item at a specific index in the upcoming queue."""
        if not 0 <= index <= len(self._queue):
            raise IndexError("Index out of bounds")

        self._ensure_room()
        self._queue.insert(index, item)

    def put_at(self, index: int, item: Any) -> None:
        """Insert an item at a specific index in the upcoming queue."""
        self.put_at_index(index, item)

    def put_at_front(self, item: Any) -> None:
        """Insert an item at the front of the upcoming queue (index 0)."""
        self.put_at_index(0, item)

    def extend(self, items: Iterable[Any]) -> None:
        """Add multiple items to the end of the queue."""
        for item in items:
            self.put(item)

    def move(self, index_from: int, index_to: int) -> None:
        """Move an item from index_from to index_to in the queue."""
        size = len(self._queue)

        if not 0 <= index_from < size:
            raise IndexError("Source index out of bounds")

        if not 0 <= index_to < size:
            raise IndexError("Destination index out of bounds")

        if index_from == index_to:
            return

        item = self._queue.pop(index_from)
        self._queue.insert(index_to, item)

    def remove_at(self, index: int) -> Any:
        """Remove and return the item at index in the queue."""
        if not 0 <= index < len(self._queue):
            raise IndexError("Index out of bounds")

        return self._queue.pop(index)

    def remove(self, item: Any) -> None:
        """Remove the first occurrence of item from the queue."""
        self._queue.remove(item)

    def shuffle(self) -> None:
        """Shuffle the upcoming queue in place."""
        random.shuffle(self._queue)

    def clear(self) -> None:
        """Clear all upcoming tracks from the queue."""
        self._queue.clear()

    def to_list(self) -> List[Any]:
        """Return a copy list of upcoming tracks in queue."""
        return list(self._queue)

    def set_loop_mode(self, mode: LoopMode) -> None:
        """Set the queue loop mode."""
        self._loop_mode = mode

    def disable_loop(self) -> None:
        """Disable loop mode."""
        self._loop_mode = LoopMode.OFF

    def copy(self) -> CustomQueue:
        """Return a copy of the CustomQueue."""
        new_q = CustomQueue(max_size=self.max_size, overflow=self._overflow)
        new_q._queue = list(self._queue)
        new_q._current = self._current
        new_q._loop_mode = self._loop_mode
        return new_q

    def __len__(self) -> int:
        return len(self._queue)

    def __iter__(self):
        return iter(self._queue)

    def __getitem__(self, index: int) -> Any:
        return self._queue[index]

    def __bool__(self) -> bool:
        return not self.is_empty


class CustomPlayer(wavelink.Player):
    """
    Custom player using our standalone CustomQueue with Wavelink.
    """

    def __init__(
        self,
        client: discord.Client = MISSING,
        channel: discord.abc.Connectable = MISSING,
        *,
        nodes: list[wavelink.Node] | None = None,
    ):
        if not nodes and client is not MISSING:
            client_nodes = [
                n for n in wavelink.Pool.nodes.values()
                if n.client == client and n.status == wavelink.NodeStatus.CONNECTED
            ]
            if client_nodes:
                nodes = client_nodes
        super().__init__(client, channel, nodes=nodes)

        self.queue: CustomQueue = CustomQueue()
        self.twenty_four_seven: bool = False
        self.autoplay_enabled: bool = False
        self.history: List[wavelink.Playable] = []
        self.home_channel = None
        self.current_track_start_time = None

    @property
    def is_playing(self) -> bool:
        return self.playing

    @property
    def is_paused(self) -> bool:
        return self.paused

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def set_pause(self, pause: bool) -> None:
        await self.pause(pause)

    async def destroy(self) -> None:
        await self.disconnect()

    async def get_tracks(self, query: str | dict[str, Any]) -> list[wavelink.Playable] | wavelink.Playlist:
        if isinstance(query, dict):
            query = query.get("uri") or query.get("url") or query.get("query") or (
                f"ytsearch:{query.get('title', '')} {query.get('author', '')}".strip()
                if query.get('title') or query.get('author')
                else ""
            )
        if not isinstance(query, str):
            query = str(query or "").strip()

        # Playable.search adds its own default search prefix. Preserve explicit
        # prefixes so queries such as ``ytsearch:...`` do not become
        # ``ytmsearch:ytsearch:...`` and return unrelated artwork/results.
        explicit_search_prefixes = (
            'ytsearch:',
            'ytmsearch:',
            'scsearch:',
            'spsearch:',
            'dzsearch:',
            'amsearch:',
            'bcsearch:',
        )
        if query.lower().startswith(explicit_search_prefixes):
            return await wavelink.Pool.fetch_tracks(query, node=self.node)
        return await wavelink.Playable.search(query, node=self.node)

    async def play(
        self,
        track: wavelink.Playable,
        *,
        replace: bool = True,
        start: int = 0,
        end: Optional[int] = None,
        volume: Optional[int] = None,
        paused: Optional[bool] = None,
        add_history: bool = False,
        filters: Optional[wavelink.Filters] = None,
        populate: bool = False,
        max_populate: int = 5,
        **kwargs: Any,
    ) -> wavelink.Playable:
        custom_art = getattr(track, 'custom_artwork', None)
        requester = getattr(track, 'requester', None)
        played = await super().play(
            track,
            replace=replace,
            start=start,
            end=end,
            volume=volume,
            paused=paused,
            add_history=False,
            filters=filters,
            populate=populate,
            max_populate=max_populate,
        )
        if custom_art:
            played.custom_artwork = custom_art
        if requester:
            played.requester = requester
        self.queue.set_current(played)
        return played
