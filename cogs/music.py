import asyncio
import logging
import re
import time
from typing import Optional, cast
from urllib.parse import urlparse

import discord
import lava_lyra
from discord import app_commands
from discord.ext import commands

from config import Config
from database.models import UserModel, GuildModel
from utils.i18n import i18n
from utils.permissions import require_capability
from utils.lastfm import lastfm_handler
from utils.queue import (
    CustomPlayer,
    LoopMode,
    UnsafeTrackQuery,
    apply_session_volume,
    assert_safe_track_query,
    remember_session_volume,
)
from cogs._music_playlists import PlaylistCommands
from services.music.presentation import MusicPresentation, build_queue_timeline, get_track_artwork
from services.music.recovery import PlaybackRecovery, MAX_CONSECUTIVE_PLAYBACK_FAILURES
from services.music.prefetch import PlaybackPrefetch
from utils.autoplay import AutoplayMixin, is_channel_blocked, track_channel_id
from utils.autoplay import clean_artist_name as clean_artist_name
from utils.autoplay import normalize_core_title as normalize_core_title
from utils.music_views import (
    IdlePlaylistView,
    LastFMAuthView,
    NowPlayingView,
    PlayerSettingsSelect,
    PlaylistSaveSelect,
    PlaylistSelect,
    QueuePaginationView,
)

logger = logging.getLogger(__name__)

# Idle seconds before the bot auto-disconnects from voice when there is
# nothing left to play (and 24/7 mode is off).
IDLE_DISCONNECT_SECONDS = 300


