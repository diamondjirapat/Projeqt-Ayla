"""Presentation behavior extracted from the music cog."""

import asyncio
import logging
from typing import Optional
import discord
import lava_lyra
from config import Config
from utils.i18n import i18n
from utils.queue import (
    LoopMode,
)
from utils.artwork import artwork_contract
from utils.autoplay import clean_artist_name as clean_artist_name
from utils.autoplay import normalize_core_title as normalize_core_title
from utils.music_embed import (
    build_custom_music_embed,
    build_music_channel_embed,
    is_discord_attachment_expired,
    is_http_url,
    refresh_music_embed_config_attachments,
    resolve_music_banner_url_async,
    resolve_music_bar_url_async,
)
from utils.music_views import (
    IdlePlaylistView,
    NowPlayingView,
)

logger = logging.getLogger(__name__)


def build_queue_timeline(player):
    history = list(player.history) if hasattr(player, 'history') else []
    current = [player.current] if player.current else []
    queue = player.queue.to_list()

    history_ids = {id(track) for track in history}
    timeline = [(track, 'Played') for track in history]

    if current:
        timeline.append((current[0], 'Now'))

    for track in queue:
        status = 'Loop' if player.queue.loop_mode == LoopMode.QUEUE and id(track) in history_ids else 'Next'
        timeline.append((track, status))

    current_index = len(history) if current else -1
    queue_start_index = len(history) + len(current)
    return timeline, current_index, queue_start_index


def get_track_artwork(track: Optional[lava_lyra.Track]) -> Optional[str]:
    if not track:
        return None
    return artwork_contract(track)['artwork']


