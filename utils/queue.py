import wavelink
import random
from typing import Optional, List, Union

class CustomQueue(wavelink.Queue):
    """
    Extended Wavelink queue with safe utilities:
    move, remove by index, and helpers.
    Note: shuffle() and clear() are inherited from wavelink.Queue
    """

    def move(self, index_from: int, index_to: int):
        """
        Move a track from one index to another (0-based).
        """
        size = len(self._items)

        if not 0 <= index_from < size:
            raise IndexError("Source index out of bounds")

        if not 0 <= index_to < size:
            raise IndexError("Destination index out of bounds")

        if index_from == index_to:
            return

        track = self._items.pop(index_from)
        self._items.insert(index_to, track)

    def remove_at(self, index: int):
        """Remove a track at a specific index (0-based)."""
        if not 0 <= index < len(self._items):
            raise IndexError("Index out of bounds")
        return self._items.pop(index)

    def to_list(self) -> List[wavelink.Playable]:
        """Return a copy of the queue."""
        return list(self._items)


class CustomPlayer(wavelink.Player):
    """
    Custom player using CustomQueue.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if not isinstance(self.queue, CustomQueue):
            self.queue = CustomQueue()

        self.twenty_four_seven = False

