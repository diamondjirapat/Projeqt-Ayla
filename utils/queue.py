import wavelink
import random
from typing import Optional, List, Union

class CustomQueue(wavelink.Queue):
    """
    A custom queue class that extends wavelink.Queue to add
    more advanced management features like shuffle, move, and remove.
    """
    def __init__(self):
        super().__init__()

    def shuffle(self):
        """Shuffles the queue in place."""
        random.shuffle(self._queue)

    def move(self, index_from: int, index_to: int):
        """
        Moves a track from one position to another.
        Indices are 0-based.
        """
        if index_from < 0 or index_from >= len(self):
            raise IndexError("Source index out of bounds")
        
        # When moving, if we delete source first, indices shift.
        # But we want 'move x to y' to mean the item at x ends up at y.
        
        item = self._queue[index_from]
        del self._queue[index_from]
        self._queue.insert(index_to, item)

    def remove(self, index: int):
        """Removes a track at a specific index (0-based)."""
        if index < 0 or index >= len(self):
            raise IndexError("Index out of bounds")
        del self._queue[index]

    def clear(self):
        """Clears the queue."""
        self._queue.clear()
        
    def to_list(self) -> list:
        """Returns a copy of the queue as a list."""
        return list(self._queue)

class CustomPlayer(wavelink.Player):
    """
    A custom player class that uses CustomQueue.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue = CustomQueue()
        self.twenty_four_seven = False
        self.autoplay = False