class MusicPresentation:
    def _get_static_embed_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._static_embed_locks:
            self._static_embed_locks[guild_id] = asyncio.Lock()
        return self._static_embed_locks[guild_id]

    async def update_static_embed(self, guild_id: int):
        """Refresh this bot's voice player, then the guild's shared directory."""
        try:
            await self.update_now_playing_embed(guild_id)
        except Exception as e:
            logger.error(f'Failed to update voice player for guild {guild_id}: {e}', exc_info=True)
        try:
            await self._update_static_embed(guild_id)
        except Exception as e:
            logger.error(f'Failed to update music directory for guild {guild_id}: {e}', exc_info=True)

    async def update_now_playing_embed(self, guild_id: int):
        """Keep one control message in this bot's current voice channel chat."""
        async with self._get_static_embed_lock(guild_id):
            guild = self.bot.get_guild(guild_id)
            player = guild.voice_client if guild else None
            if not player or not player.is_connected or not player.channel:
                await self._delete_voice_music_message(guild_id)
                return
            self._voice_player_sessions[guild_id] = player
            await self._update_voice_embed_inner(guild_id, guild, player, player.channel)

    async def _delete_voice_music_message(self, guild_id: int):
        message = self.now_playing_messages.get(guild_id)
        binding = {'channel_id': message.channel.id, 'message_id': message.id} if message else await self.guild_model.get_voice_music_message(guild_id, self.bot.user.id)
        if message is None and binding:
            channel = self.bot.get_channel(binding['channel_id'])
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(binding['channel_id'])
                except discord.NotFound:
                    channel = None
            try:
                message = await channel.fetch_message(binding['message_id']) if channel else None
            except discord.NotFound:
                pass
        if message is not None:
            if message.author.id != self.bot.user.id:
                raise RuntimeError('Voice player message belongs to another bot')
            try:
                await message.delete()
            except discord.NotFound:
                pass
        view = self._voice_player_views.pop(guild_id, None)
        if view:
            view.stop()
        if binding:
            await self.guild_model.clear_voice_music_message(guild_id, self.bot.user.id, binding['message_id'])
        self.now_playing_messages.pop(guild_id, None)

    async def player_disconnected(self, player):
        """Tear down presentation only for the session that actually disconnected."""
        guild_id = player.guild.id
        async with self._get_static_embed_lock(guild_id):
            session = self._voice_player_sessions.get(guild_id)
            if session is not None and session is not player:
                return
            self.cancel_timeout(guild_id)
            self._clear_autoplay_prefetch(guild_id)
            await self._delete_voice_music_message(guild_id)
            self._voice_player_sessions.pop(guild_id, None)
        await self._update_static_embed(guild_id)

    async def _update_voice_embed_inner(self, guild_id, guild, player, channel):
        binding = await self.guild_model.get_voice_music_message(guild_id, self.bot.user.id)
        message = self.now_playing_messages.get(guild_id)
        if (binding and binding['channel_id'] != channel.id) or (message and message.channel.id != channel.id):
            # A failed deletion aborts the move, so we never create a second player.
            await self._delete_voice_music_message(guild_id)
            binding = None
            message = None
        if message is None and binding:
            try:
                message = await channel.fetch_message(binding['message_id'])
            except discord.NotFound:
                await self.guild_model.clear_voice_music_message(guild_id, self.bot.user.id, binding['message_id'])
                binding = None
            # Permission errors and transient fetch errors must never cause a resend.
        if message is not None and message.author.id != self.bot.user.id:
            raise RuntimeError('Voice player message belongs to another bot')

        if message is None:
            # A stable marker recovers our own message after an interrupted save.
            # Never adopt arbitrary embeds or another bot's controls from history.
            found = []
            async for candidate in channel.history(limit=100):
                if candidate.author.id == self.bot.user.id and any(str(getattr(component, 'custom_id', '')).startswith('voice_music:') for row in candidate.components for component in row.children):
                    found.append(candidate)
            if found:
                message = found[0]
                self.now_playing_messages[guild_id] = message
                for duplicate in found[1:]:
                    try:
                        await duplicate.delete()
                    except discord.NotFound:
                        pass

        locale = await i18n.get_guild_locale(guild_id) or 'en'

        is_connected = bool(player and player.is_connected)
        # NB: CustomPlayer.is_playing is False while paused, but a paused track is still loaded:
        # keep the now-playing embed (with Resume) instead of wiping to idle.
        is_playing = bool(player and (player.is_playing or player.current is not None))
        current_track = player.current if player else None

        # Fetch custom embed config
        music_embed_config = await self.guild_model.get_music_embed_config(guild_id)
        if isinstance(music_embed_config, dict):
            await refresh_music_embed_config_attachments(music_embed_config)

        # Build placeholders dictionary
        prefix = '!'
        if hasattr(self.bot, 'get_prefix'):
            try:
                p = await self.bot.get_prefix(channel)
                if isinstance(p, (list, tuple)):
                    prefix = p[0] if p else '!'
                elif p:
                    prefix = str(p)
            except Exception:
                prefix = '!'

        channel_name = channel.name if hasattr(channel, 'name') and channel.name else ''
        channel_mention = channel.mention if hasattr(channel, 'mention') and channel.mention else (f'#{channel_name}' if channel_name else '')

        guild_name = guild.name if guild and getattr(guild, 'name', None) else ''
        server_icon = ''
        if guild and getattr(guild, 'icon', None):
            server_icon = str(guild.icon.url)

        bot_name = self.bot.user.name if self.bot.user and getattr(self.bot.user, 'name', None) else 'Projeqt Ayla'
        bot_avatar = ''
        if self.bot.user:
            display_avatar = getattr(self.bot.user, 'display_avatar', None)
            if display_avatar and hasattr(display_avatar, 'url'):
                bot_avatar = str(display_avatar.url)
            elif hasattr(self.bot.user, 'avatar') and self.bot.user.avatar:
                bot_avatar = str(self.bot.user.avatar.url)

        banner_url = await resolve_music_banner_url_async(getattr(Config, 'MUSIC_BANNER_URL', ''))
        if not banner_url or is_discord_attachment_expired(banner_url):
            banner_url = '/music-banner.png'
        if banner_url.startswith('/') and getattr(Config, 'WEB_URL', None):
            banner_url = f'{Config.WEB_URL.rstrip("/")}{banner_url}'

        bar_url = await resolve_music_bar_url_async(getattr(Config, 'BAR_URL', ''))
        if not bar_url or is_discord_attachment_expired(bar_url):
            bar_url = '/bar444.png'
        if bar_url.startswith('/') and getattr(Config, 'WEB_URL', None):
            bar_url = f'{Config.WEB_URL.rstrip("/")}{bar_url}'
        unknown_user = 'Unknown'

        loop_mode_str = 'Off'
        if player and hasattr(player, 'queue') and hasattr(player.queue, 'loop_mode'):
            loop_mode_val = player.queue.loop_mode
            loop_mode_str = getattr(loop_mode_val, 'name', str(loop_mode_val)).capitalize()

        queue_len_str = str(player.queue.count) if player and hasattr(player, 'queue') and hasattr(player.queue, 'count') else '0'
        volume_str = str(getattr(player, 'volume', 100)) if player else '100'

        placeholders = {
            'server': guild_name,
            'guild': guild_name,
            'server_icon': server_icon,
            'channel': channel_name,
            'channel_mention': channel_mention,
            'prefix': prefix,
            'bot_name': bot_name,
            'bot_avatar': bot_avatar,
            'banner': banner_url,
            'banner_url': banner_url,
            'music_banner': banner_url,
            'music_banner_url': banner_url,
            'bar': bar_url,
            'bar_url': bar_url,
            'title': '',
            'artist': '',
            'author': '',
            'duration': '',
            'url': '',
            'uri': '',
            'source': '',
            'requester': '',
            'requester_mention': '',
            'volume': volume_str,
            'loop_mode': loop_mode_str,
            'queue_length': queue_len_str,
            'artwork': '',
            'thumbnail': '',
        }

        if is_connected and is_playing and current_track:
            track_len = getattr(current_track, 'length', 0) or 0
            duration_str = f'{track_len // 1000 // 60}:{track_len // 1000 % 60:02d}'
            track_uri = current_track.uri or ''
            track_source = 'Spotify' if ('spotify' in track_uri.lower()) else 'YouTube'
            requester_val = getattr(current_track, 'requester', None) or unknown_user
            requester_mention_val = getattr(current_track, 'requester_mention', None) or getattr(current_track, 'requester', None) or ''
            artwork_url = get_track_artwork(current_track) or ''

            placeholders.update(
                {
                    'title': current_track.title or '',
                    'artist': current_track.author or '',
                    'author': current_track.author or '',
                    'duration': duration_str,
                    'url': track_uri,
                    'uri': track_uri,
                    'source': track_source,
                    'requester': str(requester_val),
                    'requester_mention': str(requester_mention_val),
                    'volume': volume_str,
                    'loop_mode': loop_mode_str,
                    'queue_length': queue_len_str,
                    'artwork': artwork_url,
                    'thumbnail': artwork_url,
                }
            )

        embed = None
        content = None

        # Custom music embeds always apply (the former opt-in flag was removed).
        if isinstance(music_embed_config, dict):
            try:
                state = 'now_playing' if (is_connected and is_playing and current_track) else 'idle'
                embed, content = build_custom_music_embed(guild_id, state, music_embed_config, placeholders)
            except Exception as e:
                logger.warning(
                    f'Failed to build custom music embed for guild {guild_id}, falling back to default: {e}',
                    exc_info=True,
                )
                embed = None
                content = None

        if embed is None:
            content = None
            embed = discord.Embed(color=discord.Color.blurple())

            if not is_connected:
                embed.title = await i18n.t(channel, 'music.player.idle.title', static_embed=True)
                embed.description = await i18n.t(channel, 'music.player.idle.description_disconnect', static_embed=True)
                embed.set_image(url=Config.MUSIC_BANNER_URL)

            elif not is_playing or not current_track:
                embed.title = await i18n.t(channel, 'music.player.idle.title', static_embed=True)
                embed.description = await i18n.t(channel, 'music.player.idle.description_empty', static_embed=True)
                embed.set_image(url=Config.MUSIC_BANNER_URL)
            else:
                now_playing_title = await i18n.t(
                    channel,
                    'music.player.now_playing',
                    static_embed=True,
                    title=current_track.title,
                )
                artist_label = await i18n.t(
                    channel,
                    'music.player.artist',
                    artist=current_track.author,
                    static_embed=True,
                )
                duration_label = await i18n.t(
                    channel,
                    'music.player.duration_label',
                    duration=f'{current_track.length // 1000 // 60}:{current_track.length // 1000 % 60:02d}',
                    static_embed=True,
                )

                # Detect Spotify source
                is_spotify = current_track.uri and 'spotify' in current_track.uri.lower()
                if is_spotify:
                    embed.color = discord.Color.green()
                    source_indicator = 'Spotify\n'
                else:
                    embed.color = discord.Color.blurple()
                    source_indicator = 'YouTube\n'

                embed.title = now_playing_title
                embed.description = f'{source_indicator}**[{current_track.title}]({current_track.uri})**\n\n{artist_label}\n{duration_label}'
                artwork = get_track_artwork(current_track)
                if is_http_url(artwork):
                    embed.set_thumbnail(url=artwork)
                if hasattr(current_track, 'requester') and current_track.requester:
                    req_text = await i18n.t(channel, 'music.player.requester_label', user=current_track.requester, static_embed=True)
                    embed.add_field(name=' ', value=req_text)
                    embed.set_image(url=Config.BAR_URL.strip()) if is_http_url(Config.BAR_URL) else None

        # Resolve the view after rendering, and replace the previous callbacks.
        if is_connected and is_playing and current_track:
            view = NowPlayingView(player, self.user_model, self.guild_model, locale=locale)
            await view.async_init()
        else:
            view = IdlePlaylistView(guild_id, self.user_model, self.guild_model, self.bot, locale=locale)
        for item in view.children:
            if getattr(item, 'custom_id', None):
                item.custom_id = f'voice_music:{item.custom_id}'

        # Voice moves/disconnects can happen while artwork/configuration is loading.
        if guild.voice_client is not player or not player.is_connected or not player.channel or player.channel.id != channel.id:
            view.stop()
            return
        try:
            if message is None:
                message = await channel.send(content=content, embed=embed, view=view)
                # Retain the reference even if persistence fails; do not blindly resend.
                self.now_playing_messages[guild_id] = message
            else:
                await message.edit(content=content, embed=embed, view=view)
                self.now_playing_messages[guild_id] = message
            if not binding or binding['channel_id'] != channel.id or binding['message_id'] != message.id:
                await self.guild_model.set_voice_music_message(guild_id, self.bot.user.id, channel.id, message.id)
        except discord.NotFound:
            self.now_playing_messages.pop(guild_id, None)
            await self.guild_model.clear_voice_music_message(guild_id, self.bot.user.id, message.id)
            view.stop()
            return
        except Exception:
            view.stop()
            raise
        old_view = self._voice_player_views.get(guild_id)
        if old_view:
            old_view.stop()
        self._voice_player_views[guild_id] = view

    async def _update_static_embed(self, guild_id: int):
        """A single bot owns the directory; every bot still owns its voice player."""
        from runtime.bots import all_bots

        owners = [bot for bot in all_bots if bot.get_guild(guild_id) is not None and bot.get_cog('Music')]
        owner = owners[0] if owners else self.bot
        if owner is not self.bot:
            await owner.get_cog('Music')._update_static_embed(guild_id)
            return
        async with self._get_static_embed_lock(guild_id):
            await self._update_static_embed_inner(guild_id)

    async def _update_static_embed_inner(self, guild_id: int):
        from runtime.bots import all_bots

        channel_id = await self.guild_model.get_music_channel(guild_id)
        cached = self._music_directory_messages.get(guild_id)
        if cached and cached.channel.id != channel_id:
            try:
                await cached.delete()
            except discord.NotFound:
                pass
            self._music_directory_messages.pop(guild_id, None)
            cached = None
        if not channel_id:
            return
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        locale = await i18n.get_guild_locale(guild_id) or 'en'
        bots = all_bots or [self.bot]
        links = []
        for bot in bots:
            guild = bot.get_guild(guild_id)
            player = guild.voice_client if guild else None
            if not player or not player.is_connected or not player.channel:
                continue
            cog = bot.get_cog('Music')
            message = cog.now_playing_messages.get(guild_id) if cog else None
            url = f'https://discord.com/channels/{guild_id}/{player.channel.id}/{message.id}' if message and message.channel.id == player.channel.id else f'https://discord.com/channels/{guild_id}/{player.channel.id}'
            links.append(i18n.get_text('music.player.directory.entry', locale, bot=bot.user.name, channel=player.channel.mention, url=url))
        guild = channel.guild
        config = await self.guild_model.get_music_channel_embed_config(guild_id)
        await refresh_music_embed_config_attachments(config)
        banner = await resolve_music_banner_url_async(getattr(Config, 'MUSIC_BANNER_URL', ''))
        bar = await resolve_music_bar_url_async(getattr(Config, 'BAR_URL', ''))
        embed, content = build_music_channel_embed(
            guild_id,
            config,
            {
                'directory_title': i18n.get_text('music.player.directory.title', locale),
                'directory_description': i18n.get_text('music.player.directory.description', locale),
                'players': '\n'.join(links) or i18n.get_text('music.player.directory.empty', locale),
                'active_player_count': str(len(links)),
                'server': guild.name,
                'guild': guild.name,
                'server_icon': str(guild.icon.url) if guild.icon else '',
                'channel': channel.name,
                'channel_mention': channel.mention,
                'bot_name': self.bot.user.name,
                'bot_avatar': str(self.bot.user.display_avatar.url),
                'banner': banner,
                'banner_url': banner,
                'music_banner': banner,
                'music_banner_url': banner,
                'bar': bar,
                'bar_url': bar,
            },
        )
        view_bot = self.bot
        message_id = await self.guild_model.get_music_message(guild_id)
        message = cached if cached and (not message_id or cached.id == message_id) else None
        if message is None and message_id:
            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                await self.guild_model.set_music_message(guild_id, 0)
            if message and message.author.id != self.bot.user.id:
                # Preserve an existing directory created by another managed bot.
                view_bot = next((bot for bot in bots if bot.user.id == message.author.id), None)
                if view_bot is None:
                    logger.warning('Music directory %s belongs to an unmanaged author', message_id)
                    return
                owner_channel = view_bot.get_channel(channel_id) or await view_bot.fetch_channel(channel_id)
                message = await owner_channel.fetch_message(message_id)
        if message is None:
            managed_ids = {bot.user.id for bot in bots}
            found = []
            async for candidate in channel.history(limit=100):
                if candidate.author.id in managed_ids and any(str(getattr(component, 'custom_id', '')).startswith('music_directory:') for row in candidate.components for component in row.children):
                    found.append(candidate)
            if found:
                message = found[0]
                view_bot = next(bot for bot in bots if bot.user.id == message.author.id)
                owner_channel = view_bot.get_channel(channel_id) or await view_bot.fetch_channel(channel_id)
                message = await owner_channel.fetch_message(message.id)
                for duplicate in found[1:]:
                    duplicate_bot = next(bot for bot in bots if bot.user.id == duplicate.author.id)
                    duplicate_channel = duplicate_bot.get_channel(channel_id) or await duplicate_bot.fetch_channel(channel_id)
                    try:
                        duplicate = await duplicate_channel.fetch_message(duplicate.id)
                        await duplicate.delete()
                    except discord.NotFound:
                        pass
        view = IdlePlaylistView(guild_id, self.user_model, self.guild_model, view_bot, locale=locale)
        for item in view.children:
            if getattr(item, 'custom_id', None):
                item.custom_id = f'music_directory:{item.custom_id}'
        if message:
            try:
                await message.edit(content=content, embed=embed, view=view)
            except discord.NotFound:
                self._music_directory_messages.pop(guild_id, None)
                await self.guild_model.set_music_message(guild_id, 0)
                view.stop()
                return
        else:
            message = await channel.send(content=content, embed=embed, view=view)
        self._music_directory_messages[guild_id] = message
        if message_id != message.id:
            await self.guild_model.set_music_message(guild_id, message.id)
