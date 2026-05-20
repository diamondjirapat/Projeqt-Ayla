import pomice
import random
from typing import List, Optional

class CustomQueue(pomice.Queue):
    """
    Extended Pomice queue with safe utilities.

    Pomice stores the current track inside _queue for QUEUE loop mode. That
    makes the currently playing track appear as a duplicate queued item. This
    queue keeps _queue as the visible upcoming queue and stores the loop cycle
    separately.
    """

    __slots__ = ("_loop_items",)

    def __init__(self, max_size: Optional[int] = None, *, overflow: bool = True):
        super().__init__(max_size=max_size, overflow=overflow)
        self._loop_items: List[pomice.Track] = []

    def _current(self) -> Optional[pomice.Track]:
        return getattr(self, "_current_item", None)

    @staticmethod
    def _find_item_index(items: List[pomice.Track], item: pomice.Track) -> int:
        for index, candidate in enumerate(items):
            if candidate is item:
                return index
        return items.index(item)

    def _build_loop_items(self) -> None:
        current = self._current()
        items: List[pomice.Track] = []

        if current:
            items.append(current)

        for track in self._queue:
            if track is not current:
                items.append(track)

        self._loop_items = items

    def _sync_queue_from_loop(self, *, wrap: bool) -> None:
        if self._loop_mode != pomice.LoopMode.QUEUE:
            return

        if not self._loop_items:
            self._queue = []
            return

        current = self._current()
        if current is None:
            self._queue = list(self._loop_items)
            return

        try:
            index = self._find_item_index(self._loop_items, current)
        except ValueError:
            self._loop_items.insert(0, current)
            index = 0

        if wrap:
            self._queue = self._loop_items[index + 1:] + self._loop_items[:index]
        else:
            self._queue = self._loop_items[index + 1:]

    def _loop_index_from_visible_index(self, index: int) -> int:
        current = self._current()
        if current is None:
            return index

        current_index = self._find_item_index(self._loop_items, current)
        before_wrap_count = len(self._loop_items) - current_index - 1

        if index < before_wrap_count:
            return current_index + 1 + index
        return index - before_wrap_count

    def _loop_insert_index_from_visible_index(self, index: int) -> int:
        current = self._current()
        if current is None:
            return index

        current_index = self._find_item_index(self._loop_items, current)
        before_wrap_count = len(self._loop_items) - current_index - 1

        if index <= before_wrap_count:
            return current_index + 1 + index
        return index - before_wrap_count

    def _ensure_room(self) -> None:
        if not self.is_full:
            return

        if not self._overflow:
            raise pomice.QueueFull(
                f"Queue max_size of {self.max_size} has been reached.",
            )

        if self._loop_mode == pomice.LoopMode.QUEUE and self._queue:
            self.remove_at(len(self._queue) - 1)
        else:
            self._drop()

    @property
    def count(self) -> int:
        """Return the visible queued item count, excluding the current track."""
        return len(self._queue)

    @property
    def is_empty(self) -> bool:
        if self._loop_mode == pomice.LoopMode.QUEUE and self._loop_items:
            return False
        return not bool(self.count)

    def set_current(self, item: pomice.Track) -> None:
        """Update the queue's current item when playback bypasses queue.get()."""
        item = self._check_track(item)
        previous = self._current()
        self._current_item = item

        if self._loop_mode != pomice.LoopMode.QUEUE:
            return

        if not self._loop_items:
            self._build_loop_items()
        else:
            try:
                index = self._find_item_index(self._loop_items, item)
                self._loop_items[index] = item
            except ValueError:
                try:
                    previous_index = self._find_item_index(self._loop_items, previous)
                    self._loop_items[previous_index] = item
                except (TypeError, ValueError):
                    self._loop_items.insert(0, item)

        self._sync_queue_from_loop(wrap=True)

    def get(self) -> pomice.Track:
        """Return the next item while keeping queue-loop shadows out of _queue."""
        if self._loop_mode == pomice.LoopMode.TRACK:
            current = self._current()
            if current is None:
                raise pomice.QueueEmpty("No current item to loop.")
            return current

        if self._loop_mode == pomice.LoopMode.QUEUE:
            if not self._loop_items:
                self._build_loop_items()

            if not self._loop_items:
                raise pomice.QueueEmpty("No items in the queue.")

            current = self._current()
            if current is None:
                item = self._loop_items[0]
            elif len(self._loop_items) == 1:
                item = self._loop_items[0]
            else:
                try:
                    index = self._find_item_index(self._loop_items, current)
                except ValueError:
                    self._loop_items.insert(0, current)
                    index = 0
                item = self._loop_items[(index + 1) % len(self._loop_items)]

            self._current_item = item
            self._sync_queue_from_loop(wrap=True)
            return item

        return super().get()

    def put(self, item: pomice.Track) -> None:
        """Put the given item into the back of the queue."""
        item = self._check_track(item)

        if self._loop_mode != pomice.LoopMode.QUEUE:
            self._ensure_room()
            return self._put(item)

        self._ensure_room()
        if not self._loop_items:
            self._build_loop_items()
        self._loop_items.append(item)
        self._sync_queue_from_loop(wrap=True)

    def put_at_index(self, index: int, item: pomice.Track) -> None:
        """Put the given item into the visible queue at the specified index."""
        item = self._check_track(item)

        if self._loop_mode != pomice.LoopMode.QUEUE:
            self._ensure_room()
            return self._insert(index, item)

        if not 0 <= index <= len(self._queue):
            raise IndexError("Index out of bounds")

        self._ensure_room()
        if not self._loop_items:
            self._build_loop_items()

        loop_index = self._loop_insert_index_from_visible_index(index)
        self._loop_items.insert(loop_index, item)
        self._sync_queue_from_loop(wrap=True)

    def put_at_front(self, item: pomice.Track) -> None:
        """Put the given item into the front of the visible queue."""
        return self.put_at_index(0, item)

    def move(self, index_from: int, index_to: int):
        """
        Move a track from one visible queue index to another.
        """
        size = len(self._queue)

        if not 0 <= index_from < size:
            raise IndexError("Source index out of bounds")

        if not 0 <= index_to < size:
            raise IndexError("Destination index out of bounds")

        if index_from == index_to:
            return

        track = self.remove_at(index_from)
        self.put_at_index(index_to, track)

    def remove_at(self, index: int):
        """Remove a track at a specific index (0-based)."""
        if not 0 <= index < len(self._queue):
            raise IndexError("Index out of bounds")

        if self._loop_mode != pomice.LoopMode.QUEUE:
            return self._queue.pop(index)

        loop_index = self._loop_index_from_visible_index(index)
        track = self._loop_items.pop(loop_index)
        self._sync_queue_from_loop(wrap=True)
        return track

    def remove(self, item: pomice.Track) -> None:
        item = self._check_track(item)

        if self._loop_mode != pomice.LoopMode.QUEUE:
            return super().remove(item)

        index = self._find_item_index(self._loop_items, item)
        if self._loop_items[index] is self._current():
            raise ValueError("Cannot remove the currently playing track from the queue.")

        self._loop_items.pop(index)
        self._sync_queue_from_loop(wrap=True)

    def to_list(self) -> List[pomice.Track]:
        """Return a copy of the queue."""
        return list(self._queue)

    def put_at(self, index: int, item: pomice.Track):
        """Insert a track at a specific index (0-based)."""
        return self.put_at_index(index, item)

    def set_loop_mode(self, mode: pomice.LoopMode) -> None:
        """Set loop mode without injecting the current track into _queue."""
        if self._loop_mode == pomice.LoopMode.QUEUE and mode != pomice.LoopMode.QUEUE:
            self._sync_queue_from_loop(wrap=False)
            self._loop_items = []

        self._loop_mode = mode

        if mode == pomice.LoopMode.QUEUE:
            self._build_loop_items()
            self._sync_queue_from_loop(wrap=True)

    def disable_loop(self) -> None:
        if not self._loop_mode:
            raise pomice.QueueException("Queue loop is already disabled.")

        if self._loop_mode == pomice.LoopMode.QUEUE:
            self._sync_queue_from_loop(wrap=False)
            self._loop_items = []

        self._loop_mode = None

    def shuffle(self) -> None:
        if self._loop_mode != pomice.LoopMode.QUEUE:
            return random.shuffle(self._queue)

        visible_queue = list(self._queue)
        random.shuffle(visible_queue)

        current = self._current()
        self._loop_items = ([current] if current else []) + visible_queue
        self._queue = visible_queue

    def clear(self) -> None:
        self._queue.clear()

        if self._loop_mode == pomice.LoopMode.QUEUE:
            self._loop_items = []
            self._loop_mode = None

    def copy(self) -> pomice.Queue:
        new_queue = self.__class__(max_size=self.max_size, overflow=self._overflow)
        new_queue._queue = list(self._queue)
        new_queue._loop_items = list(self._loop_items)
        new_queue._loop_mode = self._loop_mode

        current = self._current()
        if current is not None:
            new_queue._current_item = current

        return new_queue


class CustomPlayer(pomice.Player):
    """
    Custom player using CustomQueue.
    """

    def __init__(self, client, channel, *, node=None):
        super().__init__(client, channel, node=node)

        self.queue = CustomQueue()
        self.twenty_four_seven = False
        self.autoplay_enabled = False
        self.history = []
        self.home_channel = None
        self.current_track_start_time = None

    async def play(
        self,
        track: pomice.Track,
        *,
        start: int = 0,
        end: int = 0,
        ignore_if_playing: bool = False,
    ) -> pomice.Track:
        played = await super().play(
            track,
            start=start,
            end=end,
            ignore_if_playing=ignore_if_playing,
        )
        self.queue.set_current(played)
        return played
