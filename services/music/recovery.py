"""Music playback recovery behavior, composed by the Discord music cog."""

import logging

import lava_lyra

from utils.i18n import i18n
from utils.queue import CustomPlayer, LoopMode

logger = logging.getLogger(__name__)
MAX_CONSECUTIVE_PLAYBACK_FAILURES = 5


class PlaybackRecovery:
    def _get_playback_failure_count(self, player) -> int:
        """Return the current consecutive playback-failure streak for a player."""
        try:
            return int(getattr(player, 'consecutive_playback_failures', 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _reset_playback_failures(self, player) -> None:
        """Clear the consecutive-failure streak after a track plays through."""
        for attr, value in (('consecutive_playback_failures', 0), ('_counted_failure_track', None)):
            try:
                setattr(player, attr, value)
            except Exception:
                pass

    def _register_playback_failure(self, player, track=None) -> int:
        """Record one failed track attempt and return the updated streak."""
        count = self._get_playback_failure_count(player) + 1
        try:
            player.consecutive_playback_failures = count
            player._counted_failure_track = track
        except Exception:
            pass
        return count

    async def _resolve_playback_failure_channel(self, player):
        """Best-effort channel for playback failure notices."""
        target_channel = getattr(player, 'channel', None) or getattr(player, 'home_channel', None)
        if target_channel is not None:
            return target_channel
        guild = getattr(player, 'guild', None)
        if guild is None:
            return None
        try:
            static_channel_id = await self.guild_model.get_music_channel(guild.id)
        except Exception:
            static_channel_id = None
        if static_channel_id:
            try:
                channel = self.bot.get_channel(static_channel_id)
            except Exception:
                channel = None
            if channel is not None:
                return channel
        return getattr(guild, 'system_channel', None)

    async def _play_next_track(self, player: CustomPlayer, next_track: lava_lyra.Track) -> bool:
        """Play the next queued track, counting immediate start errors as failures.

        Returns True when playback started. When starting fails and the
        consecutive-failure threshold is reached, playback gives up instead of
        idling with a stale queue.
        """
        try:
            await player.play(next_track, gapless=getattr(player, 'gapless_enabled', False))
            return True
        except Exception as play_err:
            failures = self._register_playback_failure(player, next_track)
            logger.error(
                f"[PLAYBACK] Failed to start next track '{getattr(next_track, 'title', 'Unknown')}': {play_err}"
            )
            if failures >= MAX_CONSECUTIVE_PLAYBACK_FAILURES:
                await self._give_up_after_repeated_failures(player, next_track, failures)
            else:
                self.start_timeout(player.guild.id, player)
                await self.update_static_embed(player.guild.id)
                await self.delete_now_playing_message(player.guild.id)
            return False

    async def _halt_after_repeated_failures(self, player, track, failures: int) -> None:
        """Stop auto-advancing after repeated consecutive failures and notify.

        Autoplay is switched off and the pending end event for the failed
        track is suppressed so the queue does not advance. Remaining queued
        tracks are kept - use /play or /skip to resume. See _give_up_after_repeated_failures
        for the voice-player restart that follows this halt.


        """
        try:
            player.autoplay_enabled = False
        except Exception:
            pass
        try:
            player._give_up_track = track
        except Exception:
            pass
        try:
            if getattr(player, 'is_playing', False):
                await player.stop()
        except Exception as e:
            logger.debug(f'[PLAYBACK] Error stopping player during give-up: {e}')
        guild = getattr(player, 'guild', None)
        if guild is not None:
            try:
                self.start_timeout(guild.id, player)
            except Exception:
                pass
            try:
                await self.update_static_embed(guild.id)
            except Exception:
                pass
            try:
                await self.delete_now_playing_message(guild.id)
            except Exception:
                pass
        target_channel = await self._resolve_playback_failure_channel(player)
        if target_channel is not None:
            try:
                msg = await i18n.t(target_channel, 'music.errors.too_many_failures', count=failures)
                await target_channel.send(msg, delete_after=15)
            except Exception:
                pass
        self._reset_playback_failures(player)

    async def _give_up_after_repeated_failures(self, player, track, failures: int) -> None:
        """Stop retrying after repeated failures, then restart the voice player.

        Halts auto-advancing and notifies via _halt_after_repeated_failures,
        then restarts the voice player (best-effort) so a stuck connection
        does not leave the bot idling in voice. The upcoming queue, loop
        mode, history, and session settings are preserved - use /play or
        /skip to resume.
        """
        await self._halt_after_repeated_failures(player, track, failures)
        try:
            await self._restart_player_after_failures(player)
        except Exception as e:
            logger.debug(f'[PLAYBACK] Player restart after repeated failures failed: {e}')

    async def _restart_player_after_failures(self, player) -> bool:
        """Disconnect and reconnect the voice player to clear stuck state.

        Reconnects to the same voice channel and restores the upcoming
        queue, loop mode, queue position, history, home channel, and
        session volume onto the fresh player. Autoplay stays off so the
        bot does not loop back into failures. Returns True when a fresh
        player was connected. Never raises - on any failure the bot stays
        on the existing (stopped) player.
        """
        guild = getattr(player, 'guild', None)
        if guild is None:
            return False
        voice_channel = getattr(player, 'channel', None)
        if voice_channel is None:
            return False
        connect = getattr(voice_channel, 'connect', None)
        if connect is None:
            return False
        guild_id = getattr(guild, 'id', None)
        if guild_id is None:
            return False

        queue_obj = getattr(player, 'queue', None)
        try:
            queued_tracks = list(queue_obj.to_list()) if queue_obj is not None else []
        except Exception:
            queued_tracks = []
        try:
            loop_mode = queue_obj.loop_mode if queue_obj is not None else LoopMode.OFF
        except Exception:
            loop_mode = LoopMode.OFF
        try:
            queue_current = queue_obj.current if queue_obj is not None else None
        except Exception:
            queue_current = None
        try:
            history = list(getattr(player, 'history', None) or [])
        except Exception:
            history = []
        home_channel = getattr(player, 'home_channel', None)
        session_volume = getattr(player, 'session_volume', None)

        try:
            await player.disconnect(force=True)
        except Exception as e:
            logger.debug(f'[PLAYBACK] Error disconnecting stuck player for guild {guild_id}: {e}')

        try:
            new_player = await voice_channel.connect(cls=CustomPlayer)
        except Exception as e:
            logger.warning(f'[PLAYBACK] Failed to restart player for guild {guild_id}: {e}')
            return False

        try:
            await self._init_player(new_player, guild_id, home_channel)
        except Exception as e:
            logger.debug(f'[PLAYBACK] Error re-initializing restarted player for guild {guild_id}: {e}')
        try:
            new_player.autoplay_enabled = False
        except Exception:
            pass
        try:
            if session_volume is not None:
                new_player.session_volume = session_volume
        except Exception:
            pass
        try:
            new_queue = getattr(new_player, 'queue', None)
            if new_queue is not None:
                try:
                    new_queue.set_loop_mode(loop_mode)
                except Exception:
                    pass
                for queued in queued_tracks:
                    try:
                        new_queue.put(queued)
                    except Exception:
                        break
                if queue_current is not None:
                    try:
                        new_queue.set_current(queue_current)
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            new_player.history = list(history)
        except Exception:
            pass
        try:
            if isinstance(session_volume, int) and not isinstance(session_volume, bool):
                await new_player.set_volume(session_volume)
        except Exception:
            pass
        logger.info(f'[PLAYBACK] Restarted player for guild {guild_id} after repeated failures')
        try:
            self.start_timeout(guild_id, new_player)
        except Exception:
            pass
        try:
            await self.update_static_embed(guild_id)
        except Exception:
            pass
        return True
