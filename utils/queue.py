import wavelink
import random
from typing import Optional, List, Union

class CustomQueue(wavelink.Queue):
    """
    Extended Wavelink queue with safe utilities:
    shuffle, move, remove, swap, and helpers.
    """

    def shuffle(self, *, keep_next: bool = False):
        """Shuffle the queue. Optionally keep the next track in place."""
        if len(self._queue) < 2:
            return

        if keep_next:
            first = self._queue.pop(0)
            random.shuffle(self._queue)
            self._queue.insert(0, first)
        else:
            random.shuffle(self._queue)

    def move(self, index_from: int, index_to: int):
        """
        Move a track from one index to another (0-based).
        """
        queue = self._queue
        size = len(queue)

        if not 0 <= index_from < size:
            raise IndexError("Source index out of bounds")

        if not 0 <= index_to < size:
            raise IndexError("Destination index out of bounds")

        if index_from == index_to:
            return

        track = queue.pop(index_from)

        # Adjust when moving forward
        # if index_from < index_to:
        #     index_to -= 1

        queue.insert(index_to, track)

    def remove(self, index: int):
        """Remove a track at a specific index (0-based)."""
        if not 0 <= index < len(self._queue):
            raise IndexError("Index out of bounds")
        return self._queue.pop(index)

    # def swap(self, i: int, j: int):
    #     """Swap two tracks in the queue."""
    #     if not (0 <= i < len(self._queue) and 0 <= j < len(self._queue)):
    #         raise IndexError("Index out of bounds")
    #     self._queue[i], self._queue[j] = self._queue[j], self._queue[i]

    # def move_to_next(self, index: int):
    #     """Move a track to be played next."""
    #     self.move(index, 0)

    def clear(self):
        """Clear the queue."""
        self._queue.clear()

    def to_list(self) -> List[wavelink.Playable]:
        """Return a copy of the queue."""
        return list(self._queue)


class CustomPlayer(wavelink.Player):
    """
    Custom player using CustomQueue.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if not isinstance(self.queue, CustomQueue):
            self.queue = CustomQueue()

        self.twenty_four_seven = False
        self.custom_autoplay = False