class Music(commands.Cog, AutoplayMixin, MusicPresentation, PlaylistCommands, PlaybackRecovery, PlaybackPrefetch):
    def __init__(self, bot):
        self.bot = bot
        self.user_model = UserModel()
        self.guild_model = GuildModel()
        self.timeout_tasks = {}
        self.selecting_users = set()
        self.autoplay_played_uris = {}  # guild_id -> bounded history of played URIs
        self.autoplay_played_titles = {}  # guild_id -> bounded history of normalized core titles
        self.autoplay_recent_artists = {}  # guild_id -> bounded history of recent artists
        self.now_playing_messages = {}  # guild_id -> message reference for cleanup
        self._voice_player_sessions = {}  # guild_id -> the session owning the voice player
        self._voice_player_views = {}  # guild_id -> active control callbacks
        self._music_directory_messages = {}  # guild_id -> shared directory message
        self._static_embed_locks = {}  # guild_id -> asyncio.Lock()
        self._autoplay_prefetch_tasks = {}  # guild_id -> asyncio.Task for in-flight prefetch
        self._autoplay_prefetch_cache = {}  # guild_id -> {'for_track': track, 'track': prefetched}

    @property
    def node(self):
        node_id = 'MAIN' if getattr(self.bot, 'is_main', True) else f'MAIN_{self.bot.user.id}'
        try:
            return lava_lyra.NodePool.get_node(identifier=node_id)
        except Exception:
            return lava_lyra.NodePool.get_node()

    async def cog_before_invoke(self, ctx: commands.Context):
        """Automatically defer slash commands to prevent timeout"""
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        """Global error handler for the music cog to catch node and session errors gracefully."""
        original = getattr(error, 'original', error)
        err_str = str(original)

        if 'Session not found' in err_str:
            if ctx.guild and ctx.guild.voice_client:
                try:
                    await ctx.guild.voice_client.disconnect(force=True)
                except Exception:
                    pass
            try:
                msg = await i18n.t(ctx, 'music.errors.session_died')
                if ctx.interaction and not ctx.interaction.response.is_done():
                    await ctx.send(msg, ephemeral=True)
                else:
                    await ctx.send(msg, delete_after=10)
            except Exception:
                pass
            return

        if 'refused' in err_str.lower() or 'connect' in err_str.lower():
            if ctx.guild and ctx.guild.voice_client:
                try:
                    await ctx.guild.voice_client.disconnect(force=True)
                except Exception:
                    pass
            try:
                msg = await i18n.t(ctx, 'music.errors.node_offline')
                if ctx.interaction and not ctx.interaction.response.is_done():
                    await ctx.send(msg, ephemeral=True)
                else:
                    await ctx.send(msg, delete_after=10)
            except Exception:
                pass
            return

        logger.error(f'Unhandled command error in Music cog: {error}')

    def start_timeout(self, guild_id, player):
        """Starts the idle auto-disconnect timer (see IDLE_DISCONNECT_SECONDS)."""
        if guild_id in self.timeout_tasks:
            self.timeout_tasks[guild_id].cancel()

        # Bypass 24/7
        if hasattr(player, 'twenty_four_seven') and player.twenty_four_seven:
            return

        async def _timeout():
            await asyncio.sleep(IDLE_DISCONNECT_SECONDS)
            if player and player.is_connected and player.guild.voice_client is player and not player.twenty_four_seven:
                await player.destroy()

        self.timeout_tasks[guild_id] = self.bot.loop.create_task(_timeout())

    async def delete_now_playing_message(self, guild_id: int):
        """Keep the voice player while connected, including an empty queue."""
        await self.update_now_playing_embed(guild_id)

    @commands.Cog.listener()
    async def on_ready(self):
        # Reconcile persisted bot-scoped messages after a restart or gateway reconnect.
        for guild in self.bot.guilds:
            await self.update_static_embed(guild.id)

    def cancel_timeout(self, guild_id):
        """Cancels the disconnect timer"""
        if guild_id in self.timeout_tasks:
            task = self.timeout_tasks.pop(guild_id)
            if task is not asyncio.current_task():
                task.cancel()

    async def cog_load(self):
        self.bot.loop.create_task(self._connect_nodelink())

    async def _connect_nodelink(self):
        """Connect to NodeLink after bot is ready with automatic retry on failure."""
        await self.bot.wait_until_ready()
        parsed = urlparse(Config.NODELINK_URI)
        host = parsed.hostname or '127.0.0.1'
        port = parsed.port or 2333
        node_id = 'MAIN' if getattr(self.bot, 'is_main', True) else f'MAIN_{self.bot.user.id}'

        backoff = 5
        max_backoff = 300
        attempt = 0

        while True:
            attempt += 1
            logger.info(f'[NODELINK] Connection attempt #{attempt} to {host}:{port}')
            try:
                if node_id in lava_lyra.NodePool._nodes:
                    try:
                        await lava_lyra.NodePool._nodes[node_id].disconnect()
                    except Exception:
                        pass

                node = await lava_lyra.NodePool.create_node(
                    bot=self.bot,
                    host=host,
                    port=port,
                    password=Config.NODELINK_PASSWORD,
                    identifier=node_id,
                    lyrics=True,
                    search=True,
                    fallback=True,
                )
                logger.info(f'[NODELINK] Node created successfully: {node.identifier}')
                return
            except Exception as e:
                logger.error(f'[NODELINK] Failed to connect (attempt #{attempt}): {e}')
                logger.info(f'[NODELINK] Retrying in {backoff}s...')
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def check_voice_channel(self, ctx: commands.Context, response_channel=None, redirected: bool = False):
        """Check if the user is in the same voice channel as the bot.
        Returns True if OK, or sends an error message and returns False.
        """
        if not ctx.voice_client:
            return True

        if not ctx.author.voice or ctx.author.voice.channel != ctx.voice_client.channel:
            from runtime.bots import all_bots

            free_bot = None
            same_channel_bot = None

            for b in all_bots:
                if b == self.bot:
                    continue
                if not b.get_guild(ctx.guild.id):
                    continue

                member = ctx.guild.get_member(b.user.id)
                b_voice = member.voice if member else None
                b_vc = b_voice.channel if b_voice else None

                if b_vc:
                    if ctx.author.voice and b_vc == ctx.author.voice.channel:
                        same_channel_bot = b
                else:
                    if not free_bot:
                        free_bot = b

            if same_channel_bot:
                msg = (
                    f'I am currently in use. Please use {same_channel_bot.user.mention}'
                    f' who is already in your voice channel!'
                )
            elif free_bot:
                msg = f'I am currently in use. Please use {free_bot.user.mention} instead!'
            else:
                msg = await i18n.t(ctx, 'music.errors.voice_channel_mismatch')

            if response_channel:
                await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=10)
            else:
                await ctx.send(msg, ephemeral=True, delete_after=10)
            return False
        return True

    async def get_response_channel(self, ctx: commands.Context) -> discord.TextChannel:
        """Get the channel where the bot should respond (Static Channel or Context Channel)"""
        static_channel_id = await self.guild_model.get_music_channel(ctx.guild.id)
        if static_channel_id:
            channel = self.bot.get_channel(static_channel_id)
            if channel:
                return channel
        return ctx.channel

    async def handle_command_cleanup(self, ctx: commands.Context):
        """Delete user command if it was sent outside the static channel"""
        static_channel_id = await self.guild_model.get_music_channel(ctx.guild.id)
        if static_channel_id and ctx.channel.id != static_channel_id:
            try:
                await ctx.message.delete()
            except Exception:
                pass

    async def acknowledge_static_redirect(self, ctx: commands.Context) -> bool:
        """
        For slash commands with a static channel configured, acknowledge the interaction.
        Returns True if acknowledgment was sent, False otherwise.
        """
        static_channel_id = await self.guild_model.get_music_channel(ctx.guild.id)
        if ctx.interaction and static_channel_id and ctx.channel.id != static_channel_id:
            channel = self.bot.get_channel(static_channel_id)
            channel_mention = channel.mention if channel else f'#{static_channel_id}'
            msg_processed = await i18n.t(ctx, 'music.static_response.processed', channel=channel_mention)
            await ctx.send(msg_processed, ephemeral=True)
            return True
        return False

    async def _init_player(self, player: CustomPlayer, guild_id: int, home_channel=None) -> None:
        """Initialize player settings and home channel from guild configuration."""
        self._clear_autoplay_prefetch(guild_id)
        if home_channel is not None:
            player.home_channel = home_channel
        player.autoplay_enabled = await self.guild_model.get_autoplay_enabled(guild_id)
        try:
            await self.apply_autoplay_settings(player, guild_id)
        except Exception:
            pass
        player.twenty_four_seven = await self.guild_model.get_247_enabled(guild_id)
        player.consecutive_playback_failures = 0
        player._counted_failure_track = None
        player._give_up_track = None

    async def send_response(
        self,
        ctx: commands.Context,
        response_channel,
        redirected: bool,
        content=None,
        embed=None,
        view=None,
        delete_after=None,
    ):
        """
        Send a response to the proper channel based on context.
        For slash commands without redirect, uses ctx.send to respond to the interaction.
        Otherwise, sends to the response_channel.
        """
        if ctx.interaction and not redirected:
            return await ctx.send(content=content, embed=embed, view=view, delete_after=delete_after)
        return await response_channel.send(content=content, embed=embed, view=view, delete_after=delete_after)

    async def edit_and_delete(self, message, content=None, embed=None, view=None, delete_after=None):
        """
        Safely edit a message and delete it after a delay if specified.
        Works with both standard messages and WebhookMessages (interaction responses).
        """
        if message:
            await message.edit(content=content, embed=embed, view=view)
            if delete_after:
                try:
                    await message.delete(delay=delete_after)
                except Exception:

                    async def delayed_delete():
                        await asyncio.sleep(delete_after)
                        try:
                            await message.delete()
                        except Exception:
                            pass

                    self.bot.loop.create_task(delayed_delete())

    @commands.Cog.listener()
    async def on_lyra_node_connected(self, node_id: str, is_nodelink: bool, reconnect: bool):
        logger.info(f'[NODELINK] Node connected: {node_id} (reconnect={reconnect})')

    @commands.Cog.listener()
    async def on_lyra_track_start(self, player: CustomPlayer, track: lava_lyra.Track):
        if not player or not track:
            return

        logger.info(f'[LASTFM DEBUG] Track START event fired: {track.title}')

        player.current_track_start_time = int(time.time())
        self.cancel_timeout(player.guild.id)

        # Stage the next queued song server-side while this one plays so the
        # transition is seamless (no-op when the queue is empty).
        await self._preload_gapless_next(player)

        try:
            self._schedule_autoplay_prefetch(player, track)
        except Exception:
            pass

        await self.update_static_embed(player.guild.id)

        if not lastfm_handler.enabled:
            return

        if player.channel:
            member_ids = [m.id for m in player.channel.members if not m.bot]
            for user_id in member_ids:
                user_data = await self.user_model.get_user(user_id)
                if user_data and user_data.get('lastfm', {}).get('session_key'):
                    artist, title = lastfm_handler.clean_track_info(track.author, track.title)
                    await lastfm_handler.update_now_playing(user_data['lastfm']['session_key'], artist, title)

    @commands.Cog.listener()
    async def on_lyra_track_end(self, player: CustomPlayer, track: lava_lyra.Track, reason: str):
        """Handle track end: advance queue, autoplay, scrobble, and update embeds.

        The entire body is wrapped so an unexpected exception in one branch
        cannot break future track-end events — we log, refresh the static
        embed, and let playback recover on the next event.
        """
        scrobble_ts = None  # populated by the GAPLESS branch
        try:
            logger.info(f'[LASTFM DEBUG] Track end event fired - reason: {reason}')
            if not player:
                logger.warning('[LASTFM DEBUG] No player in payload')
                return
            if not track:
                logger.warning('[LASTFM DEBUG] No track in payload')
                return
            logger.info(f'[LASTFM DEBUG] Player channel: {player.channel}')

            # Save the start time before any branch can trigger
            # on_lyra_track_start, which overwrites
            # player.current_track_start_time for the next song.
            track_start = getattr(player, 'current_track_start_time', None)

            if not hasattr(player, 'history'):
                player.history = []

            # NodeLink/Lavalink send end reasons in mixed case ('finished',
            # 'loadFailed', 'stopped', 'replaced', 'cleanup', 'gapless'), so
            # normalize before matching.
            normalized_reason = (reason or '').replace('_', '').upper()

            if normalized_reason == 'CLEANUP':
                # CLEANUP: the player is being destroyed. Do not advance the
                # queue or autoplay.
                return

            give_up_track = getattr(player, '_give_up_track', None)
            if give_up_track is not None:
                if track is not None and give_up_track is track:
                    # End event caused by our own give-up stop(): swallow it so
                    # the queue does not advance after we stopped retrying.
                    return
                try:
                    player._give_up_track = None
                except Exception:
                    pass

            # When gapless is enabled, a skip (player.stop()) causes NodeLink to
            # swap the preloaded next track in server-side and report REPLACED
            # instead of GAPLESS.  No TrackStart event follows, so we must
            # handle it here identically to GAPLESS — advance the queue, sync
            # the now-playing embed, and preload the next-next song.
            is_gapless_replaced = (
                normalized_reason == 'REPLACED'
                and getattr(player, 'gapless_enabled', False)
                and player.current is not None
                and player.current is not track
            )

            if normalized_reason == 'GAPLESS' or is_gapless_replaced:
                # A new track took over, so any failure streak ends here.
                self._reset_playback_failures(player)
                # NodeLink already swapped in the preloaded next track server-side
                # (lava-lyra points player.current at it) and no TrackStart event
                # follows, so this branch covers what a track start would: queue
                # sync, scrobble of the ended song (via fall-through to the
                # scrobble block), now-playing announce, and preloading of the
                # FOLLOWING song so the chain stays seamless.
                new_current = player.current
                # Gapless has no library TrackStart event to update this pointer.
                # Otherwise the following end event still identifies the old song.
                player._ending_track = new_current
                if not player.history or player.history[-1] is not track:
                    player.history.append(track)
                # Saved before on_lyra_track_start overwrites it for the new song.
                scrobble_ts = getattr(player, 'current_track_start_time', int(time.time() - (track.length / 1000)))
                if new_current is not None:
                    for i, queued in enumerate(player.queue.to_list()):
                        if queued is new_current:
                            player.queue.remove_at(i)
                            break
                    if player.queue.loop_mode == LoopMode.QUEUE:
                        # Mirror CustomQueue.get(): the ended track goes to the back.
                        player.queue.put(track)
                    player.queue.set_current(new_current)
                    try:
                        await self.on_lyra_track_start(player, new_current)
                    except Exception as e:
                        logger.warning(f'[GAPLESS] Track-start announce failed: {e}')
                else:
                    await self.update_static_embed(player.guild.id)

            if normalized_reason == 'REPLACED' and not is_gapless_replaced:
                # Non-gapless REPLACED: a new track was explicitly played (e.g.
                # /play while something was already playing).  A TrackStart
                # event will follow and handle the embed update; nothing to do.
                return

            if normalized_reason not in ('FINISHED', 'STOPPED', 'LOADFAILED', 'GAPLESS') and not is_gapless_replaced:
                # FINISHED/STOPPED/LOADFAILED advance below; GAPLESS only falls
                # through to the scrobble block. Anything else is ignored.
                return

            # GAPLESS already transitioned above and must not advance again.
            if normalized_reason != 'GAPLESS' and not is_gapless_replaced:
                if normalized_reason == 'FINISHED':
                    # The track played through, so any failure streak ends here.
                    self._reset_playback_failures(player)

                if normalized_reason == 'LOADFAILED':
                    if getattr(player, '_counted_failure_track', None) is track:
                        # Already counted via the track exception/stuck handler.
                        failures = self._get_playback_failure_count(player)
                    else:
                        failures = self._register_playback_failure(player, track)
                    try:
                        player._counted_failure_track = None
                    except Exception:
                        pass
                    if failures >= MAX_CONSECUTIVE_PLAYBACK_FAILURES:
                        logger.warning(
                            f'[PLAYBACK] Giving up after {failures} consecutive playback failures '
                            f'for guild {player.guild.id if player.guild else "unknown"}'
                        )
                        await self._give_up_after_repeated_failures(player, track, failures)
                        return

                # Avoid duplicate history entries when looping the same track
                if not player.history or player.history[-1] is not track:
                    player.history.append(track)

                if player.queue.loop_mode == LoopMode.TRACK and normalized_reason == 'FINISHED':
                    logger.info(f"[TRACK_END] Track loop active, replaying: '{track.title}'")
                    next_track = player.queue.get()
                    await apply_session_volume(player, await self.guild_model.get_default_volume(player.guild.id))

                    await player.play(next_track)
                else:
                    await self._advance_queue_or_autoplay(player, track)

            if (
                reason.lower() == 'finished' or normalized_reason == 'GAPLESS' or is_gapless_replaced
            ) and player.channel:
                member_ids = [m.id for m in player.channel.members if not m.bot]
                logger.info(f'Track finished: {track.title} - checking scrobble for {len(member_ids)} users')

                timestamp = track_start if track_start is not None else int(time.time() - (track.length / 1000))
                if (normalized_reason == 'GAPLESS' or is_gapless_replaced) and scrobble_ts is not None:
                    timestamp = scrobble_ts

                for user_id in member_ids:
                    user_data = await self.user_model.get_user(user_id)
                    if not user_data:
                        logger.debug(f'User {user_id} has no user data')
                        continue

                    lastfm = user_data.get('lastfm', {})
                    session_key = lastfm.get('session_key')
                    if not session_key:
                        logger.debug(f'User {user_id} has no Last.fm session_key')
                        continue

                    # Default to True if scrobbling key doesn't exist (backwards compatibility)
                    if not lastfm.get('scrobbling', True):
                        logger.debug(f'User {user_id} has scrobbling disabled')
                        continue

                    artist, title = lastfm_handler.clean_track_info(track.author, track.title)
                    logger.info(f'Scrobbling track for user {user_id}: {artist} - {title}')
                    await lastfm_handler.scrobble(session_key, artist, title, timestamp)

        except Exception:
            logger.error('[TRACK_END] Unhandled error in track end handler:', exc_info=True)
            # Best-effort: refresh the static embed so the UI doesn't appear stuck
            if player and player.guild:
                try:
                    await self.update_static_embed(player.guild.id)
                except Exception:
                    pass

    async def _advance_queue_or_autoplay(self, player: CustomPlayer, track: lava_lyra.Track) -> None:
        """Advance only the idle session, rechecking state after recommendation awaits."""

        def is_idle_session():
            # A paused song also owns playback even though is_playing is False.
            return (
                player.is_connected
                and player.guild.voice_client is player
                and player.current is None
                and not player.is_playing
            )

        def can_autoplay():
            return player.autoplay_enabled and player.queue.is_empty

        if not is_idle_session():
            return

        if can_autoplay():
            await asyncio.sleep(0.5)  # wait to avoid infinite skip loop
            if not is_idle_session():
                return
            if can_autoplay():
                recommended = self._take_autoplay_prefetch(player, track)
                if recommended is None:
                    recommended = await self._await_pending_autoplay_prefetch(player, track)
                if not is_idle_session():
                    return
                if can_autoplay() and recommended is None:
                    recommended = await self._fetch_autoplay_track(player, track)
                if not is_idle_session():
                    return
                if can_autoplay() and recommended is not None:
                    default_volume = await self.guild_model.get_default_volume(player.guild.id)
                    if not is_idle_session():
                        return
                    if can_autoplay():
                        await apply_session_volume(player, default_volume)
                        if not is_idle_session():
                            return
                        if can_autoplay() and not is_channel_blocked(
                            recommended, getattr(player, 'autoplay_config', None)
                        ):
                            try:
                                await player.play(recommended)
                                return
                            except Exception as play_err:
                                failures = self._register_playback_failure(player, recommended)
                                if failures >= MAX_CONSECUTIVE_PLAYBACK_FAILURES:
                                    await self._give_up_after_repeated_failures(player, recommended, failures)
                                    return
                                logger.error(
                                    f"[AUTOPLAY] Failed to play recommended track '{recommended.title}': {play_err}"
                                )

        # User requests arriving during any await take precedence over autoplay.
        if not player.queue.is_empty:
            next_track = player.queue.get(force_next=True)
            self._clear_autoplay_prefetch(player.guild.id)
            await self._play_next_track(player, next_track)
        else:
            self.start_timeout(player.guild.id, player)
            await self.update_static_embed(player.guild.id)
            await self.delete_now_playing_message(player.guild.id)

    @commands.Cog.listener()
    async def on_lyra_track_exception(self, player: CustomPlayer, track: lava_lyra.Track, exception):
        """Handler for when a track encounters an exception during playback."""
        logger.warning(f"[LYRA] Track exception for '{track.title if track else 'Unknown'}': {exception}")

        if not player:
            return
        failures = self._register_playback_failure(player, track)
        if failures >= MAX_CONSECUTIVE_PLAYBACK_FAILURES:
            logger.warning(
                f'[PLAYBACK] Giving up after {failures} consecutive playback failures '
                f'for guild {player.guild.id if player.guild else "unknown"}'
            )
            await self._give_up_after_repeated_failures(player, track, failures)
            return

        target_channel = await self._resolve_playback_failure_channel(player)
        if target_channel:
            msg = await i18n.t(target_channel, 'music.errors.track_failed', error=str(exception))
            await target_channel.send(msg, delete_after=10)

    @commands.Cog.listener()
    async def on_lyra_track_stuck(self, player: CustomPlayer, track: lava_lyra.Track, threshold: float):
        """Handler for when a track gets stuck (stops sending frames)."""
        logger.warning(f"[LYRA] Track stuck: '{track.title if track else 'Unknown'}' at {threshold}ms")

        if not player:
            return

        failures = self._register_playback_failure(player, track)
        if failures >= MAX_CONSECUTIVE_PLAYBACK_FAILURES:
            logger.warning(
                f'[PLAYBACK] Giving up after {failures} consecutive playback failures '
                f'for guild {player.guild.id if player.guild else "unknown"}'
            )
            await self._give_up_after_repeated_failures(player, track, failures)
            return

        target_channel = await self._resolve_playback_failure_channel(player)

        if target_channel:
            locale = await i18n.get_guild_locale(player.guild.id) or i18n.default_locale
            await target_channel.send(
                i18n.get_text('music.errors.track_stuck', locale, seconds=int(threshold // 1000)), delete_after=10
            )

    @commands.Cog.listener()
    async def on_lyra_websocket_closed(self, payload: lava_lyra.WebSocketClosedPayload):
        guild = payload.guild or self.bot.get_guild(getattr(payload, '_guild_id', 0))
        guild_id_str = str(guild.id) if guild else str(getattr(payload, '_guild_id', 'Unknown'))
        logger.warning(
            f'[LYRA] WebSocket closed for guild {guild_id_str}: '
            f'Code {payload.code}, Reason: {payload.reason} (by_remote={payload.by_remote})'
        )

        # 4014 also occurs during channel moves, including local voice transport
        # replacement in NodeLink. The close can arrive before the gateway move
        # event, so even the old cached channel means we must keep the session.
        # Fresh voice updates reconnect it; an actual kick is cleaned up by
        # on_voice_state_update (or below if the gateway leave arrived first).
        voice_state = getattr(getattr(guild, 'me', None), 'voice', None)
        if payload.code == 4014 and voice_state and voice_state.channel:
            logger.info(f'[LYRA] Keeping player during voice transport change for guild {guild.id}')
            return

        # 5001 = NodeLink worker_failed. Clean up actual voice disconnects too.
        if guild and guild.voice_client:
            player = guild.voice_client
            if payload.code in (5001, 4014) or not payload.by_remote:
                logger.info(f'[LYRA] Cleaning up disconnected player for guild {guild.id} (code {payload.code})')
                try:
                    await player.disconnect(force=True)
                except Exception as e:
                    logger.debug(f'[LYRA] Error disconnecting player during websocket_closed: {e}')

    @commands.Cog.listener()
    async def on_lyra_node_disconnected(self, node_id: str, is_nodelink: bool, player_count: int):
        logger.warning(f'[LYRA] Node disconnected: {node_id} (nodelink={is_nodelink}, players={player_count})')

    @commands.Cog.listener()
    async def on_lyra_node_reconnecting(self, node_id: str, is_nodelink: bool, retry_in: float):
        logger.warning(f'[LYRA] Node reconnecting: {node_id} (retry in {retry_in}s)')

    @commands.Cog.listener()
    async def on_lyra_pause(self, player: CustomPlayer, paused: bool):
        if player and player.guild:
            await self.update_static_embed(player.guild.id)

    @commands.Cog.listener()
    async def on_lyra_seek(self, player: CustomPlayer, position: float):
        if player and player.guild:
            logger.info(f'[LYRA] Seek in guild {player.guild.id} to {position}ms')
            try:
                self._maybe_prefetch_after_seek(player, position)
            except Exception:
                pass
            await self.update_static_embed(player.guild.id)

    @commands.Cog.listener()
    async def on_lyra_volume_changed(self, player: CustomPlayer, volume: int):
        if player and player.guild:
            logger.info(f'[LYRA] Volume in guild {player.guild.id} changed to {volume}')
            # Any volume change (command, web UI, dashboard) becomes the
            # session volume so the next track keeps it.
            remember_session_volume(player, volume)
            await self.update_static_embed(player.guild.id)

    @commands.Cog.listener()
    async def on_lyra_lyrics_found(self, player: CustomPlayer, track: lava_lyra.Track, lyrics):
        logger.info(f'[LYRICS] Found lyrics for "{track.title if track else "Unknown"}" ({len(lyrics.lines)} lines)')

    @commands.Cog.listener()
    async def on_lyra_lyrics_not_found(self, player: CustomPlayer, track: lava_lyra.Track):
        logger.info(f'[LYRICS] No lyrics found for "{track.title if track else "Unknown"}"')

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.id == self.bot.user.id:
            if before.channel and not after.channel:
                logger.info(f'[VOICE] Bot disconnected from voice in guild {member.guild.id}')
                player = self._voice_player_sessions.get(member.guild.id) or member.guild.voice_client
                if player:
                    await player.destroy()
                await self.update_static_embed(member.guild.id)
                return
            if after.channel and before.channel != after.channel:
                # An idle timer from the previous channel must not disconnect
                # playback after the bot has moved to a channel with listeners.
                self.cancel_timeout(member.guild.id)
                player = member.guild.voice_client
                if player and len(after.channel.members) == 1 and after.channel.members[0].id == self.bot.user.id:
                    self.start_timeout(member.guild.id, player)
                await self.update_static_embed(member.guild.id)
                return

        player = member.guild.voice_client
        if not player or not player.channel:
            return

        # Check if bot is alone
        if len(player.channel.members) == 1 and player.channel.members[0].id == self.bot.user.id:
            if not getattr(player, 'twenty_four_seven', False):
                logger.debug(f'[VOICE] Bot is alone in channel, starting timeout for guild {member.guild.id}')
                self.start_timeout(member.guild.id, player)
        elif len(player.channel.members) > 1:
            self.cancel_timeout(member.guild.id)

        await asyncio.sleep(1)
        await self.update_static_embed(member.guild.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if not message.guild:
            return

        from runtime.bots import all_bots

        channel_id = await self.guild_model.get_music_channel(message.guild.id)
        player = message.guild.voice_client
        in_voice_chat = bool(
            player and player.is_connected and player.channel and player.channel.id == message.channel.id
        )
        if not in_voice_chat:
            if not channel_id or message.channel.id != channel_id:
                return
            owners = [bot for bot in all_bots if bot.get_guild(message.guild.id) is not None and bot.get_cog('Music')]
            if owners and owners[0] is not self.bot:
                return

        logger.info(f'[MUSIC] Message in music channel from {message.author}: {message.content[:50]}')

        if message.author.id in self.selecting_users:
            return

        try:
            await message.delete()
            logger.info(f'[MUSIC] Deleted message from {message.author}')
        except discord.Forbidden:
            logger.warning(f'[MUSIC] Missing permissions to delete message in guild {message.guild.id}')
        except discord.NotFound:
            pass
        except Exception as e:
            logger.warning(f'[MUSIC] Failed to delete message: {e}')

        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        query = message.content
        if not query or not query.strip():
            try:
                hint = await i18n.t(message.channel, 'music.static_response.help_hint')
                await message.channel.send(hint, delete_after=10)
            except Exception:
                pass
            return

        await self._play_logic(ctx, query, message.channel)

    async def delegate_to_bot(self, ctx, target_bot, query, response_channel, mode):
        """Helper to delegate the play command execution to another bot."""
        import copy

        new_ctx = copy.copy(ctx)
        new_ctx.bot = target_bot
        new_ctx.guild = target_bot.get_guild(ctx.guild.id)
        if new_ctx.guild:
            new_ctx.author = new_ctx.guild.get_member(ctx.author.id) or ctx.author

        target_cog = target_bot.get_cog('Music')
        if not target_cog:
            return False

        target_response_channel = target_bot.get_channel(response_channel.id) or response_channel

        msg = f"I am currently in use. I've asked {target_bot.user.mention} to handle your request!"
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.interaction.response.send_message(msg, ephemeral=True)
        else:
            await ctx.send(msg, delete_after=10)

        self.bot.loop.create_task(
            target_cog._play_logic(new_ctx, query, target_response_channel, redirected=True, mode=mode)
        )
        return True

    async def _play_logic(
        self,
        ctx: commands.Context,
        query: str,
        response_channel: discord.TextChannel,
        redirected: bool = False,
        mode: str = 'normal',
    ):
        """Shared logic for play command and static channel. Mode can be 'normal', 'now', or 'next'."""
        logger.info(f'[PLAY] User {ctx.author.id} requested: {query[:100]}')

        # Check if we should delegate this command to another bot
        if not redirected and ctx.guild:
            from runtime.bots import all_bots

            user_vc = ctx.author.voice.channel if (ctx.author.voice and ctx.guild) else None
            if user_vc:
                target_bot = None
                for b in all_bots:
                    if b == self.bot:
                        continue
                    for vc in b.voice_clients:
                        if vc.guild.id == ctx.guild.id and vc.channel.id == user_vc.id:
                            target_bot = b
                            break
                    if target_bot:
                        break

                if target_bot:
                    if await self.delegate_to_bot(ctx, target_bot, query, response_channel, mode):
                        return

                self_vc = ctx.voice_client.channel if ctx.voice_client else None
                if self_vc and self_vc.id != user_vc.id:
                    free_bot = None
                    for b in all_bots:
                        if b == self.bot:
                            continue
                        is_busy = any(vc.guild.id == ctx.guild.id for vc in b.voice_clients)
                        if not is_busy and b.get_guild(ctx.guild.id):
                            free_bot = b
                            break

                    if free_bot:
                        if await self.delegate_to_bot(ctx, free_bot, query, response_channel, mode):
                            return

        user_data = await self.user_model.get_user(ctx.author.id)
        guild_data = await self.guild_model.get_guild(ctx.guild.id)

        # Check for saved playlists (use source_url for imported playlists)
        saved_pl = None
        if user_data and 'playlists' in user_data and query in user_data['playlists']:
            saved_pl = user_data['playlists'][query]
        elif guild_data and 'playlists' in guild_data and query in guild_data['playlists']:
            saved_pl = guild_data['playlists'][query]
        if saved_pl:
            if saved_pl.get('type') == 'imported' and saved_pl.get('source_url'):
                query = saved_pl['source_url']
            else:
                saved_pl = None

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            try:
                logger.debug(f'[VOICE] Connecting to voice channel: {ctx.author.voice.channel.name}')
                player = await ctx.author.voice.channel.connect(cls=CustomPlayer)
                await self._init_player(player, ctx.guild.id, response_channel)
            except Exception as e:
                logger.warning(f'[VOICE] Failed to connect: {e}')
                msg = await i18n.t(ctx, 'music.commands.play.not_in_voice')
                return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
        else:
            if not await self.check_voice_channel(ctx):
                return

        url_pattern = re.compile(r'https?://(?:www\.)?.+')
        is_url = url_pattern.match(query)
        SPOTIFY_URL_PATTERN = re.compile(r'https?://open\.spotify\.com/(track|album|playlist|artist)/([a-zA-Z0-9]+)')
        is_spotify = SPOTIFY_URL_PATTERN.match(query) is not None

        try:
            query = await assert_safe_track_query(query)
        except UnsafeTrackQuery as e:
            msg = await i18n.t(ctx, 'music.errors.track_failed', error=str(e))
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        try:
            if player:
                tracks = await player.get_tracks(query)
            else:
                tracks = await self.node.get_tracks(query)
        except Exception as e:
            err_str = str(e)
            if 'Session not found' in err_str and player:
                logger.warning(f'[RECOVERY] Session died for guild {ctx.guild.id}. Reconnecting...')
                try:
                    await player.destroy()
                except Exception:
                    if ctx.guild.voice_client:
                        await ctx.guild.voice_client.disconnect(force=True)
                try:
                    player = await ctx.author.voice.channel.connect(cls=CustomPlayer)
                    await self._init_player(player, ctx.guild.id, response_channel)
                    tracks = await player.get_tracks(query)
                except Exception as inner_e:
                    msg = await i18n.t(ctx, 'music.errors.track_failed', error=str(inner_e))
                    return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
            elif 'refused' in err_str.lower() or 'connect' in err_str.lower():
                if player:
                    try:
                        await player.destroy()
                    except Exception:
                        if ctx.guild.voice_client:
                            await ctx.guild.voice_client.disconnect(force=True)
                try:
                    msg = await i18n.t(ctx, 'music.errors.node_offline')
                except Exception:
                    msg = 'Music server is currently offline. Please try again later.'
                return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=10)
            else:
                msg = await i18n.t(ctx, 'music.errors.track_failed', error=err_str)
                return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
        if not tracks:
            msg = await i18n.t(ctx, 'music.commands.play.no_results', query=query)
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        # If it's a URL or a specific Container (Playlist), auto-play/queue
        if is_url or isinstance(tracks, lava_lyra.Playlist):
            if isinstance(tracks, lava_lyra.Playlist):
                for track in tracks.tracks:
                    track.requester = ctx.author.mention

                # For playlists, mode only affects the first track
                if mode == 'now' and player.is_playing:
                    # Play first track immediately, queue the rest
                    first_track = tracks.tracks[0]
                    for track in tracks.tracks[1:]:
                        player.queue.put_at_front(track)
                    await apply_session_volume(player, await self.guild_model.get_default_volume(ctx.guild.id))

                    await player.play(first_track)
                    msg = await i18n.t(ctx, 'music.commands.play.playing_now', title=first_track.title)
                elif mode == 'next':
                    # Add all tracks to front of queue (in reverse order so first track is next)
                    for track in reversed(tracks.tracks):
                        player.queue.put_at_front(track)
                    msg = await i18n.t(
                        ctx,
                        'music.commands.play.playlist_added_next',
                        count=len(tracks.tracks),
                        name=tracks.name,
                    )
                else:
                    player.queue.extend(tracks.tracks)
                    msg = await i18n.t(
                        ctx,
                        'music.commands.play.playlist_added',
                        count=len(tracks.tracks),
                        name=tracks.name,
                    )

                source_label = '🎵 Spotify' if is_spotify else '🎵 YouTube'
                msg = f'{source_label} | {msg}'
                await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
            else:
                track = tracks[0]
                track.requester = ctx.author.mention
                source_label = '🎵 Spotify' if is_spotify else '🎵 YouTube'

                if mode == 'now' and player.is_playing:
                    await apply_session_volume(player, await self.guild_model.get_default_volume(ctx.guild.id))

                    await player.play(track)
                    msg = await i18n.t(ctx, 'music.commands.play.playing_now', title=track.title)
                elif mode == 'next':
                    player.queue.put_at_front(track)
                    msg = await i18n.t(ctx, 'music.commands.play.added_next', title=track.title)
                else:
                    player.queue.put(track)
                    msg = await i18n.t(ctx, 'music.commands.play.added_to_queue', title=track.title)

                msg = f'{source_label} | {msg}'
                await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

            if not player.is_playing:
                await apply_session_volume(player, await self.guild_model.get_default_volume(ctx.guild.id))

                await player.play(player.queue.get(force_next=True))

            await self.update_static_embed(ctx.guild.id)
            return

        tracks_top = tracks[:5]
        select_text = await i18n.t(ctx, 'music.ui.select_track')
        instructions = await i18n.t(ctx, 'music.ui.selection_instructions')

        options_text = ''
        for i, track in enumerate(tracks_top):
            options_text += f'**{i + 1}.** {track.title} - {track.author} ({track.length // 1000}s)\n'

        full_msg = f'{select_text}\n{options_text}\n{instructions}'

        self.selecting_users.add(ctx.author.id)

        try:
            msg = await self.send_response(ctx, response_channel, redirected, content=full_msg, delete_after=60)

            def check(m):
                return m.author.id == ctx.author.id and m.channel.id == response_channel.id and m.content.isdigit()

            while True:
                response = await self.bot.wait_for('message', check=check, timeout=60)

                try:
                    await response.delete()
                except Exception:
                    pass

                choice = int(response.content)

                if choice == 0:
                    cancelled_text = await i18n.t(ctx, 'music.ui.selection_cancelled')
                    await self.edit_and_delete(msg, content=cancelled_text, delete_after=5)
                    return

                if 1 <= choice <= len(tracks_top):
                    track = tracks_top[choice - 1]
                    track.requester = ctx.author.mention

                    if mode == 'now' and player.is_playing:
                        await apply_session_volume(player, await self.guild_model.get_default_volume(ctx.guild.id))

                        await player.play(track)
                        added_text = await i18n.t(ctx, 'music.commands.play.playing_now', title=track.title)
                    elif mode == 'next':
                        player.queue.put_at_front(track)
                        added_text = await i18n.t(ctx, 'music.commands.play.added_next', title=track.title)
                    else:
                        player.queue.put(track)
                        if not player.is_playing:
                            await apply_session_volume(player, await self.guild_model.get_default_volume(ctx.guild.id))

                            await player.play(player.queue.get(force_next=True))
                        added_text = await i18n.t(ctx, 'music.commands.play.added_to_queue', title=track.title)

                    await self.edit_and_delete(msg, content=added_text, delete_after=5)
                    await self.update_static_embed(ctx.guild.id)
                    return

        except asyncio.TimeoutError:
            timeout_text = await i18n.t(ctx, 'music.ui.selection_timeout')
            try:
                await self.edit_and_delete(msg, content=timeout_text, delete_after=5)
            except Exception:
                pass
        finally:
            self.selecting_users.discard(ctx.author.id)

    @commands.hybrid_command(name='play', aliases=['p'])
    @app_commands.describe(
        query='Song name, URL, or playlist name',
        mode="Play mode: 'now' to play immediately, 'next' to play after current song",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name='Normal (add to queue)', value='normal'),
            app_commands.Choice(name='Play Now (skip current)', value='now'),
            app_commands.Choice(name='Play Next (after current)', value='next'),
        ]
    )
    async def play(self, ctx: commands.Context, *, query: str, mode: str = 'normal'):  # temp fix (might be permanent)
        """Play a song or saved playlist."""
        await self._play_command_logic(ctx, query, 'normal')

    @commands.command(name='playnow', aliases=['pn'])
    async def playnow(self, ctx: commands.Context, *, query: str):
        """Play a song immediately (skips current)."""
        await self._play_command_logic(ctx, query, 'now')

    @commands.command(name='playnext', aliases=['pnx'])
    async def playnext(self, ctx: commands.Context, *, query: str):
        """Play a song after the current one."""
        await self._play_command_logic(ctx, query, 'next')

    async def _play_command_logic(self, ctx: commands.Context, query: str, mode: str = 'normal'):
        """Shared entry point for play commands."""
        await self.handle_command_cleanup(ctx)

        if not ctx.guild:
            return

        response_channel = await self.get_response_channel(ctx)
        redirected = False

        static_channel_id = await self.guild_model.get_music_channel(ctx.guild.id)
        if static_channel_id and ctx.channel.id != static_channel_id:
            if ctx.interaction:
                # Slash command used outside static channel
                channel = self.bot.get_channel(static_channel_id)
                channel_mention = channel.mention if channel else f'#{static_channel_id}'

                msg_processed = await i18n.t(ctx, 'music.static_response.processed', channel=channel_mention)
                msg_tip = await i18n.t(ctx, 'music.static_response.tip', channel=channel_mention)

                await ctx.send(f'{msg_processed}{msg_tip}', ephemeral=True)
                redirected = True

        await self._play_logic(ctx, query, response_channel, redirected, mode=mode)

    # ===== Personal Playlist Commands =====

    @commands.hybrid_command(name='previous', aliases=['prev', 'back'])
    async def previous(self, ctx: commands.Context):
        """Play the previous song"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        if not await self.check_voice_channel(ctx):
            return

        player: CustomPlayer | None = cast(CustomPlayer, ctx.voice_client)

        if not player:
            msg = await i18n.t(ctx, 'music.commands.disconnect.not_connected')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        if not hasattr(player, 'history') or not player.history:
            msg = await i18n.t(ctx, 'music.errors.no_history')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        previous_track = player.history[-1]

        if player.queue.loop_mode == LoopMode.QUEUE:
            player.history.pop()
            await player.play(previous_track)
        else:
            if player.current:
                player.queue.put_at(0, player.current)

            player.history.pop()
            player.queue.put_at(0, previous_track)

            await player.stop()

        msg = await i18n.t(ctx, 'music.commands.previous.playing', title=previous_track.title)
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

    @commands.hybrid_command(name='skip', aliases=['s', 'next'])
    async def skip(self, ctx: commands.Context):
        """Skip the current song"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        if not await self.check_voice_channel(ctx, response_channel, redirected):
            return

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, 'music.commands.disconnect.not_connected')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        if not player.is_playing:
            msg = await i18n.t(ctx, 'music.commands.skip.nothing_playing')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        await player.stop()
        msg = await i18n.t(ctx, 'music.commands.skip.skipped')
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

    @commands.hybrid_command(name='skipto', aliases=['st', 'jumpto'])
    @app_commands.describe(position='Queue position to skip to (e.g. 3 to skip to the 3rd song)')
    async def skipto(self, ctx: commands.Context, position: int):
        """Skip to a specific song in the queue"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        if not await self.check_voice_channel(ctx, response_channel, redirected):
            return

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, 'music.commands.disconnect.not_connected')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        if not player.is_playing:
            msg = await i18n.t(ctx, 'music.commands.skip.nothing_playing')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        timeline, current_index, queue_start_index = build_queue_timeline(player)
        if position < 1 or position > len(timeline):
            msg = await i18n.t(ctx, 'music.commands.skipto.invalid_position', position=position, max=len(timeline))
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        target_index = position - 1
        target_track, _ = timeline[target_index]

        if target_index == current_index:
            target_title = target_track.title
        else:
            if player.current and player.current is not target_track:
                if not hasattr(player, 'history'):
                    player.history = []
                if not player.history or player.history[-1] is not player.current:
                    player.history.append(player.current)

            if player.queue.loop_mode == LoopMode.QUEUE:
                await player.play(target_track)
                target_title = target_track.title
            elif target_index >= queue_start_index:
                queue_index = target_index - queue_start_index
                for _ in range(queue_index):
                    player.queue.remove_at(0)

                target_track = player.queue.remove_at(0)
                target_title = target_track.title
                await player.play(target_track)
            else:
                target_title = target_track.title
                await player.play(target_track)

        msg = await i18n.t(ctx, 'music.commands.skipto.success', position=position, title=target_title)
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

    @commands.hybrid_command(name='seek')
    @app_commands.describe(position='Position to seek to (e.g. 1:30, 90, or 1:30:00)')
    async def seek(self, ctx: commands.Context, position: str):
        """Seek to a specific position in the current track"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        if not await self.check_voice_channel(ctx, response_channel, redirected):
            return

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, 'music.commands.disconnect.not_connected')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        if not player.current:
            msg = await i18n.t(ctx, 'music.commands.seek.nothing_playing')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        # Parse position - supports formats: "1:30", "90", "1:30:00"
        try:
            parts = position.split(':')
            if len(parts) == 1:
                # Just seconds
                milliseconds = int(parts[0]) * 1000
            elif len(parts) == 2:
                # Minutes:Seconds
                milliseconds = (int(parts[0]) * 60 + int(parts[1])) * 1000
            elif len(parts) == 3:
                # Hours:Minutes:Seconds
                milliseconds = (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])) * 1000
            else:
                msg = await i18n.t(ctx, 'music.commands.seek.invalid_format')
                return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
        except ValueError:
            msg = await i18n.t(ctx, 'music.commands.seek.invalid_format')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        # Check bounds
        if milliseconds < 0 or milliseconds > player.current.length:
            msg = await i18n.t(ctx, 'music.commands.seek.out_of_bounds')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        await player.seek(milliseconds)
        logger.info(f'[PLAY] User {ctx.author.id} seeked to {position} ({milliseconds}ms)')

        # Format the position for display
        total_seconds = milliseconds // 1000
        if total_seconds >= 3600:
            formatted = f'{total_seconds // 3600}:{(total_seconds % 3600) // 60:02d}:{total_seconds % 60:02d}'
        else:
            formatted = f'{total_seconds // 60}:{total_seconds % 60:02d}'

        msg = await i18n.t(ctx, 'music.commands.seek.success', position=formatted)
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

    @commands.hybrid_command(name='stop', aliases=['leave', 'dc', 'disconnect'])
    async def stop(self, ctx: commands.Context):
        """Stop playback and disconnect"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        if not await self.check_voice_channel(ctx):
            return

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, 'music.commands.disconnect.not_connected')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        try:
            await player.destroy()
        except Exception as e:
            logger.error(f'Error destroying player gracefully: {e}')
            if ctx.guild.voice_client:
                await ctx.guild.voice_client.disconnect(force=True)

        msg = await i18n.t(ctx, 'music.commands.disconnect.disconnected')
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
        await self.update_static_embed(ctx.guild.id)

    @commands.hybrid_command(name='queue', aliases=['q', 'list'])
    async def queue(self, ctx: commands.Context):
        """Show the current queue"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        player = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, 'music.commands.disconnect.not_connected')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        title = await i18n.t(ctx, 'music.commands.queue.title')
        locale = await i18n.get_locale(ctx)
        view = QueuePaginationView(player, title=title, locale=locale)

        if not view.full_playlist:
            msg = await i18n.t(ctx, 'music.commands.queue.empty')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        msg = await self.send_response(ctx, response_channel, redirected, embed=view.get_embed(), view=view)
        view.message = msg

    @commands.hybrid_command(name='shuffle')
    async def shuffle(self, ctx: commands.Context):
        """Shuffle the queue"""
        try:
            await self.handle_command_cleanup(ctx)
            response_channel = await self.get_response_channel(ctx)
            redirected = await self.acknowledge_static_redirect(ctx)

            player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
            if not player:
                msg = await i18n.t(ctx, 'music.commands.disconnect.not_connected')
                return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

            if not await self.check_voice_channel(ctx, response_channel, redirected):
                return

            if player.queue.is_empty:
                msg = await i18n.t(ctx, 'music.commands.queue.empty')
                return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

            player.queue.shuffle()
            msg = await i18n.t(ctx, 'music.commands.shuffle.success')
            await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
            await self.update_static_embed(ctx.guild.id)
        except Exception as e:
            logger.error(f'Error in shuffle command: {e}', exc_info=True)
            try:
                error_msg = await i18n.t(ctx, 'music.errors.command_error', error=str(e))
                if ctx.interaction and not ctx.interaction.response.is_done():
                    await ctx.send(error_msg, ephemeral=True)
                elif ctx.interaction:
                    await ctx.send(error_msg, delete_after=10)
                else:
                    await ctx.send(error_msg, delete_after=10)
            except Exception:
                pass

    @commands.hybrid_command(name='move')
    async def move(self, ctx: commands.Context, index_from: int, index_to: int):
        """Move a song from one position to another"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, 'music.commands.disconnect.not_connected')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        if not await self.check_voice_channel(ctx, response_channel, redirected):
            return

        try:
            _, _, queue_start_index = build_queue_timeline(player)
            queue_start_position = queue_start_index + 1
            if index_from < queue_start_position or index_to < queue_start_position:
                raise IndexError

            player.queue.move(index_from - queue_start_position, index_to - queue_start_position)
            msg = await i18n.t(ctx, 'music.commands.move.success', from_index=index_from, to_index=index_to)
            await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
            await self.update_static_embed(ctx.guild.id)
        except IndexError:
            msg = await i18n.t(ctx, 'music.commands.move.invalid_index')
            await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

    @commands.hybrid_command(name='remove')
    async def remove(self, ctx: commands.Context, index: int):
        """Remove a song from the queue"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, 'music.commands.disconnect.not_connected')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        if not await self.check_voice_channel(ctx, response_channel, redirected):
            return

        try:
            _, _, queue_start_index = build_queue_timeline(player)
            queue_start_position = queue_start_index + 1
            if index < queue_start_position:
                raise IndexError

            removed_track = player.queue.remove_at(index - queue_start_position)
            # If the removed song was the gapless-staged next track, drop the
            # orphan or it would still play at the next transition.
            await self._drop_stale_gapless_preload(player)
            msg = await i18n.t(ctx, 'music.commands.remove.success', title=removed_track.title)
            await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
            await self.update_static_embed(ctx.guild.id)
        except IndexError:
            msg = await i18n.t(ctx, 'music.commands.remove.invalid_index')
            await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

    @commands.hybrid_command(name='clear')
    async def clear(self, ctx: commands.Context):
        """Clear the queue"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, 'music.commands.disconnect.not_connected')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        if not await self.check_voice_channel(ctx, response_channel, redirected):
            return

        player.queue.clear()
        if hasattr(player, 'history'):
            player.history.clear()
        # A gapless-preloaded next track would otherwise survive the clear
        # and still play at the next transition.
        await self._drop_stale_gapless_preload(player)
        msg = await i18n.t(ctx, 'music.commands.clear.success')
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
        await self.update_static_embed(ctx.guild.id)

    @commands.hybrid_group(name='volume', aliases=['v', 'vol'])
    async def volume(self, ctx):
        """Manage volume"""
        if ctx.invoked_subcommand is None:
            await self.volume_set(ctx)

    @volume.command(name='set')
    async def volume_set(self, ctx: commands.Context, volume: int = None):
        """Set volume (0-100)"""
        if volume is None:
            if ctx.invoked_subcommand is None:
                await self.handle_command_cleanup(ctx)
                response_channel = await self.get_response_channel(ctx)
                redirected = await self.acknowledge_static_redirect(ctx)
                player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
                if not player:
                    msg = await i18n.t(ctx, 'music.commands.disconnect.not_connected')
                    return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
                msg = await i18n.t(ctx, 'music.commands.volume.current', volume=player.volume)
                return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
            return

        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        if not await self.check_voice_channel(ctx, response_channel, redirected):
            return

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, 'music.commands.disconnect.not_connected')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        if not 0 <= volume <= 100:
            msg = await i18n.t(ctx, 'music.commands.volume.invalid')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        await player.set_volume(volume)
        remember_session_volume(player, volume)
        msg = await i18n.t(ctx, 'music.commands.volume.set', volume=volume)
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
        await self.update_static_embed(ctx.guild.id)

    @volume.command(name='default')
    @commands.has_permissions(manage_guild=True)
    async def volume_default(self, ctx: commands.Context, volume: int):
        """Set the default volume for the server"""
        if not 0 <= volume <= 100:
            msg = await i18n.t(ctx, 'music.commands.volume.invalid')
            return await ctx.send(msg, delete_after=5)

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if player:
            await player.set_volume(volume)
            remember_session_volume(player, volume)

        await self.guild_model.set_default_volume(ctx.guild.id, volume)
        msg = await i18n.t(ctx, 'music.commands.volume.default_set', volume=volume)
        await ctx.send(msg, delete_after=10)

    @commands.hybrid_group(name='musicchannel')
    @require_capability('edit_configuration')
    async def musicchannel(self, ctx):
        """Manage static music channel"""
        pass

    # Hybrid groups force invoke_without_command=True, so group-level checks
    # never execute for subcommands - repeat them on each mutating subcommand.
    @musicchannel.command(name='set')
    @require_capability('edit_configuration')
    @app_commands.describe(
        channel='The text channel linking to active voice-chat players',
        message_id='Optional existing message ID to bind as the static embed',
    )
    async def musicchannel_set(self, ctx, channel: discord.TextChannel, message_id: Optional[str] = None):
        """Set the static music channel, optionally binding an existing message ID"""
        # The bot deletes every human message in the music channel, so the invoker must already be
        # allowed to manage that specific channel and its messages.
        target_perms = channel.permissions_for(ctx.author)
        if not (target_perms.view_channel and target_perms.manage_channels and target_perms.manage_messages):
            raise commands.MissingPermissions(['manage_channels', 'manage_messages'])
        old_channel_id = await self.guild_model.get_music_channel(ctx.guild.id)
        old_message_id = await self.guild_model.get_music_message(ctx.guild.id)

        await self.guild_model.set_music_channel(ctx.guild.id, channel.id)

        bound_mid = None
        if message_id:
            try:
                mid = int(message_id.strip())
                msg_obj = await channel.fetch_message(mid)
                if msg_obj.author.id == self.bot.user.id:
                    await self.guild_model.set_music_message(ctx.guild.id, msg_obj.id)
                    bound_mid = msg_obj.id
                else:
                    not_bot_msg = await i18n.t(ctx, 'music.commands.musicchannel.not_bot_message')
                    await ctx.send(not_bot_msg, ephemeral=True)
            except Exception as e:
                logger.warning(f'Could not bind message {message_id} in {channel.id}: {e}')

        # Clean up old static message if channel changed and no bound message was set
        if old_channel_id and old_message_id and old_channel_id != channel.id:
            old_ch = self.bot.get_channel(old_channel_id)
            if old_ch:
                try:
                    old_msg = await old_ch.fetch_message(old_message_id)
                    await old_msg.delete()
                except Exception:
                    pass

        if bound_mid:
            msg = await i18n.t(
                ctx,
                'music.commands.musicchannel.bound',
                message_id=str(bound_mid),
                channel=channel.mention,
            )
        else:
            msg = await i18n.t(ctx, 'music.commands.musicchannel.set', channel=channel.mention)
        await ctx.send(msg)
        await self.update_static_embed(ctx.guild.id)

    @musicchannel.command(name='bind')
    @require_capability('edit_configuration')
    @app_commands.describe(message_id='The ID of the bot message to bind as the static music embed')
    async def musicchannel_bind(self, ctx, message_id: str):
        """Bind the static music embed to an existing message ID"""
        channel_id = await self.guild_model.get_music_channel(ctx.guild.id)
        target_channel = self.bot.get_channel(channel_id) if channel_id else ctx.channel
        if not channel_id:
            await self.guild_model.set_music_channel(ctx.guild.id, target_channel.id)

        try:
            mid = int(message_id.strip())
        except (ValueError, AttributeError):
            msg = await i18n.t(ctx, 'music.commands.musicchannel.invalid_message_id')
            return await ctx.send(msg, ephemeral=True)

        target_msg = None
        try:
            target_msg = await target_channel.fetch_message(mid)
        except discord.NotFound:
            # If not found in target_channel, also try ctx.channel if different
            if ctx.channel.id != target_channel.id:
                try:
                    target_msg = await ctx.channel.fetch_message(mid)
                    target_channel = ctx.channel
                    await self.guild_model.set_music_channel(ctx.guild.id, target_channel.id)
                except Exception:
                    target_msg = None
            if not target_msg:
                msg = await i18n.t(ctx, 'music.commands.musicchannel.message_not_found', channel=target_channel.mention)
                return await ctx.send(msg, ephemeral=True)
        except Exception as e:
            logger.error(f'Error fetching message {mid} for binding: {e}')
            return await ctx.send(f'❌ Error fetching message: {e}', ephemeral=True)

        if target_msg.author.id != self.bot.user.id:
            msg = await i18n.t(ctx, 'music.commands.musicchannel.not_bot_message')
            return await ctx.send(msg, ephemeral=True)

        await self.guild_model.set_music_message(ctx.guild.id, target_msg.id)
        msg = await i18n.t(
            ctx,
            'music.commands.musicchannel.bound',
            message_id=str(target_msg.id),
            channel=target_channel.mention,
        )
        await ctx.send(msg)
        await self.update_static_embed(ctx.guild.id)

    @musicchannel.command(name='remove')
    @require_capability('edit_configuration')
    async def musicchannel_remove(self, ctx):
        """Remove the static music channel"""
        channel_id = await self.guild_model.get_music_channel(ctx.guild.id)
        message_id = await self.guild_model.get_music_message(ctx.guild.id)
        if channel_id and message_id:
            ch = self.bot.get_channel(channel_id)
            if ch:
                try:
                    old_msg = await ch.fetch_message(message_id)
                    await old_msg.delete()
                except Exception:
                    pass
        await self.guild_model.remove_music_channel(ctx.guild.id)
        msg = await i18n.t(ctx, 'music.commands.musicchannel.removed')
        await ctx.send(msg)

    @commands.hybrid_command(name='247', aliases=['24/7'])
    async def twenty_four_seven(self, ctx: commands.Context):
        """Toggle 24/7 mode"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, 'music.commands.disconnect.not_connected')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        if not await self.check_voice_channel(ctx, response_channel, redirected):
            return

        player.twenty_four_seven = not player.twenty_four_seven
        await self.guild_model.set_247_enabled(ctx.guild.id, player.twenty_four_seven)

        if not player.twenty_four_seven:
            if player.queue.is_empty and not player.is_playing:
                self.start_timeout(ctx.guild.id, player)

            elif len(player.channel.members) == 1:
                self.start_timeout(ctx.guild.id, player)
        else:
            self.cancel_timeout(ctx.guild.id)

        status = 'enabled' if player.twenty_four_seven else 'disabled'
        msg = await i18n.t(ctx, f'music.commands.247.{status}')
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
        await self.update_static_embed(ctx.guild.id)

    @commands.hybrid_command(name='autoplay')
    async def autoplay(self, ctx: commands.Context):
        """Toggle Autoplay mode"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, 'music.commands.disconnect.not_connected')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        if not await self.check_voice_channel(ctx, response_channel, redirected):
            return

        if getattr(player, 'autoplay_enabled', False):
            player.autoplay_enabled = False
            status = 'disabled'
            self._clear_autoplay_prefetch(ctx.guild.id)
        else:
            player.autoplay_enabled = True
            status = 'enabled'
            try:
                if getattr(player, 'current', None) is not None:
                    self._schedule_autoplay_prefetch(player, player.current)
            except Exception:
                pass

        await self.guild_model.set_autoplay_enabled(ctx.guild.id, player.autoplay_enabled)
        msg = await i18n.t(ctx, f'music.commands.autoplay.{status}')
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
        await self.update_static_embed(ctx.guild.id)

    @commands.hybrid_command(name='autoplayblock', aliases=['apblock'])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def autoplay_block(self, ctx: commands.Context):
        """Block the current track's YouTube channel from server autoplay."""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)
        player = ctx.voice_client

        async def respond(key, **params):
            message = await i18n.t(ctx, f'music.commands.autoplay_block.{key}', **params)
            return await self.send_response(ctx, response_channel, redirected, content=message, delete_after=15)

        if not player or not player.current:
            return await respond('nothing_playing')
        if not await self.check_voice_channel(ctx, response_channel, redirected):
            return
        track = player.current
        channel_id = track_channel_id(track)
        if channel_id is None:
            # Tracks queued before the metadata update can be reloaded by their original URL.
            try:
                source = getattr(track, 'original', None) or track
                loaded = await player.get_tracks(source.uri)
                tracks = loaded.tracks if isinstance(loaded, lava_lyra.Playlist) else loaded
                match = next((item for item in tracks or [] if item.identifier == source.identifier), None)
                channel_id = track_channel_id(match)
                if channel_id and isinstance(getattr(source, 'info', None), dict):
                    source.info['channelId'] = channel_id
            except Exception:
                logger.debug('Could not reload uploader metadata', exc_info=True)
        if channel_id is None:
            return await respond('unavailable')

        settings = await self.guild_model.get_autoplay_settings(ctx.guild.id)
        config = settings['config']
        blocked = list(config['channel_blacklist'])
        if channel_id in blocked:
            return await respond('already_blocked', channel_id=channel_id)
        if len(blocked) >= 100:
            return await respond('full')
        config = {**config, 'channel_blacklist': [*blocked, channel_id]}
        await self.guild_model.set_autoplay_custom_config(ctx.guild.id, config)

        from runtime.bots import all_bots

        bots = {id(bot): bot for bot in [self.bot, *all_bots]}
        for bot in bots.values():
            for active_player in bot.voice_clients:
                if active_player.guild.id != ctx.guild.id:
                    continue
                cog = bot.get_cog('Music')
                if cog:
                    active_player.autoplay_config = config.copy()
                    cog._clear_autoplay_prefetch(ctx.guild.id)
                    if active_player.current is not None:
                        cog._schedule_autoplay_prefetch(active_player, active_player.current)
        await respond('added', channel_id=channel_id)

    @commands.hybrid_command(name='lyrics')
    async def lyrics(self, ctx: commands.Context):
        """Show lyrics for the current song"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player or not player.current:
            msg = await i18n.t(ctx, 'music.commands.lyrics.nothing_playing')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        try:
            lyrics_data = await player.fetch_lyrics()
        except Exception as e:
            logger.warning(f'[LYRICS] Failed to fetch: {e}')
            lyrics_data = None

        if not lyrics_data:
            msg = await i18n.t(ctx, 'music.commands.lyrics.not_found')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        track_title = player.current.title
        track_author = player.current.author

        if lyrics_data.lines:
            lines_text = []
            for line in lyrics_data.lines:
                if lyrics_data.synced:
                    mins = int(line.time // 60)
                    secs = int(line.time % 60)
                    lines_text.append(f'`{mins:02d}:{secs:02d}` {line.text}')
                else:
                    lines_text.append(line.text)
            full_text = '\n'.join(lines_text)
        elif lyrics_data.text:
            full_text = lyrics_data.text
        else:
            msg = await i18n.t(ctx, 'music.commands.lyrics.not_found')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        provider = lyrics_data.provider or lyrics_data.name or 'Unknown'
        embed = discord.Embed(
            title=f'🎤 {track_title}',
            description=full_text[:4096],
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f'Provider: {provider} | {track_author}')
        await self.send_response(ctx, response_channel, redirected, embed=embed)

    @commands.hybrid_group(name='lastfm')
    async def lastfm(self, ctx):
        """Last.fm integration"""
        pass

    @lastfm.command(name='login')
    async def lastfm_login(self, ctx):
        """Login to Last.fm"""
        url, token = await lastfm_handler.get_auth_data()
        if not url or not token:
            msg = await i18n.t(ctx, 'music.lastfm.not_configured')
            return await ctx.send(msg, delete_after=10)

        locale = await i18n.get_locale(ctx)
        view = LastFMAuthView(ctx.author.id, url, token, self.user_model, locale=locale)
        msg = await i18n.t(ctx, 'music.lastfm.authorize_prompt')
        # The authorize link carries a bearer token that any Last.fm user can approve, so it must only
        # ever reach the invoker: ephemeral for slash commands, a DM for prefix commands.
        if ctx.interaction is not None:
            await ctx.send(msg, view=view, ephemeral=True)
            return
        try:
            await ctx.author.send(msg, view=view)
        except discord.HTTPException:
            return await ctx.send(await i18n.t(ctx, 'music.lastfm.dm_failed'), delete_after=10)
        await ctx.send(await i18n.t(ctx, 'music.lastfm.dm_sent'), delete_after=10)

    @lastfm.command(name='info')
    async def lastfm_info(self, ctx):
        """Check your Last.fm status"""
        user_data = await self.user_model.get_user(ctx.author.id)
        if not user_data or 'lastfm' not in user_data:
            msg = await i18n.t(ctx, 'music.lastfm.unlink.not_linked')
            return await ctx.send(msg, delete_after=10)

        lfm = user_data['lastfm']
        username = lfm.get('username') or await i18n.t(ctx, 'general.unknown')
        scrobbling = lfm.get('scrobbling', True)

        status_text = (
            await i18n.t(ctx, 'music.lastfm.scrobbling_enabled')
            if scrobbling
            else await i18n.t(ctx, 'music.lastfm.scrobbling_disabled')
        )
        msg = await i18n.t(ctx, 'music.lastfm.info_status', username=username, status=status_text)
        await ctx.send(msg, delete_after=10)

    @lastfm.command(name='logout')
    async def lastfm_logout(self, ctx):
        """Unlink your Last.fm account"""
        await self.user_model.remove_lastfm(ctx.author.id)
        msg = await i18n.t(ctx, 'music.lastfm.logout_success')
        await ctx.send(msg, delete_after=10)

    @lastfm.command(name='scrobble')
    @app_commands.describe(state='Enable or disable scrobbling (on/off)')
    @app_commands.choices(
        state=[app_commands.Choice(name='On', value='on'), app_commands.Choice(name='Off', value='off')]
    )
    async def lastfm_scrobble(self, ctx, state: app_commands.Choice[str]):
        """Toggle scrobbling on/off"""
        user_data = await self.user_model.get_user(ctx.author.id)
        if not user_data or 'lastfm' not in user_data:
            msg = await i18n.t(ctx, 'music.lastfm.login_first')
            return await ctx.send(msg)

        enabled = state.value == 'on'
        await self.user_model.toggle_lastfm_scrobbling(ctx.author.id, enabled)

        status_text = 'enabled' if enabled else 'disabled'
        msg = await i18n.t(ctx, 'music.lastfm.scrobble_toggled', status=status_text)
        await ctx.send(msg)


__all__ = [
    'Music',
    'build_queue_timeline',
    'get_track_artwork',
    'IdlePlaylistView',
    'LastFMAuthView',
    'NowPlayingView',
    'PlayerSettingsSelect',
    'PlaylistSaveSelect',
    'PlaylistSelect',
    'QueuePaginationView',
]


async def setup(bot):
    await bot.add_cog(Music(bot))
