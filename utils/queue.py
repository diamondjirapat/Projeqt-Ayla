import pomice
import random
from typing import Optional, List, Union

class CustomQueue(pomice.Queue):
    """
    Extended Pomice queue with safe utilities:
    move, remove by index, and helpers.
    Note: shuffle() and clear() are inherited from pomice.Queue
    """

    def move(self, index_from: int, index_to: int):
        """
        Move a track from one index to another
        """
        size = len(self._queue)

        if not 0 <= index_from < size:
            raise IndexError("Source index out of bounds")

        if not 0 <= index_to < size:
            raise IndexError("Destination index out of bounds")

        if index_from == index_to:
            return
        
        index_from -= 1
        index_to -= 1

        track = self._queue.pop(index_from)
        self._queue.insert(index_to, track)

    def remove_at(self, index: int):
        """Remove a track at a specific index (0-based)."""
        if not 0 <= index < len(self._queue):
            raise IndexError("Index out of bounds")
        return self._queue.pop(index)

    def to_list(self) -> List[pomice.Track]:
        """Return a copy of the queue."""
        return list(self._queue)

    def put_at(self, index: int, item: pomice.Track):
        """Insert a track at a specific index (0-based)."""
        self._queue.insert(index, item)

    def set_loop_mode(self, mode: pomice.LoopMode) -> None:
        """Override set_loop_mode to fix queue retention when cycling modes."""
        if self._loop_mode == pomice.LoopMode.QUEUE and mode != pomice.LoopMode.QUEUE:
            try:
                index = self.find_position(self._current_item) + 1
                self._queue = self._queue[index:]
            except ValueError:
                pass
            except AttributeError:
                pass
        super().set_loop_mode(mode)


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
