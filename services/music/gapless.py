"""Keep the audio server's staged track aligned with the live queue."""

import asyncio
import logging

from utils.queue import LoopMode

logger = logging.getLogger(__name__)


async def sync_gapless_queue(player) -> None:
    """Refresh the preload without consuming tracks or interrupting playback.

    Queue edits and track-start notifications can overlap while node requests
    are pending. Serialize those requests and re-read the queue after each await.
    """
    lock = getattr(player, '_gapless_sync_lock', None)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        player._gapless_sync_lock = lock
    async with lock:
        try:
            while True:
                upcoming = player.queue.to_list()
                head = (
                    upcoming[0]
                    if upcoming
                    and player.current is not None
                    and getattr(player, 'gapless_enabled', False) is True
                    and player.queue.loop_mode != LoopMode.TRACK
                    else None
                )
                staged = getattr(player, '_next_track', None)
                if staged is head:
                    return
                if staged is not None:
                    await player.stop(gapless=True)
                    if getattr(player, '_next_track', None) is staged:
                        return
                    # Playback or the queue may have changed during the request.
                    continue
                if head is None:
                    return
                await player.play(head, gapless=True)
                if getattr(player, '_next_track', None) is not head:
                    return
        except Exception as exc:
            logger.warning('[GAPLESS] Could not synchronize the queue preload: %s', exc)
