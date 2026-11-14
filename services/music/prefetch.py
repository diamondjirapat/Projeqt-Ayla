"""Music playback prefetch behavior, composed by the Discord music cog."""

import asyncio
import logging
from typing import Optional

from utils.queue import CustomPlayer, LoopMode
from services.music.gapless import sync_gapless_queue
from utils.autoplay import is_channel_blocked

logger = logging.getLogger(__name__)
AUTOPLAY_PREFETCH_SECONDS = 10


class PlaybackPrefetch:
    async def _drop_stale_gapless_preload(self, player: CustomPlayer) -> None:
        """Discard a staged gapless next-track that is no longer queued.

        lava-lyra keeps the preloaded track in player._next_track and NodeLink
        keeps its stream staged; without this the orphaned song would still
        play at the next transition (e.g. after the queue was cleared).
        """
        try:
            staged = getattr(player, '_next_track', None)
            if staged is None:
                return
            if any(staged is queued for queued in player.queue.to_list()):
                return
            player._next_track = None
            try:
                # Clears only the staged next track server-side; the current
                # song keeps playing.
                await player.stop(gapless=True)
            except Exception:
                pass
        except Exception:
            pass

    def _autoplay_prefetch_guild_id(self, player) -> Optional[int]:
        try:
            guild_id = player.guild.id
        except Exception:
            return None
        return guild_id if isinstance(guild_id, int) else None

    def _cancel_autoplay_prefetch_task(self, guild_id: int) -> None:
        task = self._autoplay_prefetch_tasks.pop(guild_id, None)
        if isinstance(task, (asyncio.Task, asyncio.Future)):
            try:
                if not task.done():
                    task.cancel()
            except Exception:
                pass

    def _clear_autoplay_prefetch(self, guild_id: int) -> None:
        try:
            self._autoplay_prefetch_cache.pop(guild_id, None)
        except Exception:
            pass
        self._cancel_autoplay_prefetch_task(guild_id)

    def _take_autoplay_prefetch(self, player, track):
        """Consume a prefetched autoplay track prepared for the ended track."""
        try:
            guild_id = self._autoplay_prefetch_guild_id(player)
            if guild_id is None:
                return None
            entry = self._autoplay_prefetch_cache.pop(guild_id, None)
            if not isinstance(entry, dict):
                return None
            for_track = entry.get('for_track')
            prefetched = entry.get('track')
            if prefetched is None or not getattr(prefetched, 'title', None):
                return None
            if is_channel_blocked(prefetched, getattr(player, 'autoplay_config', None)):
                return None
            if for_track is track:
                return prefetched
            try:
                for_uri = getattr(for_track, 'uri', None)
                track_uri = getattr(track, 'uri', None)
                if for_uri and track_uri and for_uri == track_uri:
                    return prefetched
            except Exception:
                pass
            return None
        except Exception:
            return None

    async def _await_pending_autoplay_prefetch(self, player, track):
        """Wait briefly for an in-flight prefetch, then consume it when ready."""
        try:
            guild_id = self._autoplay_prefetch_guild_id(player)
            if guild_id is None:
                return None
            task = self._autoplay_prefetch_tasks.get(guild_id)
            if not isinstance(task, (asyncio.Task, asyncio.Future)):
                return None
            if task.done():
                return self._take_autoplay_prefetch(player, track)
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=8)
            except asyncio.CancelledError:
                # Disabling autoplay cancels its worker, not the track-end handler.
                if asyncio.current_task().cancelling():
                    raise
                return None
            except Exception:
                pass
            return self._take_autoplay_prefetch(player, track)
        except Exception:
            return None

    def _schedule_autoplay_prefetch(self, player, track) -> None:
        """Fetch the next autoplay track ~AUTOPLAY_PREFETCH_SECONDS before this one ends."""
        try:
            guild_id = self._autoplay_prefetch_guild_id(player)
            if guild_id is None:
                return
            self._cancel_autoplay_prefetch_task(guild_id)
            self._autoplay_prefetch_cache.pop(guild_id, None)
            if getattr(player, 'autoplay_enabled', False) is not True:
                return
            queue = getattr(player, 'queue', None)
            if queue is None:
                return
            try:
                loop_mode = queue.loop_mode
            except Exception:
                return
            if not isinstance(loop_mode, LoopMode):
                return
            if loop_mode == LoopMode.TRACK:
                return
            try:
                is_empty = queue.is_empty
            except Exception:
                return
            if is_empty is not True:
                return
            length = getattr(track, 'length', None)
            if isinstance(length, bool) or not isinstance(length, (int, float)) or length <= 0:
                return
            delay = float(length) / 1000.0 - AUTOPLAY_PREFETCH_SECONDS
            if delay < 0:
                delay = 0
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            worker = self._autoplay_prefetch_worker(player, track, delay, guild_id)
            self._autoplay_prefetch_tasks[guild_id] = loop.create_task(worker)
        except Exception:
            pass

    async def _autoplay_prefetch_worker(self, player, track, delay: float, guild_id: int) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        try:
            current = getattr(player, 'current', None)
            if current is not None and current is not track:
                try:
                    if getattr(current, 'uri', None) != getattr(track, 'uri', None):
                        return
                except Exception:
                    return
            if not getattr(player, 'autoplay_enabled', False):
                return
            queue = getattr(player, 'queue', None)
            if queue is not None:
                try:
                    if queue.loop_mode == LoopMode.TRACK:
                        return
                except Exception:
                    pass
                try:
                    if not queue.is_empty:
                        return
                except Exception:
                    pass
            if self._autoplay_prefetch_cache.get(guild_id) is not None:
                return
            recommended = await self._fetch_autoplay_track(player, track)
            if recommended is None:
                return
            current_after = getattr(player, 'current', None)
            if current_after is not None and current_after is not track:
                try:
                    if getattr(current_after, 'uri', None) != getattr(track, 'uri', None):
                        return
                except Exception:
                    return
            try:
                if queue is not None and not queue.is_empty:
                    return
            except Exception:
                pass
            if not getattr(player, 'autoplay_enabled', False):
                return
            self._autoplay_prefetch_cache[guild_id] = {'for_track': track, 'track': recommended}
            logger.info(f"[AUTOPLAY PREFETCH] Cached '{recommended.title}' for guild {guild_id}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f'[AUTOPLAY PREFETCH] Prefetch failed: {e}')
        finally:
            try:
                current_task = asyncio.current_task()
                if self._autoplay_prefetch_tasks.get(guild_id) is current_task:
                    self._autoplay_prefetch_tasks.pop(guild_id, None)
            except Exception:
                pass

    def _maybe_prefetch_after_seek(self, player, position_ms) -> None:
        """Start an immediate prefetch when a seek lands inside the prefetch window."""
        try:
            guild_id = self._autoplay_prefetch_guild_id(player)
            if guild_id is None:
                return
            if getattr(player, 'autoplay_enabled', False) is not True:
                return
            if self._autoplay_prefetch_cache.get(guild_id) is not None:
                return
            pending = self._autoplay_prefetch_tasks.get(guild_id)
            if isinstance(pending, (asyncio.Task, asyncio.Future)) and not pending.done():
                return
            queue = getattr(player, 'queue', None)
            if queue is None:
                return
            try:
                if queue.loop_mode == LoopMode.TRACK:
                    return
                if not queue.is_empty:
                    return
            except Exception:
                return
            current = getattr(player, 'current', None)
            if current is None:
                return
            length = getattr(current, 'length', None)
            if isinstance(length, bool) or not isinstance(length, (int, float)) or length <= 0:
                return
            try:
                position = float(position_ms)
            except (TypeError, ValueError):
                return
            remaining_ms = float(length) - position
            if remaining_ms <= 0 or remaining_ms > (AUTOPLAY_PREFETCH_SECONDS + 2) * 1000:
                return
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            worker = self._autoplay_prefetch_worker(player, current, 0, guild_id)
            self._autoplay_prefetch_tasks[guild_id] = loop.create_task(worker)
        except Exception:
            pass

    async def _preload_gapless_next(self, player: CustomPlayer) -> None:
        """Stage the next queued track for a seamless gapless handoff.

        Peeks at the queue head WITHOUT popping: when the current song ends,
        NodeLink swaps to the staged track itself and reports a GAPLESS end,
        which on_lyra_track_end turns into a queue pop. Any failure is
        swallowed so playback always falls back to a normal advance on the
        next FINISHED event.
        """
        await sync_gapless_queue(player)
