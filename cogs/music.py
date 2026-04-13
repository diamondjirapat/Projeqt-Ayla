import asyncio
import logging
import re
import time
from datetime import datetime
from typing import cast, Optional
from urllib.parse import urlparse

import discord
import pomice
from discord import app_commands
from discord.ext import commands

from config import Config
from database.models import UserModel, GuildModel
from utils.i18n import i18n
from utils.lastfm import lastfm_handler
from utils.queue import CustomPlayer

logger = logging.getLogger(__name__)

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.user_model = UserModel()
        self.guild_model = GuildModel()
        self.timeout_tasks = {}
        self.selecting_users = set()
        self.autoplay_played_uris = {}  # guild_id -> set of played URIs to avoid repeats
        self.now_playing_messages = {}  # guild_id -> message reference for cleanup
    
    async def cog_before_invoke(self, ctx: commands.Context):
        """Automatically defer slash commands to prevent timeout"""
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        """Global error handler for the music cog to catch node and session errors gracefully."""
        original = getattr(error, 'original', error)
        err_str = str(original)

        if "Session not found" in err_str:
            if ctx.guild and ctx.guild.voice_client:
                try:
                    await ctx.guild.voice_client.disconnect(force=True)
                except:
                    pass
            try:
                msg = await i18n.t(ctx, "music.errors.session_died")
                if ctx.interaction and not ctx.interaction.response.is_done():
                    await ctx.send(msg, ephemeral=True)
                else:
                    await ctx.send(msg, delete_after=10)
            except:
                pass
            return
            
        if "refused" in err_str.lower() or "connect" in err_str.lower():
            if ctx.guild and ctx.guild.voice_client:
                try:
                    await ctx.guild.voice_client.disconnect(force=True)
                except:
                    pass
            try:
                msg = await i18n.t(ctx, "music.errors.node_offline")
                if ctx.interaction and not ctx.interaction.response.is_done():
                    await ctx.send(msg, ephemeral=True)
                else:
                    await ctx.send(msg, delete_after=10)
            except:
                pass
            return
            
        logger.error(f"Unhandled command error in Music cog: {error}")

    def start_timeout(self, guild_id, player):
        """Starts a 3-minute disconnect timer"""
        if guild_id in self.timeout_tasks:
            self.timeout_tasks[guild_id].cancel()
        
        # Bypass 24/7
        if hasattr(player, "twenty_four_seven") and player.twenty_four_seven:
            return

        async def _timeout():
            await asyncio.sleep(180)
            if player:
                await player.disconnect()

        self.timeout_tasks[guild_id] = self.bot.loop.create_task(_timeout())

    async def _fetch_autoplay_track(self, player: pomice.Player, last_track: pomice.Track) -> pomice.Track | None:
        """Fetch a recommended track based on the last played track's artist"""
        guild_id = player.guild.id

        if guild_id not in self.autoplay_played_uris:
            self.autoplay_played_uris[guild_id] = set()

        if last_track.uri:
            self.autoplay_played_uris[guild_id].add(last_track.uri)

        if len(self.autoplay_played_uris[guild_id]) > 50:
            self.autoplay_played_uris[guild_id] = set(list(self.autoplay_played_uris[guild_id])[-50:])

        query = f"{last_track.author}"
        logger.info(f"[AUTOPLAY] Searching for tracks by: {query}")

        try:
            tracks = await player.get_tracks(query)
            if not tracks:
                logger.info("[AUTOPLAY] No tracks found")
                return None

            if isinstance(tracks, pomice.Playlist):
                tracks = tracks.tracks

            for track in tracks:
                if track.uri and track.uri in self.autoplay_played_uris[guild_id]:
                    continue
                if track.title.lower() == last_track.title.lower():
                    continue
                track.requester = "AutoPlay 🎵"
                logger.info(f"[AUTOPLAY] Selected: '{track.title}' by {track.author}")
                return track
            
            return None
        except Exception as e:
            logger.warning(f"[AUTOPLAY] Search failed: {e}")
            return None

    def cancel_timeout(self, guild_id):
        """Cancels the disconnect timer"""
        if guild_id in self.timeout_tasks:
            self.timeout_tasks[guild_id].cancel()
            del self.timeout_tasks[guild_id]

    async def cog_load(self):
        self.pomice_pool = pomice.NodePool()
        
        # Monkey patch pomice.Node.connect to silence unretrieved task exceptions from its internal reconnect attempts. 
        if not getattr(pomice.Node, "_patched_connect", False):
            original_connect = pomice.Node.connect
            
            async def safe_connect(node_self, *args, **kwargs):
                try:
                    return await original_connect(node_self, *args, **kwargs)
                except Exception as e:
                    if type(e).__name__ == "NodeConnectionFailure" and kwargs.get("reconnect", False):
                        logger.debug(f"[POMICE] Suppressed internal reconnect error: {e}")
                        return node_self
                    raise
                    
            pomice.Node._patched_connect = True
            pomice.Node.connect = safe_connect

        self.bot.loop.create_task(self._connect_lavalink())

    async def _connect_lavalink(self):
        """Connect to Lavalink after bot is ready with automatic retry on failure."""
        await self.bot.wait_until_ready()
        parsed = urlparse(Config.LAVALINK_URI)
        host = parsed.hostname or '127.0.0.1'
        port = parsed.port or 2333

        backoff = 5 
        max_backoff = 300 
        attempt = 0

        while True:
            attempt += 1
            logger.info(f"[LAVALINK] Connection attempt #{attempt} to {host}:{port}")
            try:
                try:
                    existing = self.pomice_pool.get_node(identifier='MAIN')
                    if existing:
                        await existing.disconnect()
                except Exception:
                    pass

                await self.pomice_pool.create_node(
                    bot=self.bot,
                    host=host,
                    port=port,
                    password=Config.LAVALINK_PASSWORD,
                    identifier='MAIN',
                )
                logger.info("[LAVALINK] Connection pool initialized successfully")
                return
            except Exception as e:
                logger.error(f"[LAVALINK] Failed to connect (attempt #{attempt}): {e}")
                logger.info(f"[LAVALINK] Retrying in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def check_voice_channel(self, ctx: commands.Context, response_channel=None, redirected: bool = False):
        """Check if the user is in the same voice channel as the bot.
        Returns True if OK, or sends an error message and returns False.
        """
        if not ctx.voice_client:
            return True

        if not ctx.author.voice or ctx.author.voice.channel != ctx.voice_client.channel:
            msg = await i18n.t(ctx, "music.errors.voice_channel_mismatch")
            if response_channel:
                await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
            else:
                await ctx.send(msg, ephemeral=True, delete_after=5)
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
            except:
                pass

    async def acknowledge_static_redirect(self, ctx: commands.Context) -> bool:
        """
        For slash commands with a static channel configured, acknowledge the interaction.
        Returns True if acknowledgment was sent, False otherwise.
        """
        static_channel_id = await self.guild_model.get_music_channel(ctx.guild.id)
        if ctx.interaction and static_channel_id and ctx.channel.id != static_channel_id:
            channel = self.bot.get_channel(static_channel_id)
            channel_mention = channel.mention if channel else f"#{static_channel_id}"
            msg_processed = await i18n.t(ctx, "music.static_response.processed", channel=channel_mention)
            await ctx.send(msg_processed, ephemeral=True)
            return True
        return False

    async def send_response(self, ctx: commands.Context, response_channel, redirected: bool, content=None, embed=None, view=None, delete_after=None):
        """
        Send a response to the proper channel based on context.
        For slash commands without redirect, uses ctx.send to respond to the interaction.
        Otherwise, sends to the response_channel.
        """
        if ctx.interaction and not redirected:
            return await ctx.send(content=content, embed=embed, view=view, delete_after=delete_after)
        return await response_channel.send(content=content, embed=embed, view=view, delete_after=delete_after)

    # Node ready is handled by pomice.NodePool.create_node()

    @commands.Cog.listener()
    async def on_pomice_track_start(self, player: pomice.Player, track: pomice.Track):
        logger.info(f"[LASTFM DEBUG] Track START event fired: {track.title}")

        if not player:
            return

        player.current_track_start_time = int(time.time())
        self.cancel_timeout(player.guild.id)

        await self.update_static_embed(player.guild.id)

        static_channel_id = await self.guild_model.get_music_channel(player.guild.id)

        target_channel = None
        if hasattr(player, "home_channel") and player.home_channel:
            target_channel = player.home_channel

        if target_channel and static_channel_id:
            if target_channel.id == static_channel_id:
                return

        if not target_channel:
            target_channel = player.guild.system_channel
            if not target_channel and player.guild.text_channels:
                target_channel = player.guild.text_channels[0]

        if target_channel:
            if not hasattr(player, "history"):
                player.history = []
            position = len(player.history) + 1
            title = await i18n.t(target_channel, "music.player.now_playing", static_embed=True, position=position, title=track.title)
            
            # Detect Spotify
            is_spotify = track.uri and "spotify" in track.uri.lower()
            embed_color = discord.Color.green() if is_spotify else discord.Color.blurple()
            source_indicator = "🎵 Spotify" if is_spotify else "🎵 YouTube"
            
            description = f"**[{track.title}]({track.uri})**"
            if source_indicator:
                description = f"{source_indicator}\n{description}"
            
            embed = discord.Embed(title=title, description=description, color=embed_color)

            artist_label = await i18n.t(target_channel, "music.player.artist", artist=track.author)
            duration_label = await i18n.t(target_channel, "music.player.duration_label",
                                          duration=f"{track.length // 1000 // 60}:{track.length // 1000 % 60:02d}")
            requester_label = await i18n.t(target_channel, "music.player.requester_label",
                                           user=getattr(track, 'requester', 'Unknown'))

            embed.add_field(name=" ", value=artist_label, inline=False)
            embed.add_field(name=" ", value=duration_label, inline=False)
            embed.add_field(name=" ", value=requester_label, inline=False)
            embed.set_image(url=Config.BAR_URL)
            embed.set_footer(text=f"⚠️ Running on the Pomice branch — not fully tested yet.")

            if track.thumbnail: embed.set_thumbnail(url=track.thumbnail)

            view = NowPlayingView(player, self.user_model, self.guild_model)
            msg = await target_channel.send(embed=embed, view=view)
            # message reference for cleanup
            self.now_playing_messages[player.guild.id] = msg

        if not lastfm_handler.enabled:
            return

        if player.channel:
            member_ids = [m.id for m in player.channel.members if not m.bot]
            for user_id in member_ids:
                user_data = await self.user_model.get_user(user_id)
                if user_data and user_data.get('lastfm', {}).get('session_key'):
                    await lastfm_handler.update_now_playing(
                        user_data['lastfm']['session_key'],
                        track.author,
                        track.title
                    )

    @commands.Cog.listener()
    async def on_pomice_track_end(self, player: pomice.Player, track: pomice.Track, reason: str):
        logger.info(f"[LASTFM DEBUG] Track end event fired - reason: {reason}")
        if not player:
            logger.warning("[LASTFM DEBUG] No player in payload")
            return
        logger.info(f"[LASTFM DEBUG] Player channel: {player.channel}")

        if not hasattr(player, "history"):
            player.history = []

        if reason.upper() not in ("LOAD_FAILED", "CLEANUP", "REPLACED"):
            player.history.append(track)
            autoplay_on = getattr(player, 'autoplay_enabled', False)
            queue_empty = player.queue.is_empty
            logger.info(f"[TRACK_END] autoplay={autoplay_on}, queue_empty={queue_empty}, is_playing={player.is_playing}")
            if autoplay_on:
                if queue_empty:
                    await asyncio.sleep(0.5) # wait to avoid infinite skip loop
                    if player.queue.is_empty and not player.is_playing:
                        logger.info(f"[AUTOPLAY] Fetching recommendation based on: '{track.title}' by {track.author}")
                        recommended = await self._fetch_autoplay_track(player, track)
                        if recommended:
                            logger.info(f"[AUTOPLAY] Playing '{recommended.title}' by {recommended.author}")
                            vol = await self.guild_model.get_default_volume(player.guild.id)
                            await player.set_volume(vol)
                            await player.play(recommended)
                        else:
                            logger.info("[AUTOPLAY] No recommendation found, starting timeout")
                            self.start_timeout(player.guild.id, player)
                            await self.update_static_embed(player.guild.id)
                else:
                    next_track = player.queue.get()
                    logger.info(f"[AUTOPLAY] Queue not empty, playing next: '{next_track.title}'")
                    await player.play(next_track)
            elif not queue_empty:
                await player.play(player.queue.get())
            else:
                self.start_timeout(player.guild.id, player)
                await self.update_static_embed(player.guild.id)

        if reason.lower() == "finished" and player.channel:
            member_ids = [m.id for m in player.channel.members if not m.bot]
            logger.info(f"Track finished: {track.title} - checking scrobble for {len(member_ids)} users")

            timestamp = getattr(player, "current_track_start_time", int(time.time() - (track.length / 1000)))

            for user_id in member_ids:
                user_data = await self.user_model.get_user(user_id)
                if not user_data:
                    logger.debug(f"User {user_id} has no user data")
                    continue

                lastfm = user_data.get("lastfm", {})
                session_key = lastfm.get("session_key")
                if not session_key:
                    logger.debug(f"User {user_id} has no Last.fm session_key")
                    continue
                
                # Default to True if scrobbling key doesn't exist (backwards compatibility)
                if not lastfm.get("scrobbling", True):
                    logger.debug(f"User {user_id} has scrobbling disabled")
                    continue

                logger.info(f"Scrobbling track for user {user_id}: {track.author} - {track.title}")
                await lastfm_handler.scrobble(session_key, track.author, track.title, timestamp)

    @commands.Cog.listener()
    async def on_pomice_track_exception(self, player: pomice.Player, track: pomice.Track, exception: str):
        """Handler for when a track encounters an exception during playback."""
        logger.warning(f"[POMICE] Track exception for '{track.title if track else 'Unknown'}': {exception}")
        
        if player:
            target_channel = getattr(player, "home_channel", None)
            if not target_channel and player.guild:
                static_channel_id = await self.guild_model.get_music_channel(player.guild.id)

                if static_channel_id:
                    target_channel = self.bot.get_channel(static_channel_id)
                
                if not target_channel:
                    target_channel = player.guild.system_channel 
            
            if target_channel:
                msg = await i18n.t(target_channel, "music.errors.track_failed", error=str(exception))
                await target_channel.send(msg, delete_after=10)

    @commands.Cog.listener()
    async def on_pomice_track_stuck(self, player: pomice.Player, track: pomice.Track, threshold: float):
        """Handler for when a track gets stuck (stops sending frames)."""
        logger.warning(f"[POMICE] Track stuck: '{track.title if track else 'Unknown'}' at {threshold}ms")
        
        if not player:
            return

        target_channel = getattr(player, "home_channel", None)
        if not target_channel and player.guild:
             static_channel_id = await self.guild_model.get_music_channel(player.guild.id)
             if static_channel_id:
                 target_channel = self.bot.get_channel(static_channel_id)

        if target_channel:
             await target_channel.send(f"⚠️ Track stuck at {threshold//1000}s. Attempting to recover playback...", delete_after=10)

        # Attempt to recover (searching the track again and playing from the stuck position)
        try:
            query = track.uri if track.uri else f"{track.title} - {track.author}"
            logger.info(f"[POMICE] Recovery search query: {query}")
            
            tracks = await player.get_tracks(query)
            if tracks:
                new_track = tracks[0] if isinstance(tracks, list) else tracks.tracks[0] if hasattr(tracks, 'tracks') else None
                
                if new_track:
                    if hasattr(track, 'requester'):
                        new_track.requester = track.requester
                    
                    vol = await self.guild_model.get_default_volume(player.guild.id)
                    await player.set_volume(vol)
                    await player.play(new_track, start=int(threshold))
                    logger.info(f"[POMICE] Successfully recovered '{new_track.title}' from {threshold}ms")
                    return
            
            logger.warning("[POMICE] Recovery failed: Could not find track to replay.")
            # If fail, skip
            if not player.queue.is_empty:
                await player.play(player.queue.get())
                
        except Exception as e:
            logger.error(f"[POMICE] Error during stuck track recovery: {e}")
            # Just in case
            if not player.queue.is_empty:
                await player.play(player.queue.get())

    @commands.Cog.listener()
    async def on_pomice_websocket_closed(self, payload):
        """Handler for when the websocket is closed."""
        logger.warning(f"[POMICE] WebSocket closed: {payload.code} - {payload.reason}")
        if payload.code != 1000:
            logger.info("[POMICE] Unexpected WebSocket closure — scheduling Lavalink reconnection...")
            self.bot.loop.create_task(self._connect_lavalink())

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        # Check if bot disconnected
        if member.id == self.bot.user.id and before.channel and not after.channel:
            logger.info(f"[VOICE] Bot disconnected from voice in guild {member.guild.id}")
            if member.guild.id in self.now_playing_messages:
                try:
                    await self.now_playing_messages[member.guild.id].delete()
                except:
                    pass
                del self.now_playing_messages[member.guild.id]
            
            await self.update_static_embed(member.guild.id)
            return

        player = pomice.NodePool.get_node().get_player(member.guild.id)
        if not player or not player.channel:
            return

        # Check if bot is alone
        if len(player.channel.members) == 1 and player.channel.members[0].id == self.bot.user.id:
            if not getattr(player, "twenty_four_seven", False):
                logger.debug(f"[VOICE] Bot is alone in channel, starting timeout for guild {member.guild.id}")
                self.start_timeout(member.guild.id, player)
        elif len(player.channel.members) > 1:
            self.cancel_timeout(member.guild.id)

        await asyncio.sleep(1)
        await self.update_static_embed(member.guild.id)

    async def update_static_embed(self, guild_id: int):
        """Updates the static music player embed"""
        channel_id = await self.guild_model.get_music_channel(guild_id)
        if not channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        player = pomice.NodePool.get_node().get_player(guild_id)
        locale = await i18n.get_guild_locale(guild_id) or "en"

        is_connected = bool(player and player.is_connected)
        is_playing = bool(player and player.is_playing)
        current_track = player.current if player else None

        embed = discord.Embed(color=discord.Color.blurple())

        if not is_connected:
            embed.title = await i18n.t(channel, "music.player.idle.title", static_embed=True)
            embed.description = await i18n.t(channel, "music.player.idle.description_disconnect", static_embed=True)
            embed.set_image(url=Config.MUSIC_BANNER_URL)

        elif not is_playing or not current_track:
            embed.title = await i18n.t(channel, "music.player.idle.title", static_embed=True)
            embed.description = await i18n.t(channel, "music.player.idle.description_empty", static_embed=True)
            embed.set_image(url=Config.MUSIC_BANNER_URL)
        else:
            now_playing_title = await i18n.t(channel, "music.player.now_playing", static_embed=True, title=current_track.title)
            artist_label = await i18n.t(channel, "music.player.artist", artist=current_track.author, static_embed=True)
            duration_label = await i18n.t(channel, "music.player.duration_label",
                                            duration=f"{current_track.length // 1000 // 60}:{current_track.length // 1000 % 60:02d}",
                                            static_embed=True)

            # Detect Spotify source
            is_spotify = current_track.uri and "spotify" in current_track.uri.lower()
            if is_spotify:
                embed.color = discord.Color.green()
                source_indicator = "🎵 Spotify\n"
            else:
                embed.color = discord.Color.blurple()
                source_indicator = "🎵 YouTube\n"

            embed.title = now_playing_title
            embed.description = f"{source_indicator}**[{current_track.title}]({current_track.uri})**\n\n{artist_label}\n{duration_label}"
            if current_track.thumbnail: embed.set_thumbnail(url=current_track.thumbnail)
            if hasattr(current_track, 'requester') and current_track.requester:
                req_text = await i18n.t(channel, "music.player.requester_label", user=current_track.requester,
                                        static_embed=True)
                embed.add_field(name=" ", value=req_text)
                embed.set_image(url=Config.BAR_URL)
                embed.set_footer(text=f"⚠️ Running on the Pomice branch — not fully tested yet.")

        # Get existing message
        message_id = await self.guild_model.get_music_message(guild_id)
        message = None

        if message_id:
            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                pass

        if is_connected and is_playing and current_track:
            view = NowPlayingView(player, self.user_model, self.guild_model, locale=locale)
            await view.async_init()
        else:
            view = IdlePlaylistView(guild_id, self.user_model, self.guild_model, self.bot, locale=locale)

        if not message:
            message = await channel.send(embed=embed, view=view)
            await self.guild_model.set_music_message(guild_id, message.id)
        else:
            await message.edit(embed=embed, view=view)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if not message.guild:
            return

        # Check static channel
        channel_id = await self.guild_model.get_music_channel(message.guild.id)
        logger.debug(f"[MUSIC] on_message: guild={message.guild.id}, channel={message.channel.id}, static_channel={channel_id}")
        if not channel_id or message.channel.id != channel_id:
            return
        
        logger.info(f"[MUSIC] Message in music channel from {message.author}: {message.content[:50]}")
        
        if message.author.id in self.selecting_users:
            return
        
        try:
            await message.delete()
            logger.info(f"[MUSIC] Deleted message from {message.author}")
        except discord.Forbidden:
            logger.warning(f"[MUSIC] Missing permissions to delete message in guild {message.guild.id}")
        except discord.NotFound:
            pass
        except Exception as e:
            logger.warning(f"[MUSIC] Failed to delete message: {e}")

        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        query = message.content
        await self._play_logic(ctx, query, message.channel)

    async def _play_logic(self, ctx: commands.Context, query: str, response_channel: discord.TextChannel, redirected: bool = False, mode: str = "normal"):
        """Shared logic for play command and static channel. Mode can be 'normal', 'now', or 'next'."""
        logger.info(f"[PLAY] User {ctx.author.id} requested: {query[:100]}")
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

        player: pomice.Player = cast(pomice.Player, ctx.voice_client)
        if not player:
            try:
                logger.debug(f"[VOICE] Connecting to voice channel: {ctx.author.voice.channel.name}")
                player = await ctx.author.voice.channel.connect(cls=CustomPlayer)
                player.home_channel = response_channel
            except Exception as e:
                logger.warning(f"[VOICE] Failed to connect: {e}")
                msg = await i18n.t(ctx, "music.commands.play.not_in_voice")
                return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
        else:
            if not await self.check_voice_channel(ctx):
                return


        url_pattern = re.compile(r'https?://(?:www\.)?.+')
        is_url = url_pattern.match(query)
        SPOTIFY_URL_PATTERN = re.compile(r'https?://open\.spotify\.com/(track|album|playlist|artist)/([a-zA-Z0-9]+)')
        is_spotify = SPOTIFY_URL_PATTERN.match(query) is not None

        try:
            if player:
                tracks = await player.get_tracks(query)
            else:
                tracks = await pomice.NodePool.get_node().get_tracks(query)
        except Exception as e:
            err_str = str(e)
            if "Session not found" in err_str and player:
                logger.warning(f"[RECOVERY] Session died for guild {ctx.guild.id}. Reconnecting...")
                try:
                    await player.destroy()
                except:
                    if ctx.guild.voice_client:
                        await ctx.guild.voice_client.disconnect(force=True)
                try:
                    player = await ctx.author.voice.channel.connect(cls=CustomPlayer)
                    player.home_channel = response_channel
                    tracks = await player.get_tracks(query)
                except Exception as inner_e:
                    msg = await i18n.t(ctx, "music.errors.track_failed", error=str(inner_e))
                    return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
            elif "refused" in err_str.lower() or "connect" in err_str.lower():
                if player:
                    try:
                        await player.destroy()
                    except:
                        if ctx.guild.voice_client:
                            await ctx.guild.voice_client.disconnect(force=True)
                try:
                    msg = await i18n.t(ctx, "music.errors.node_offline")
                except:
                    msg = "Music server is currently offline. Please try again later."
                return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=10)
            else:
                msg = await i18n.t(ctx, "music.errors.track_failed", error=err_str)
                return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
        if not tracks:
            msg = await i18n.t(ctx, "music.commands.play.no_results", query=query)
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        # If it's a URL or a specific Container (Playlist), auto-play/queue
        if is_url or isinstance(tracks, pomice.Playlist):
            if isinstance(tracks, pomice.Playlist):
                for track in tracks.tracks:
                    track.requester = ctx.author.mention
                
                # For playlists, mode only affects the first track
                if mode == "now" and player.is_playing:
                    # Play first track immediately, queue the rest
                    first_track = tracks.tracks[0]
                    for track in tracks.tracks[1:]:
                        player.queue.put_at_front(track)
                    vol = await self.guild_model.get_default_volume(ctx.guild.id)
                    await player.set_volume(vol)
                    await player.play(first_track)
                    msg = await i18n.t(ctx, "music.commands.play.playing_now", title=first_track.title)
                elif mode == "next":
                    # Add all tracks to front of queue (in reverse order so first track is next)
                    for track in reversed(tracks.tracks):
                        player.queue.put_at_front(track)
                    msg = await i18n.t(ctx, "music.commands.play.playlist_added_next", count=len(tracks.tracks), name=tracks.name)
                else:
                    player.queue.extend(tracks.tracks)
                    msg = await i18n.t(ctx, "music.commands.play.playlist_added", count=len(tracks.tracks), name=tracks.name)
                
                source_label = "🎵 Spotify" if is_spotify else "🎵 YouTube"
                if source_label:
                    msg = f"{source_label} | {msg}"
                await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
            else:
                track = tracks[0]
                track.requester = ctx.author.mention
                source_label = "🎵 Spotify" if is_spotify else "🎵 YouTube"
                
                if mode == "now" and player.is_playing:
                    vol = await self.guild_model.get_default_volume(ctx.guild.id)
                    await player.set_volume(vol)
                    await player.play(track)
                    msg = await i18n.t(ctx, "music.commands.play.playing_now", title=track.title)
                elif mode == "next":
                    player.queue.put_at_front(track)
                    msg = await i18n.t(ctx, "music.commands.play.added_next", title=track.title)
                else:
                    player.queue.put(track)
                    msg = await i18n.t(ctx, "music.commands.play.added_to_queue", title=track.title)
                
                if source_label:
                    msg = f"{source_label} | {msg}"
                await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

            if not player.is_playing:
                vol = await self.guild_model.get_default_volume(ctx.guild.id)
                await player.set_volume(vol)
                await player.play(player.queue.get())

            await self.update_static_embed(ctx.guild.id)
            return

        tracks_top = tracks[:5]
        select_text = await i18n.t(ctx, "music.ui.select_track")
        instructions = await i18n.t(ctx, "music.ui.selection_instructions")

        options_text = ""
        for i, track in enumerate(tracks_top):
            options_text += f"**{i + 1}.** {track.title} - {track.author} ({track.length // 1000}s)\n"

        full_msg = f"{select_text}\n{options_text}\n{instructions}"
        
        self.selecting_users.add(ctx.author.id)
        
        try:
            msg = await self.send_response(ctx, response_channel, redirected, content=full_msg, delete_after=60)

            def check(m):
                return m.author.id == ctx.author.id and m.channel.id == response_channel.id and m.content.isdigit()

            while True:
                response = await self.bot.wait_for('message', check=check, timeout=60)

                try:
                    await response.delete()
                except:
                    pass

                choice = int(response.content)

                if choice == 0:
                    cancelled_text = await i18n.t(ctx, "music.ui.selection_cancelled")
                    await msg.edit(content=cancelled_text, delete_after=5)
                    return

                if 1 <= choice <= len(tracks_top):
                    track = tracks_top[choice - 1]
                    track.requester = ctx.author.mention
                    
                    if mode == "now" and player.is_playing:
                        vol = await self.guild_model.get_default_volume(ctx.guild.id)
                        await player.set_volume(vol)
                        await player.play(track)
                        added_text = await i18n.t(ctx, "music.commands.play.playing_now", title=track.title)
                    elif mode == "next":
                        player.queue.put_at_front(track)
                        added_text = await i18n.t(ctx, "music.commands.play.added_next", title=track.title)
                    else:
                        player.queue.put(track)
                        if not player.is_playing:
                            vol = await self.guild_model.get_default_volume(ctx.guild.id)
                            await player.set_volume(vol)
                            await player.play(player.queue.get())
                        added_text = await i18n.t(ctx, "music.commands.play.added_to_queue", title=track.title)

                    await msg.edit(content=added_text, delete_after=5)
                    await self.update_static_embed(ctx.guild.id)
                    return

        except asyncio.TimeoutError:
            timeout_text = await i18n.t(ctx, "music.ui.selection_timeout")
            try:
                await msg.edit(content=timeout_text, delete_after=5)
            except:
                pass
        finally:
            self.selecting_users.discard(ctx.author.id)

    @commands.hybrid_command(name="play", aliases=["p"])
    @app_commands.describe(
        query="Song name, URL, or playlist name",
        mode="Play mode: 'now' to play immediately, 'next' to play after current song"
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Normal (add to queue)", value="normal"),
        app_commands.Choice(name="Play Now (skip current)", value="now"),
        app_commands.Choice(name="Play Next (after current)", value="next"),
    ])
    async def play(self, ctx: commands.Context, *, query: str, mode: str = "normal"): # temp fix (might be permanent)
        """Play a song or saved playlist."""
        # if not ctx.interaction:
        #     valid_modes = ("normal", "now", "next")
        #     if mode.lower() not in valid_modes or mode.startswith(("http://", "https://")):
        #         query = f"{mode} {query}" if query else mode
        #         mode = "normal"
        #     else:
        #         mode = mode.lower()

        await self._play_command_logic(ctx, query, "normal")

    @commands.command(name="playnow", aliases=["pn"])
    async def playnow(self, ctx: commands.Context, *, query: str):
        """Play a song immediately (skips current)."""
        await self._play_command_logic(ctx, query, "now")

    @commands.command(name="playnext", aliases=["pnx"])
    async def playnext(self, ctx: commands.Context, *, query: str):
        """Play a song after the current one."""
        await self._play_command_logic(ctx, query, "next")

    async def _play_command_logic(self, ctx: commands.Context, query: str, mode: str = "normal"):
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
                channel_mention = channel.mention if channel else f"#{static_channel_id}"

                msg_processed = await i18n.t(ctx, "music.static_response.processed", channel=channel_mention)
                msg_tip = await i18n.t(ctx, "music.static_response.tip", channel=channel_mention)
                
                await ctx.send(f"{msg_processed}{msg_tip}", ephemeral=True)
                redirected = True

        await self._play_logic(ctx, query, response_channel, redirected, mode=mode)

    # ===== Personal Playlist Commands =====
    
    @commands.hybrid_group(name="playlist", aliases=["pl"])
    async def playlist(self, ctx):
        """Manage personal playlists"""
        if ctx.invoked_subcommand is None:
            await self.playlist_list(ctx)

    @playlist.command(name="create")
    @app_commands.describe(name="Name for the new playlist")
    async def playlist_create(self, ctx, *, name: str):
        """Create a new empty playlist"""
        success = await self.user_model.create_playlist(ctx.author.id, name)
        if success:
            msg = await i18n.t(ctx, "music.playlist.created", name=name)
        else:
            msg = await i18n.t(ctx, "music.playlist.create_failed", name=name)
        await ctx.send(msg, delete_after=10)

    @playlist.command(name="add")
    @app_commands.describe(playlist_name="Playlist to add to", url="URL of the song (optional, uses current track if omitted)")
    async def playlist_add(self, ctx, playlist_name: str, url: str = None):
        """Add the current song or a URL to a playlist"""
        player: pomice.Player = cast(pomice.Player, ctx.voice_client)
        
        if url:
            # Search for the track
            try:
                tracks = await pomice.NodePool.get_node().get_tracks(url)
                if not tracks:
                    msg = await i18n.t(ctx, "music.playlist.track_not_found")
                    return await ctx.send(msg, delete_after=10)
                track = tracks[0] if not isinstance(tracks, pomice.Playlist) else tracks.tracks[0]
                track_info = {'title': track.title, 'url': track.uri, 'author': track.author}
            except Exception as e:
                msg = await i18n.t(ctx, "music.playlist.error", error=str(e))
                return await ctx.send(msg, delete_after=10)
        elif player and player.current:
            track = player.current
            track_info = {'title': track.title, 'url': track.uri, 'author': track.author}
        else:
            msg = await i18n.t(ctx, "music.playlist.no_track_to_add")
            return await ctx.send(msg, delete_after=10)
        
        success = await self.user_model.add_track_to_playlist(ctx.author.id, playlist_name, track_info)
        if success:
            msg = await i18n.t(ctx, "music.playlist.track_added", title=track_info['title'], playlist=playlist_name)
        else:
            msg = await i18n.t(ctx, "music.playlist.playlist_not_found", name=playlist_name)
        await ctx.send(msg, delete_after=10)

    @playlist.command(name="remove")
    @app_commands.describe(playlist_name="Playlist to remove from", index="Track number to remove")
    async def playlist_remove(self, ctx, playlist_name: str, index: int):
        """Remove a track from a playlist by its number"""
        success, result = await self.user_model.remove_track_from_playlist(ctx.author.id, playlist_name, index - 1)
        
        if success:
            track_title = result.get('title', 'Unknown')
            msg = await i18n.t(ctx, "music.playlist.track_removed_title", title=track_title, playlist=playlist_name)
        else:
            error = result.get('error', 'unknown') if result else 'unknown'
            if error == 'playlist_not_found':
                msg = await i18n.t(ctx, "music.playlist.playlist_not_found", name=playlist_name)
            elif error == 'empty_playlist':
                msg = await i18n.t(ctx, "music.playlist.empty_playlist")
            elif error == 'no_additions':
                msg = await i18n.t(ctx, "music.playlist.remove_no_additions")
            elif error == 'invalid_index':
                max_index = result.get('max_index', 0)
                msg = await i18n.t(ctx, "music.playlist.remove_invalid_index", index=index, max=max_index)
            else:
                msg = await i18n.t(ctx, "music.playlist.remove_failed")
        
        await ctx.send(msg, delete_after=10)

    @playlist.command(name="view")
    @app_commands.describe(name="Playlist to view")
    async def playlist_view(self, ctx, *, name: str):
        """View info about a playlist"""
        playlist = await self.user_model.get_playlist(ctx.author.id, name)
        if not playlist:
            msg = await i18n.t(ctx, "music.playlist.playlist_not_found", name=name)
            return await ctx.send(msg, delete_after=10)
        
        is_imported = playlist.get('type') == 'imported'
        
        embed = discord.Embed(
            title=f"📂 {playlist.get('name', name)}",
            color=discord.Color.blue()
        )
        
        if is_imported:
            source_url = playlist.get('source_url', 'Unknown')
            track_count = playlist.get('source_track_count', 0)
            created_at = playlist.get('created_at', datetime.now()).strftime("%Y-%m-%d")
            
            imported_desc = await i18n.t(ctx, "music.playlist.imported_desc", source_url=source_url, track_count=track_count)
            embed.description = imported_desc
            created_field = await i18n.t(ctx, "music.playlist.created_field")
            embed.add_field(name=created_field, value=created_at, inline=True)
            
            modifications = playlist.get('modifications', {})
            additions = len(modifications.get('additions', []))
            removals = len(modifications.get('removals', []))
            
            modifications_field = await i18n.t(ctx, "music.playlist.modifications_field")
            modifications_value = await i18n.t(ctx, "music.playlist.modifications_value", additions=additions, removals=removals)
            embed.add_field(name=modifications_field, value=modifications_value, inline=True)
            
            if additions > 0:
                modifications_footer = await i18n.t(ctx, "music.playlist.modifications_footer")
                embed.set_footer(text=modifications_footer)
        else:
            tracks = playlist.get('tracks', [])
            if tracks:
                track_lines = []
                for i, track in enumerate(tracks[:25], 1):
                    title = track.get('title', 'Unknown')
                    author = track.get('author', 'Unknown')
                    track_lines.append(f"**{i}.** {title} — {author}")
                embed.description = "\n".join(track_lines)
                if len(tracks) > 25:
                    footer_text = await i18n.t(ctx, "music.playlist.view_more_tracks", count=len(tracks) - 25)
                    embed.set_footer(text=footer_text)
            else:
                embed.description = await i18n.t(ctx, "music.playlist.view_empty_desc")

        
        await ctx.send(embed=embed)

    @playlist.command(name="list")
    async def playlist_list(self, ctx):
        """List all your playlists"""
        playlists = await self.user_model.get_all_playlists(ctx.author.id)
        if not playlists:
            msg = await i18n.t(ctx, "music.playlist.no_personal")
            return await ctx.send(msg, delete_after=10)

        title = await i18n.t(ctx, "music.playlist.personal_title")
        embed = discord.Embed(title=title, color=discord.Color.blue())
        
        for key, playlist in list(playlists.items())[:25]:
            is_imported = playlist.get('type') == 'imported'
            
            if is_imported:
                count = playlist.get('source_track_count', 0)
                additions = len(playlist.get('modifications', {}).get('additions', []))
                val_text = await i18n.t(ctx, "music.playlist.list_imported_type", count=count)
                if additions > 0:
                    additions_text = await i18n.t(ctx, "music.playlist.list_imported_additions", additions=additions)
                    val_text += additions_text
            else:
                count = len(playlist.get('tracks', []))
                val_text = await i18n.t(ctx, "music.playlist.list_regular_type", count=count)
            
            embed.add_field(
                name=f"📂 {playlist.get('name', key)}", 
                value=val_text, 
                inline=True
            )
        
        footer_text = await i18n.t(ctx, "music.playlist.view_footer")
        embed.set_footer(text=footer_text)
        await ctx.send(embed=embed)

    @playlist.command(name="delete")
    @app_commands.describe(name="Playlist to delete")
    async def playlist_delete(self, ctx, *, name: str):
        """Delete an entire playlist"""
        success = await self.user_model.delete_playlist(ctx.author.id, name)
        if success:
            msg = await i18n.t(ctx, "music.playlist.deleted_personal", name=name)
        else:
            msg = await i18n.t(ctx, "music.playlist.playlist_not_found", name=name)
        await ctx.send(msg, delete_after=10)

    @playlist.command(name="play")
    @app_commands.describe(name="Playlist to play")
    async def playlist_play(self, ctx, *, name: str):
        """Play all tracks from a playlist"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)
        
        playlist = await self.user_model.get_playlist(ctx.author.id, name)
        if not playlist:
            msg = await i18n.t(ctx, "music.playlist.playlist_not_found", name=name)
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=10)
        
        is_imported = playlist.get('type') == 'imported'
        tracks_list = playlist.get('tracks', [])
        if not is_imported and not tracks_list:
            msg = await i18n.t(ctx, "music.playlist.empty_playlist")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=10)
        player: pomice.Player = cast(pomice.Player, ctx.voice_client)
        if not player:
            if not ctx.author.voice:
                msg = await i18n.t(ctx, "music.commands.play.not_in_voice")
                return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
            player = await ctx.author.voice.channel.connect(cls=CustomPlayer)
            player.home_channel = response_channel
        
        playlist_name = playlist.get('name', name)
        
        if is_imported:
            from utils.playlist_loader import PlaylistLoader
            loading_msg = await i18n.t(ctx, "music.playlist.loading_imported", name=playlist_name)
            status_msg = await self.send_response(ctx, response_channel, redirected, content=loading_msg)
            
            try:
                # Load source
                tracks = await PlaylistLoader.load_playlist(playlist, player)
                if not tracks:
                    load_failed_msg = await i18n.t(ctx, "music.playlist.load_failed")
                    return await status_msg.edit(content=load_failed_msg)
                
                # Add to queue
                count = 0
                for track in tracks:
                    track.requester = ctx.author.mention
                    player.queue.put(track)
                    count += 1
                
                if not player.is_playing:
                    vol = await self.guild_model.get_default_volume(ctx.guild.id)
                    await player.set_volume(vol)
                    await player.play(player.queue.get())
                
                msg_content = await i18n.t(ctx, "music.playlist.added_tracks", count=count, name=playlist_name)
                
                # Handle additions in background
                additions = playlist.get('modifications', {}).get('additions', [])
                if additions:
                    loading_additions_msg = await i18n.t(ctx, "music.playlist.loading_additions", count=len(additions))
                    msg_content += "\n" + loading_additions_msg
                    await status_msg.edit(content=msg_content)
                    
                    _done = {'value': False}
                    async def progress_callback(loaded, total):
                        try:
                            if not _done['value']:
                                if loaded % 5 == 0:
                                    progress_msg = await i18n.t(ctx, "music.playlist.loading_additions_progress", count=count, loaded=loaded, total=total)
                                    await status_msg.edit(content=progress_msg)

                            if not _done['value'] and (loaded == total or loaded % 5 != 0 or loaded == 0):
                                _done['value'] = True
                                final_msg = await i18n.t(ctx, "music.playlist.added_with_additions", count=count, additions=loaded, name=playlist_name)
                                await status_msg.edit(content=final_msg)
                        except:
                            pass
                    
                    self.bot.loop.create_task(
                        PlaylistLoader.load_additions_background(additions, player, progress_callback)
                    )
                else:
                    await status_msg.edit(content=msg_content)
                
                await self.update_static_embed(ctx.guild.id)
                
            except Exception as e:
                logger.error(f"Error loading imported playlist: {e}")
                error_msg = await i18n.t(ctx, "music.playlist.load_error", error=str(e))
                await status_msg.edit(content=error_msg)
        else:
            count = 0
            failed = 0
            loading_msg = await i18n.t(ctx, "music.playlist.loading_imported", name=playlist_name)
            status_msg = await self.send_response(ctx, response_channel, redirected, content=loading_msg)
            
            try:
                for track_data in tracks_list:
                    track_url = track_data.get('url', '')
                    if not track_url:
                        failed += 1
                        continue
                    try:
                        results = await player.get_tracks(track_url)
                        if results:
                            t = results[0] if isinstance(results, list) else results.tracks[0] if hasattr(results, 'tracks') else None
                            if t:
                                t.requester = ctx.author.mention
                                player.queue.put(t)
                                count += 1
                            else:
                                failed += 1
                        else:
                            failed += 1
                    except Exception:
                        failed += 1
                
                if count == 0:
                    load_failed_msg = await i18n.t(ctx, "music.playlist.load_failed")
                    return await status_msg.edit(content=load_failed_msg)
                
                if not player.is_playing:
                    vol = await self.guild_model.get_default_volume(ctx.guild.id)
                    await player.set_volume(vol)
                    await player.play(player.queue.get())
                
                msg_content = await i18n.t(ctx, "music.playlist.added_tracks", count=count, name=playlist_name)
                if failed > 0:
                    failed_text = await i18n.t(ctx, "music.playlist.tracks_failed_to_load", count=failed)
                    msg_content += failed_text
                await status_msg.edit(content=msg_content)
                await self.update_static_embed(ctx.guild.id)
            except Exception as e:
                logger.error(f"Error loading regular playlist: {e}")
                error_msg = await i18n.t(ctx, "music.playlist.load_error", error=str(e))
                await status_msg.edit(content=error_msg)



    @playlist.command(name="import")
    @app_commands.describe(url="YouTube/Spotify playlist URL", name="Custom name for the playlist (optional, uses source name if not provided)")
    async def playlist_import(self, ctx, url: str, name: Optional[str] = None):
        """Import a YouTube/Spotify playlist to your saved playlists"""
        logger.info(f"[PLAYLIST] User {ctx.author.id} importing playlist from: {url[:80]}")
        status_msg = None
        
        try:
            msg = await i18n.t(ctx, "music.playlist.importing")
            status_msg = await ctx.send(msg)
            try:
                tracks = await pomice.NodePool.get_node().get_tracks(url)
            except Exception as e:
                logger.error(f"Search failed during import: {e}")
                tracks = None

            if not tracks:
                if status_msg:
                    await status_msg.delete()
                msg = await i18n.t(ctx, "music.playlist.import_failed")
                return await ctx.send(msg, delete_after=10)
            
            track_count = 0
            source_name = "Imported Playlist"
            
            if isinstance(tracks, pomice.Playlist):
                track_count = len(tracks.tracks)
                source_name = tracks.name
            elif isinstance(tracks, list):
                track_count = len(tracks)
                if track_count > 0:
                     source_name = "Imported Tracks"
            else:
                 track_count = 1
                 source_name = tracks.title

            if track_count == 0:
                if status_msg:
                    await status_msg.delete()
                empty_msg = await i18n.t(ctx, "music.playlist.empty_import")
                return await ctx.send(empty_msg, delete_after=10)

            playlist_name = name or source_name
            
            # Save as imported playlist
            success = await self.user_model.import_playlist(ctx.author.id, playlist_name, url, track_count)
            
            if status_msg:
                await status_msg.delete()
            
            if success:
                imported_msg = await i18n.t(ctx, "music.playlist.imported_linked", name=playlist_name, source=source_name, count=track_count)
                await ctx.send(imported_msg, delete_after=15)
            else:
                msg = await i18n.t(ctx, "music.playlist.import_failed")
                await ctx.send(msg, delete_after=10)
                
        except Exception as e:
            logger.error(f"Error importing playlist: {e}", exc_info=True)
            if status_msg:
                try:
                    await status_msg.delete()
                except:
                    pass
            msg = await i18n.t(ctx, "music.playlist.import_failed")
            await ctx.send(msg, delete_after=10)

    @commands.hybrid_group(name="serverplaylist", aliases=["spl"])
    @commands.has_permissions(manage_guild=True)
    async def serverplaylist(self, ctx):
        """Manage server playlists (Admin only)"""
        if ctx.invoked_subcommand is None:
            await self.server_list(ctx)

    @serverplaylist.command(name="create")
    @app_commands.describe(name="Name for the new server playlist")
    async def server_create(self, ctx, *, name: str):
        """Create a new empty server playlist"""
        success = await self.guild_model.create_playlist(ctx.guild.id, name)
        if success:
            msg = await i18n.t(ctx, "music.playlist.created_server", name=name)
        else:
            msg = await i18n.t(ctx, "music.playlist.create_failed", name=name)
        await ctx.send(msg, delete_after=10)

    @serverplaylist.command(name="add")
    @app_commands.describe(playlist_name="Playlist to add to", url="URL of the song")
    async def server_add(self, ctx, playlist_name: str, url: str = None):
        """Add a song to a server playlist"""
        player: pomice.Player = cast(pomice.Player, ctx.voice_client)
        
        if url:
            try:
                tracks = await pomice.NodePool.get_node().get_tracks(url)
                if not tracks:
                    msg = await i18n.t(ctx, "music.playlist.track_not_found")
                    return await ctx.send(msg, delete_after=10)
                track = tracks[0] if not isinstance(tracks, pomice.Playlist) else tracks.tracks[0]
                track_info = {'title': track.title, 'url': track.uri, 'author': track.author}
            except Exception as e:
                msg = await i18n.t(ctx, "music.playlist.error", error=str(e))
                return await ctx.send(msg, delete_after=10)
        elif player and player.current:
            track = player.current
            track_info = {'title': track.title, 'url': track.uri, 'author': track.author}
        else:
            msg = await i18n.t(ctx, "music.playlist.no_track_to_add")
            return await ctx.send(msg, delete_after=10)
        
        success = await self.guild_model.add_track_to_playlist(ctx.guild.id, playlist_name, track_info)
        if success:
            msg = await i18n.t(ctx, "music.playlist.track_added_server", title=track_info['title'], playlist=playlist_name)
        else:
            msg = await i18n.t(ctx, "music.playlist.playlist_not_found", name=playlist_name)
        await ctx.send(msg, delete_after=10)

    @serverplaylist.command(name="remove")
    @app_commands.describe(playlist_name="Playlist to remove from", index="Track number to remove")
    async def server_remove(self, ctx, playlist_name: str, index: int):
        """Remove a track from a server playlist"""
        success, result = await self.guild_model.remove_track_from_playlist(ctx.guild.id, playlist_name, index - 1)
        
        if success:
            track_title = result.get('title', 'Unknown')
            msg = await i18n.t(ctx, "music.playlist.track_removed_title", title=track_title, playlist=playlist_name)
        else:
            error = result.get('error', 'unknown') if result else 'unknown'
            if error == 'playlist_not_found':
                msg = await i18n.t(ctx, "music.playlist.playlist_not_found", name=playlist_name)
            elif error == 'empty_playlist':
                msg = await i18n.t(ctx, "music.playlist.empty_playlist")
            elif error == 'no_additions':
                msg = await i18n.t(ctx, "music.playlist.remove_no_additions")
            elif error == 'invalid_index':
                max_index = result.get('max_index', 0)
                msg = await i18n.t(ctx, "music.playlist.remove_invalid_index", index=index, max=max_index)
            else:
                msg = await i18n.t(ctx, "music.playlist.remove_failed")
        await ctx.send(msg, delete_after=10)

    @serverplaylist.command(name="view")
    @app_commands.describe(name="Server playlist to view")
    async def server_view(self, ctx, *, name: str):
        """View info about a server playlist"""
        playlist = await self.guild_model.get_playlist(ctx.guild.id, name)
        if not playlist:
            msg = await i18n.t(ctx, "music.playlist.playlist_not_found", name=name)
            return await ctx.send(msg, delete_after=10)
        
        is_imported = playlist.get('type') == 'imported'
        
        embed = discord.Embed(
            title=f"📁 {playlist.get('name', name)} (Server)",
            color=discord.Color.gold()
        )
        
        if is_imported:
            source_url = playlist.get('source_url', 'Unknown')
            track_count = playlist.get('source_track_count', 0)
            created_at = playlist.get('created_at', datetime.now()).strftime("%Y-%m-%d")
            
            imported_desc = await i18n.t(ctx, "music.playlist.imported_desc", source_url=source_url, track_count=track_count)
            embed.description = imported_desc
            created_field = await i18n.t(ctx, "music.playlist.created_field")
            embed.add_field(name=created_field, value=created_at, inline=True)
            
            modifications = playlist.get('modifications', {})
            additions = len(modifications.get('additions', []))
            removals = len(modifications.get('removals', []))
            
            modifications_field = await i18n.t(ctx, "music.playlist.modifications_field")
            modifications_value = await i18n.t(ctx, "music.playlist.modifications_value", additions=additions, removals=removals)
            embed.add_field(name=modifications_field, value=modifications_value, inline=True)
            
            if additions > 0:
                modifications_footer = await i18n.t(ctx, "music.playlist.modifications_footer")
                embed.set_footer(text=modifications_footer)
        else:
            tracks = playlist.get('tracks', [])
            if tracks:
                track_lines = []
                for i, track in enumerate(tracks[:25], 1):
                    title = track.get('title', 'Unknown')
                    author = track.get('author', 'Unknown')
                    track_lines.append(f"**{i}.** {title} — {author}")
                embed.description = "\n".join(track_lines)
                if len(tracks) > 25:
                    footer_text = await i18n.t(ctx, "music.playlist.view_more_tracks", count=len(tracks) - 25)
                    embed.set_footer(text=footer_text)
            else:
                embed.description = await i18n.t(ctx, "music.playlist.view_empty_server")

        
        await ctx.send(embed=embed)

    @serverplaylist.command(name="list")
    async def server_list(self, ctx):
        """List all server playlists"""
        playlists = await self.guild_model.get_all_playlists(ctx.guild.id)
        if not playlists:
            msg = await i18n.t(ctx, "music.playlist.no_server")
            return await ctx.send(msg, delete_after=10)

        title = await i18n.t(ctx, "music.playlist.server_title")
        embed = discord.Embed(title=title, color=discord.Color.gold())
        
        for key, playlist in list(playlists.items())[:25]:
            is_imported = playlist.get('type') == 'imported'
            
            if is_imported:
                count = playlist.get('source_track_count', 0)
                additions = len(playlist.get('modifications', {}).get('additions', []))
                val_text = await i18n.t(ctx, "music.playlist.list_imported_type", count=count)
                if additions > 0:
                    additions_text = await i18n.t(ctx, "music.playlist.list_imported_additions", additions=additions)
                    val_text += additions_text
            else:
                count = len(playlist.get('tracks', []))
                val_text = await i18n.t(ctx, "music.playlist.list_regular_type", count=count)

            embed.add_field(
                name=f"📁 {playlist.get('name', key)}", 
                value=val_text, 
                inline=True
            )
        
        await ctx.send(embed=embed)

    @serverplaylist.command(name="delete")
    @app_commands.describe(name="Server playlist to delete")
    async def server_delete(self, ctx, *, name: str):
        """Delete an entire server playlist"""
        success = await self.guild_model.delete_playlist(ctx.guild.id, name)
        if success:
            msg = await i18n.t(ctx, "music.playlist.deleted_server", name=name)
        else:
            msg = await i18n.t(ctx, "music.playlist.playlist_not_found", name=name)
        await ctx.send(msg, delete_after=10)

    @serverplaylist.command(name="play")
    @app_commands.describe(name="Server playlist to play")
    async def server_play(self, ctx, *, name: str):
        """Play all tracks from a server playlist"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)
        
        playlist = await self.guild_model.get_playlist(ctx.guild.id, name)
        if not playlist:
            msg = await i18n.t(ctx, "music.playlist.playlist_not_found", name=name)
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=10)

        is_imported = playlist.get('type') == 'imported'
        tracks_list = playlist.get('tracks', [])
        if not is_imported and not tracks_list:
            msg = await i18n.t(ctx, "music.playlist.empty_playlist")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=10)
        
        player: pomice.Player = cast(pomice.Player, ctx.voice_client)
        if not player:
            if not ctx.author.voice:
                msg = await i18n.t(ctx, "music.commands.play.not_in_voice")
                return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
            player = await ctx.author.voice.channel.connect(cls=CustomPlayer)
            player.home_channel = response_channel
        
        playlist_name = playlist.get('name', name)
        
        if is_imported:
            from utils.playlist_loader import PlaylistLoader

            loading_msg = await i18n.t(ctx, "music.playlist.loading_imported", name=playlist_name)
            status_msg = await self.send_response(ctx, response_channel, redirected, content=loading_msg)
            
            try:
                tracks = await PlaylistLoader.load_playlist(playlist, player)
                if not tracks:
                    load_failed_msg = await i18n.t(ctx, "music.playlist.load_failed")
                    await status_msg.edit(content=load_failed_msg)
                    return
                
                count = 0
                for track in tracks:
                    track.requester = ctx.author.mention
                    player.queue.put(track)
                    count += 1
                
                if not player.is_playing:
                    vol = await self.guild_model.get_default_volume(ctx.guild.id)
                    await player.set_volume(vol)
                    await player.play(player.queue.get())
                
                msg_content = await i18n.t(ctx, "music.playlist.added_tracks", count=count, name=playlist_name)
                
                additions = playlist.get('modifications', {}).get('additions', [])
                if additions:
                    loading_additions_msg = await i18n.t(ctx, "music.playlist.loading_additions", count=len(additions))
                    msg_content += "\n" + loading_additions_msg
                    await status_msg.edit(content=msg_content)
                    
                    _done = {'value': False}
                    async def progress_callback(loaded, total):
                        try:
                            if not _done['value']:
                                if loaded % 5 == 0:
                                    progress_msg = await i18n.t(ctx, "music.playlist.loading_additions_progress", count=count, loaded=loaded, total=total)
                                    await status_msg.edit(content=progress_msg)
                            if not _done['value'] and (loaded == total or loaded % 5 != 0 or loaded == 0):
                                _done['value'] = True
                                final_msg = await i18n.t(ctx, "music.playlist.added_with_additions", count=count, additions=loaded, name=playlist_name)
                                await status_msg.edit(content=final_msg)
                        except:
                            pass
                    
                    self.bot.loop.create_task(
                        PlaylistLoader.load_additions_background(additions, player, progress_callback)
                    )
                else:
                    await status_msg.edit(content=msg_content)
                
                await self.update_static_embed(ctx.guild.id)
            except Exception as e:
                logger.error(f"Error loading imported server playlist: {e}")
                error_msg = await i18n.t(ctx, "music.playlist.load_error", error=str(e))
                await status_msg.edit(content=error_msg)
        else:
            count = 0
            failed = 0
            loading_msg = await i18n.t(ctx, "music.playlist.loading_imported", name=playlist_name)
            status_msg = await self.send_response(ctx, response_channel, redirected, content=loading_msg)
            
            try:
                for track_data in tracks_list:
                    track_url = track_data.get('url', '')
                    if not track_url:
                        failed += 1
                        continue
                    try:
                        results = await player.get_tracks(track_url)
                        if results:
                            t = results[0] if isinstance(results, list) else results.tracks[0] if hasattr(results, 'tracks') else None
                            if t:
                                t.requester = ctx.author.mention
                                player.queue.put(t)
                                count += 1
                            else:
                                failed += 1
                        else:
                            failed += 1
                    except Exception:
                        failed += 1
                
                if count == 0:
                    load_failed_msg = await i18n.t(ctx, "music.playlist.load_failed")
                    return await status_msg.edit(content=load_failed_msg)
                
                if not player.is_playing:
                    vol = await self.guild_model.get_default_volume(ctx.guild.id)
                    await player.set_volume(vol)
                    await player.play(player.queue.get())
                
                msg_content = await i18n.t(ctx, "music.playlist.added_tracks", count=count, name=playlist_name)
                if failed > 0:
                    failed_text = await i18n.t(ctx, "music.playlist.tracks_failed_to_load", count=failed)
                    msg_content += failed_text
                await status_msg.edit(content=msg_content)
                await self.update_static_embed(ctx.guild.id)
            except Exception as e:
                logger.error(f"Error loading regular server playlist: {e}")
                error_msg = await i18n.t(ctx, "music.playlist.load_error", error=str(e))
                await status_msg.edit(content=error_msg)


    @serverplaylist.command(name="import")
    @app_commands.describe(url="YouTube/Spotify playlist URL", name="Custom name for the playlist (optional, uses source name if not provided)")
    async def server_import(self, ctx, url: str, name: Optional[str] = None):
        """Import a YouTube/Spotify playlist to server playlists"""
        status_msg = None
        
        try:
            msg = await i18n.t(ctx, "music.playlist.importing")
            status_msg = await ctx.send(msg)
            
            try:
                tracks = await pomice.NodePool.get_node().get_tracks(url)
            except Exception as e:
                logger.error(f"Search failed during server import: {e}")
                tracks = None
            
            if not tracks:
                if status_msg:
                    await status_msg.delete()
                msg = await i18n.t(ctx, "music.playlist.import_failed")
                return await ctx.send(msg, delete_after=10)
            
            track_count = 0
            source_name = "Imported Playlist"
            
            if isinstance(tracks, pomice.Playlist):
                track_count = len(tracks.tracks)
                source_name = tracks.name
            elif isinstance(tracks, list):
                track_count = len(tracks)
                if track_count > 0:
                     source_name = "Imported Tracks"
            else:
                 track_count = 1
                 source_name = tracks.title

            if track_count == 0:
                if status_msg:
                    await status_msg.delete()
                empty_msg = await i18n.t(ctx, "music.playlist.empty_import")
                return await ctx.send(empty_msg, delete_after=10)
            
            playlist_name = name or source_name
            
            success = await self.guild_model.import_playlist(ctx.guild.id, playlist_name, url, track_count)
            
            if status_msg:
                await status_msg.delete()
            
            if success:
                imported_msg = await i18n.t(ctx, "music.playlist.imported_linked_server", name=playlist_name, source=source_name, count=track_count)
                await ctx.send(imported_msg, delete_after=15)
            else:
                msg = await i18n.t(ctx, "music.playlist.import_failed")
                await ctx.send(msg, delete_after=10)
                
        except Exception as e:
            logger.error(f"Error importing server playlist: {e}", exc_info=True)
            if status_msg:
                try:
                    await status_msg.delete()
                except:
                    pass
            msg = await i18n.t(ctx, "music.playlist.import_failed")
            await ctx.send(msg, delete_after=10)

    @commands.hybrid_command(name="previous", aliases=["prev", "back"])
    async def previous(self, ctx: commands.Context):
        """Play the previous song"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        if not await self.check_voice_channel(ctx):
            return

        player: pomice.Player | None = cast(pomice.Player, ctx.voice_client)

        if not player:
            msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        if not hasattr(player, "history") or not player.history:
            msg = await i18n.t(ctx, "music.errors.no_history")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        previous_track = player.history[-1]

        if player.current:
            player.queue.put_at(0, player.current)

        player.history.pop()
        player.queue.put_at(0, previous_track)

        await player.stop()

        msg = await i18n.t(ctx, "music.commands.previous.playing", title=previous_track.title)
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

    @commands.hybrid_command(name="skip", aliases=["s", "next"])
    async def skip(self, ctx: commands.Context):
        """Skip the current song"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        if not await self.check_voice_channel(ctx, response_channel, redirected):
            return

        player: pomice.Player = cast(pomice.Player, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        if not player.is_playing:
            msg = await i18n.t(ctx, "music.commands.skip.nothing_playing")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        await player.stop()
        msg = await i18n.t(ctx, "music.commands.skip.skipped")
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

    @commands.hybrid_command(name="skipto", aliases=["st", "jumpto"])
    @app_commands.describe(position="Queue position to skip to (e.g. 3 to skip to the 3rd song)")
    async def skipto(self, ctx: commands.Context, position: int):
        """Skip to a specific song in the queue"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        if not await self.check_voice_channel(ctx, response_channel, redirected):
            return

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        if not player.is_playing:
            msg = await i18n.t(ctx, "music.commands.skip.nothing_playing")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        queue_length = len(player.queue.to_list())
        if position < 1 or position > queue_length:
            msg = await i18n.t(ctx, "music.commands.skipto.invalid_position", position=position, max=queue_length)
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        for _ in range(position - 1):
            player.queue.remove_at(0)

        target_track = player.queue.to_list()[0] if not player.queue.is_empty else None
        target_title = target_track.title if target_track else "Unknown"

        await player.stop()

        msg = await i18n.t(ctx, "music.commands.skipto.success", position=position, title=target_title)
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

    @commands.hybrid_command(name="seek")
    @app_commands.describe(position="Position to seek to (e.g. 1:30, 90, or 1:30:00)")
    async def seek(self, ctx: commands.Context, position: str):
        """Seek to a specific position in the current track"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        if not await self.check_voice_channel(ctx, response_channel, redirected):
            return

        player: pomice.Player = cast(pomice.Player, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        if not player.current:
            msg = await i18n.t(ctx, "music.commands.seek.nothing_playing")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        # Parse position - supports formats: "1:30", "90", "1:30:00"
        try:
            parts = position.split(":")
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
                msg = await i18n.t(ctx, "music.commands.seek.invalid_format")
                return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
        except ValueError:
            msg = await i18n.t(ctx, "music.commands.seek.invalid_format")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        # Check bounds
        if milliseconds < 0 or milliseconds > player.current.length:
            msg = await i18n.t(ctx, "music.commands.seek.out_of_bounds")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        await player.seek(milliseconds)
        logger.info(f"[PLAY] User {ctx.author.id} seeked to {position} ({milliseconds}ms)")
        
        # Format the position for display
        total_seconds = milliseconds // 1000
        if total_seconds >= 3600:
            formatted = f"{total_seconds // 3600}:{(total_seconds % 3600) // 60:02d}:{total_seconds % 60:02d}"
        else:
            formatted = f"{total_seconds // 60}:{total_seconds % 60:02d}"
        
        msg = await i18n.t(ctx, "music.commands.seek.success", position=formatted)
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

    @commands.hybrid_command(name="stop", aliases=["leave", "dc", "disconnect"])
    async def stop(self, ctx: commands.Context):
        """Stop playback and disconnect"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        if not await self.check_voice_channel(ctx):
            return

        player: pomice.Player = cast(pomice.Player, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        try:
            await player.destroy()
        except Exception as e:
            logger.error(f"Error destroying player gracefully: {e}")
            if ctx.guild.voice_client:
                await ctx.guild.voice_client.disconnect(force=True)
        
        msg = await i18n.t(ctx, "music.commands.disconnect.disconnected")
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
        await self.update_static_embed(ctx.guild.id)

    @commands.hybrid_command(name="queue", aliases=["q", "list"])
    async def queue(self, ctx: commands.Context):
        """Show the current queue"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        player = cast(pomice.Player, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        title = await i18n.t(ctx, "music.commands.queue.title")
        locale = await i18n.get_locale(ctx)
        view = QueuePaginationView(player, title=title, locale=locale)
        
        if not view.full_playlist:
             msg = await i18n.t(ctx, "music.commands.queue.empty")
             return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
        
        msg = await self.send_response(ctx, response_channel, redirected, embed=view.get_embed(), view=view)
        view.message = msg

    @commands.hybrid_command(name="shuffle")
    async def shuffle(self, ctx: commands.Context):
        """Shuffle the queue"""
        try:
            await self.handle_command_cleanup(ctx)
            response_channel = await self.get_response_channel(ctx)
            redirected = await self.acknowledge_static_redirect(ctx)

            player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
            if not player:
                msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
                return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

            if player.queue.is_empty:
                msg = await i18n.t(ctx, "music.commands.queue.empty")
                return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

            player.queue.shuffle()
            msg = await i18n.t(ctx, "music.commands.shuffle.success")
            await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
            await self.update_static_embed(ctx.guild.id)
        except Exception as e:
            logger.error(f"Error in shuffle command: {e}", exc_info=True)
            try:
                error_msg = await i18n.t(ctx, "music.errors.command_error", error=str(e))
                if ctx.interaction and not ctx.interaction.response.is_done():
                    await ctx.send(error_msg, ephemeral=True)
                elif ctx.interaction:
                    await ctx.send(error_msg, delete_after=10)
                else:
                    await ctx.send(error_msg, delete_after=10)
            except:
                pass

    @commands.hybrid_command(name="move")
    async def move(self, ctx: commands.Context, index_from: int, index_to: int):
        """Move a song from one position to another"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        try:
            player.queue.move(index_from - 1, index_to - 1)
            msg = await i18n.t(ctx, "music.commands.move.success", from_index=index_from, to_index=index_to)
            await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
            await self.update_static_embed(ctx.guild.id)
        except IndexError:
            msg = await i18n.t(ctx, "music.commands.move.invalid_index")
            await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

    @commands.hybrid_command(name="remove")
    async def remove(self, ctx: commands.Context, index: int):
        """Remove a song from the queue"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        try:
            removed_track = player.queue.remove_at(index - 1)
            msg = await i18n.t(ctx, "music.commands.remove.success", title=removed_track.title)
            await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
            await self.update_static_embed(ctx.guild.id)
        except IndexError:
            msg = await i18n.t(ctx, "music.commands.remove.invalid_index")
            await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

    @commands.hybrid_command(name="clear")
    async def clear(self, ctx: commands.Context):
        """Clear the queue"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        player.queue.clear()
        msg = await i18n.t(ctx, "music.commands.clear.success")
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
        await self.update_static_embed(ctx.guild.id)

    @commands.hybrid_group(name="volume", aliases=["v", "vol"])
    async def volume(self, ctx):
        """Manage volume"""
        if ctx.invoked_subcommand is None:
            await self.volume_set(ctx)

    @volume.command(name="set")
    async def volume_set(self, ctx: commands.Context, volume: int = None):
        """Set volume (0-100)"""
        if volume is None:
             if ctx.invoked_subcommand is None:
                 await self.handle_command_cleanup(ctx)
                 response_channel = await self.get_response_channel(ctx)
                 redirected = await self.acknowledge_static_redirect(ctx)
                 player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
                 if not player:
                      msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
                      return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
                 msg = await i18n.t(ctx, "music.commands.volume.current", volume=player.volume)
                 return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
             return

        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        if not await self.check_voice_channel(ctx, response_channel, redirected):
            return

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        if not 0 <= volume <= 100:
            msg = await i18n.t(ctx, "music.commands.volume.invalid")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        await player.set_volume(volume)
        msg = await i18n.t(ctx, "music.commands.volume.set", volume=volume)
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
        await self.update_static_embed(ctx.guild.id)

    @volume.command(name="default")
    @commands.has_permissions(manage_guild=True)
    async def volume_default(self, ctx: commands.Context, volume: int):
        """Set the default volume for the server"""
        if not 0 <= volume <= 100:
            msg = await i18n.t(ctx, "music.commands.volume.invalid")
            return await ctx.send(msg, delete_after=5)
        
        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if player:
            await player.set_volume(volume)

        await self.guild_model.set_default_volume(ctx.guild.id, volume)
        msg = await i18n.t(ctx, "music.commands.volume.default_set", volume=volume)
        await ctx.send(msg, delete_after=10)
        

    @commands.hybrid_group(name="musicchannel")
    @commands.has_permissions(manage_channels=True)
    async def musicchannel(self, ctx):
        """Manage static music channel"""
        pass

    @musicchannel.command(name="set")
    async def musicchannel_set(self, ctx, channel: discord.TextChannel):
        """Set the static music channel"""
        await self.guild_model.set_music_channel(ctx.guild.id, channel.id)
        msg = await i18n.t(ctx, "music.commands.musicchannel.set", channel=channel.mention)
        await ctx.send(msg)
        await self.update_static_embed(ctx.guild.id)

    @musicchannel.command(name="remove")
    async def musicchannel_remove(self, ctx):
        """Remove the static music channel"""
        await self.guild_model.remove_music_channel(ctx.guild.id)
        msg = await i18n.t(ctx, "music.commands.musicchannel.removed")
        await ctx.send(msg)

    @commands.hybrid_command(name="247", aliases=["24/7"])
    async def twenty_four_seven(self, ctx: commands.Context):
        """Toggle 24/7 mode"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        player.twenty_four_seven = not player.twenty_four_seven
        
        if not player.twenty_four_seven:
            if player.queue.is_empty and not player.is_playing:
                 self.start_timeout(ctx.guild.id, player)

            elif len(player.channel.members) == 1:
                 self.start_timeout(ctx.guild.id, player)
        else:
            self.cancel_timeout(ctx.guild.id)

        status = "enabled" if player.twenty_four_seven else "disabled"
        msg = await i18n.t(ctx, f"music.commands.247.{status}")
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

    @commands.hybrid_command(name="autoplay")
    async def autoplay(self, ctx: commands.Context):
        """Toggle Autoplay mode"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

        if getattr(player, 'autoplay_enabled', False):
            player.autoplay_enabled = False
            status = "disabled"
        else:
            player.autoplay_enabled = True
            status = "enabled"

        msg = await i18n.t(ctx, f"music.commands.autoplay.{status}")
        await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)

    @commands.hybrid_group(name="lastfm")
    async def lastfm(self, ctx):
        """Last.fm integration"""
        pass

    @lastfm.command(name="login")
    async def lastfm_login(self, ctx):
        """Login to Last.fm"""
        url, token = await lastfm_handler.get_auth_data()
        if not url or not token:
            msg = await i18n.t(ctx, "music.lastfm.not_configured")
            return await ctx.send(msg, delete_after=10)

        view = LastFMAuthView(ctx.author.id, url, token, self.user_model)
        msg = await i18n.t(ctx, "music.lastfm.authorize_prompt")
        await ctx.send(msg, view=view)

    @lastfm.command(name="info")
    async def lastfm_info(self, ctx):
        """Check your Last.fm status"""
        user_data = await self.user_model.get_user(ctx.author.id)
        if not user_data or 'lastfm' not in user_data:
            msg = await i18n.t(ctx, "music.lastfm.unlink.not_linked")
            return await ctx.send(msg, delete_after=10)

        lfm = user_data['lastfm']
        username = lfm.get('username', 'Unknown')
        scrobbling = lfm.get('scrobbling', True)

        status_text = await i18n.t(ctx, "music.lastfm.scrobbling_enabled") if scrobbling else await i18n.t(ctx, "music.lastfm.scrobbling_disabled")
        msg = await i18n.t(ctx, "music.lastfm.info_status", username=username, status=status_text)
        await ctx.send(msg, delete_after=10)

    @lastfm.command(name="logout")
    async def lastfm_logout(self, ctx):
        """Unlink your Last.fm account"""
        await self.user_model.remove_lastfm(ctx.author.id)
        msg = await i18n.t(ctx, "music.lastfm.logout_success")
        await ctx.send(msg, delete_after=10)

    @lastfm.command(name="scrobble")
    @app_commands.describe(state="Enable or disable scrobbling (on/off)")
    @app_commands.choices(state=[
        app_commands.Choice(name="On", value="on"),
        app_commands.Choice(name="Off", value="off")
    ])
    async def lastfm_scrobble(self, ctx, state: app_commands.Choice[str]):
        """Toggle scrobbling on/off"""
        user_data = await self.user_model.get_user(ctx.author.id)
        if not user_data or 'lastfm' not in user_data:
            msg = await i18n.t(ctx, "music.lastfm.login_first")
            return await ctx.send(msg)

        enabled = state.value == "on"
        await self.user_model.toggle_lastfm_scrobbling(ctx.author.id, enabled)

        status_text = "enabled" if enabled else "disabled"
        msg = await i18n.t(ctx, "music.lastfm.scrobble_toggled", status=status_text)
        await ctx.send(msg)


class IdlePlaylistView(discord.ui.View):
    """View shown when the player is idle, with buttons to play from playlists"""
    def __init__(self, guild_id: int, user_model, guild_model, bot, locale: str = "en"):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.user_model = user_model
        self.guild_model = guild_model
        self.bot = bot
        self.locale = locale
        
        # Pre-translate button
        self.user_playlist_button.label = i18n.get_text("music.ui.my_playlists_button", locale)
        self.server_playlist_button.label = i18n.get_text("music.ui.server_playlists_button", locale)
    
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.error(f"IdlePlaylistView error: {error}", exc_info=True)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ An error occurred: {str(error)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ An error occurred: {str(error)}", ephemeral=True)
        except Exception:
            pass
    
    @discord.ui.button(label="📂 My Playlists", style=discord.ButtonStyle.primary, row=0, custom_id="idle_playlist:user")
    async def user_playlist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            playlists = await self.user_model.get_all_playlists(interaction.user.id)
            if not playlists:
                msg = await i18n.t(interaction, "music.ui.no_user_playlists")
                await interaction.response.send_message(msg, ephemeral=True)
                return
            
            select = PlaylistSelect(playlists, self.user_model, self.guild_model, self.bot, is_user_playlist=True, owner_id=interaction.user.id, locale=self.locale)
            view = discord.ui.View(timeout=60)
            view.add_item(select)
            
            msg = await i18n.t(interaction, "music.ui.select_playlist")
            await interaction.response.send_message(msg, view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"Error in user_playlist_button: {e}", exc_info=True)
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)
                else:
                    await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)
            except Exception:
                pass
    
    @discord.ui.button(label="📁 Server Playlists", style=discord.ButtonStyle.secondary, row=0, custom_id="idle_playlist:server")
    async def server_playlist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            playlists = await self.guild_model.get_all_playlists(interaction.guild_id)
            if not playlists:
                msg = await i18n.t(interaction, "music.ui.no_server_playlists")
                await interaction.response.send_message(msg, ephemeral=True)
                return
            
            select = PlaylistSelect(playlists, self.user_model, self.guild_model, self.bot, is_user_playlist=False, owner_id=interaction.guild_id, locale=self.locale)
            view = discord.ui.View(timeout=60)
            view.add_item(select)
            
            msg = await i18n.t(interaction, "music.ui.select_playlist")
            await interaction.response.send_message(msg, view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"Error in server_playlist_button: {e}", exc_info=True)
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)
                else:
                    await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)
            except Exception:
                pass


class PlaylistSelect(discord.ui.Select):
    """Select menu for choosing a playlist to play (multi-track version)"""
    def __init__(self, playlists: dict, user_model, guild_model, bot, is_user_playlist: bool, owner_id: int, locale: str = "en"):
        self.playlists = playlists
        self.user_model = user_model
        self.guild_model = guild_model
        self.bot = bot
        self.is_user_playlist = is_user_playlist
        self.owner_id = owner_id
        self.locale = locale
        
        options = []
        for key, playlist in list(playlists.items())[:25]:
            is_imported = playlist.get('type') == 'imported'
            if is_imported:
                count = playlist.get('source_track_count', 0)
                additions = len(playlist.get('modifications', {}).get('additions', []))
                val_text = i18n.get_text("music.ui.tracks_count", locale, count=count)
                if additions > 0:
                    val_text += f" (+{additions})"
            else:
                # Regular playlist with manually added tracks
                track_count = len(playlist.get('tracks', []))
                val_text = i18n.get_text("music.ui.tracks_count", locale, count=track_count)

            name = playlist.get('name', key)
            options.append(discord.SelectOption(
                label=name[:100], 
                value=key,
                description=val_text
            ))
        
        placeholder = i18n.get_text("music.ui.choose_playlist", locale)
        no_playlists_label = i18n.get_text("music.ui.no_playlist_selected", locale)[:100]
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options if options else [discord.SelectOption(label=no_playlists_label, value="none")]
        )
    
    async def callback(self, interaction: discord.Interaction):
        playlist_key = self.values[0]
        if playlist_key == "none":
            msg = await i18n.t(interaction, "music.ui.no_playlist_selected")
            await interaction.response.send_message(msg, ephemeral=True)
            return
        
        playlist = self.playlists.get(playlist_key)
        if not playlist:
            msg = await i18n.t(interaction, "music.ui.playlist_not_found")
            await interaction.response.send_message(msg, ephemeral=True)
            return

        is_imported = playlist.get('type') == 'imported'
        has_tracks = len(playlist.get('tracks', [])) > 0
        
        if not is_imported and not has_tracks:
            msg = await i18n.t(interaction, "music.ui.playlist_empty")
            await interaction.response.send_message(msg, ephemeral=True)
            return
        
        if not interaction.user.voice or not interaction.user.voice.channel:
            msg = await i18n.t(interaction, "music.ui.not_in_voice")
            await interaction.response.send_message(msg, ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        player = pomice.NodePool.get_node().get_player(interaction.guild_id)
        if not player or not player.is_connected:
            try:
                player = await interaction.user.voice.channel.connect(cls=CustomPlayer)
                player.home_channel = interaction.channel
            except Exception as e:
                msg = await i18n.t(interaction, "music.ui.connect_failed", error=str(e))
                await interaction.followup.send(msg, ephemeral=True)
                return
        
        playlist_name = playlist.get('name', playlist_key)
        
        loading_msg = await i18n.t(interaction, "music.ui.loading_playlist", name=playlist_name)
        status_msg = await interaction.followup.send(loading_msg, ephemeral=True)
        
        try:
            count = 0
            
            if is_imported:
                # Load imported playlist using PlaylistLoader
                from utils.playlist_loader import PlaylistLoader
                
                tracks = await PlaylistLoader.load_playlist(playlist, player)
                if not tracks:
                    failed_msg = await i18n.t(interaction, "music.ui.loading_failed_url")
                    await status_msg.edit(content=failed_msg)
                    return
                
                for track in tracks:
                    track.requester = interaction.user.mention
                    player.queue.put(track)
                    count += 1
                    
                if not player.is_playing:
                    vol = await self.guild_model.get_default_volume(interaction.guild_id)
                    await player.set_volume(vol)
                    await player.play(player.queue.get())
                
                added_msg = await i18n.t(interaction, "music.ui.added_tracks", count=count, name=playlist_name)
                
                # Handle additions for imported playlists
                additions = playlist.get('modifications', {}).get('additions', [])
                if additions:
                    additions_msg = await i18n.t(interaction, "music.ui.loading_additions_custom", count=len(additions))
                    msg_content = f"{added_msg}\n{additions_msg}"
                    await status_msg.edit(content=msg_content)

                    locale = self.locale
                    
                    _done = {'value': False}
                    async def progress_callback(loaded, total):
                        try:
                            if not _done['value']:
                                if loaded % 5 == 0:
                                    progress_msg = i18n.get_text("music.ui.loading_additions_progress", locale, count=count, loaded=loaded, total=total)
                                    await status_msg.edit(content=progress_msg)
                            if not _done['value'] and (loaded == total or loaded % 5 != 0 or loaded == 0):
                                _done['value'] = True
                                final_msg = i18n.get_text("music.ui.added_with_additions_custom", locale, count=count, additions=loaded, name=playlist_name)
                                await status_msg.edit(content=final_msg)
                        except:
                            pass
                            
                    self.bot.loop.create_task(
                        PlaylistLoader.load_additions_background(additions, player, progress_callback)
                    )
                else:
                    await status_msg.edit(content=added_msg)
            else:
                # Load regular playlist with manually saved tracks
                saved_tracks = playlist.get('tracks', [])
                total_tracks = len(saved_tracks)
                
                for i, track_data in enumerate(saved_tracks):
                    url = track_data.get('url')
                    if not url:
                        continue
                    
                    try:
                        search_result = await player.get_tracks(url)
                        if search_result:
                            track = search_result[0] if isinstance(search_result, list) else search_result
                            track.requester = interaction.user.mention
                            player.queue.put(track)
                            count += 1
                            
                            # Start playing after first track is loaded
                            if count == 1 and not player.is_playing:
                                vol = await self.guild_model.get_default_volume(interaction.guild_id)
                                await player.set_volume(vol)
                                await player.play(player.queue.get())
                            
                            # Update progress every 5 tracks
                            if count % 5 == 0:
                                progress_msg = await i18n.t(interaction, "music.ui.loading_progress", loaded=count, total=total_tracks)
                                await status_msg.edit(content=progress_msg)
                    except Exception as e:
                        logger.warning(f"Failed to load track {url}: {e}")
                        continue
                
                if count == 0:
                    failed_msg = await i18n.t(interaction, "music.ui.loading_failed_url")
                    await status_msg.edit(content=failed_msg)
                    return
                
                added_msg = await i18n.t(interaction, "music.ui.added_tracks", count=count, name=playlist_name)
                await status_msg.edit(content=added_msg)
                
        except Exception as e:
            logger.error(f"Error loading playlist: {e}", exc_info=True)
            error_msg = await i18n.t(interaction, "music.ui.playlist_load_error", error=str(e))
            await status_msg.edit(content=error_msg)


class PlaylistSaveSelect(discord.ui.Select):
    """Select menu for choosing which playlist to save a track to"""
    def __init__(self, playlists: dict, track_info: dict, user_model, guild_model, is_user_playlist: bool, owner_id: int, locale: str = "en"):
        self.playlists = playlists
        self.track_info = track_info
        self.user_model = user_model
        self.guild_model = guild_model
        self.is_user_playlist = is_user_playlist
        self.owner_id = owner_id
        self.locale = locale
        
        options = []
        for key, playlist in list(playlists.items())[:25]:
            is_imported = playlist.get('type') == 'imported'
            if is_imported:
                count = playlist.get('source_track_count', 0)
                additions = len(playlist.get('modifications', {}).get('additions', []))
                val_text = i18n.get_text("music.ui.tracks_count", locale, count=count)
                if additions > 0:
                    val_text += f" (+{additions})"
            else:
                val_text = i18n.get_text("music.ui.unknown_playlist_type", locale)
            name = playlist.get('name', key)
            options.append(discord.SelectOption(
                label=name[:100], 
                value=key,
                description=val_text
            ))
        
        placeholder = i18n.get_text("music.ui.choose_playlist_save", locale)
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options
        )
    
    async def callback(self, interaction: discord.Interaction):
        playlist_key = self.values[0]
        playlist = self.playlists.get(playlist_key)
        
        if not playlist:
            msg = await i18n.t(interaction, "music.ui.playlist_not_found")
            await interaction.response.send_message(msg, ephemeral=True)
            return
        
        playlist_name = playlist.get('name', playlist_key)
        
        if self.is_user_playlist:
            success = await self.user_model.add_track_to_playlist(self.owner_id, playlist_name, self.track_info)
        else:
            success = await self.guild_model.add_track_to_playlist(self.owner_id, playlist_name, self.track_info)
        
        if success:
            track_title = self.track_info.get('title', 'Unknown')
            if self.is_user_playlist:
                msg = await i18n.t(interaction, "music.ui.settings_saved_user", title=track_title, name=playlist_name)
            else:
                msg = await i18n.t(interaction, "music.ui.settings_saved_server", title=track_title, name=playlist_name)
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            msg = await i18n.t(interaction, "music.playlist.error", error="Failed to save")
            await interaction.response.send_message(msg, ephemeral=True)


class PlayerSettingsSelect(discord.ui.Select):
    def __init__(self, player: pomice.Player, user_model, guild_model, locale: str = "en"):
        self.player = player
        self.user_model = user_model
        self.guild_model = guild_model
        self.locale = locale
        
        options = self._build_options()
        placeholder = i18n.get_text("music.ui.settings_placeholder", locale)
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            row=1
        )
    
    def _build_options(self):
        """Build options dynamically based on current player state"""
        locale = self.locale

        autoplay_on = getattr(self.player, 'autoplay_enabled', False)
        autoplay_label = i18n.get_text("music.ui.settings_autoplay_on", locale) if autoplay_on else i18n.get_text("music.ui.settings_autoplay_off", locale)
        autoplay_desc = i18n.get_text("music.ui.settings_desc_autoplay", locale)

        is_247 = getattr(self.player, 'twenty_four_seven', False)
        twenty_four_seven_label = i18n.get_text("music.ui.settings_247_on", locale) if is_247 else i18n.get_text("music.ui.settings_247_off", locale)
        twenty_four_seven_desc = i18n.get_text("music.ui.settings_desc_247", locale)
        
        options = [
            discord.SelectOption(label=autoplay_label, value="autoplay", description=autoplay_desc),
            discord.SelectOption(label=twenty_four_seven_label, value="247", description=twenty_four_seven_desc),
            discord.SelectOption(label=i18n.get_text("music.ui.settings_volume_5", locale), value="vol_5", description=i18n.get_text("music.ui.settings_desc_volume", locale, volume=5)),
            discord.SelectOption(label=i18n.get_text("music.ui.settings_volume_10", locale), value="vol_10", description=i18n.get_text("music.ui.settings_desc_volume", locale, volume=10)),
            discord.SelectOption(label=i18n.get_text("music.ui.settings_volume_25", locale), value="vol_25", description=i18n.get_text("music.ui.settings_desc_volume", locale, volume=25)),
            discord.SelectOption(label=i18n.get_text("music.ui.settings_volume_50", locale), value="vol_50", description=i18n.get_text("music.ui.settings_desc_volume", locale, volume=50)),
            discord.SelectOption(label=i18n.get_text("music.ui.settings_volume_75", locale), value="vol_75", description=i18n.get_text("music.ui.settings_desc_volume", locale, volume=75)),
            discord.SelectOption(label=i18n.get_text("music.ui.settings_volume_100", locale), value="vol_100", description=i18n.get_text("music.ui.settings_desc_volume", locale, volume=100)),
            discord.SelectOption(label=i18n.get_text("music.ui.settings_save_user", locale), value="save_user", description=i18n.get_text("music.ui.settings_desc_save_user", locale)),
            discord.SelectOption(label=i18n.get_text("music.ui.settings_save_server", locale), value="save_server", description=i18n.get_text("music.ui.settings_desc_save_server", locale)),
        ]
        return options
    
    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        cog = interaction.client.get_cog("Music")
        
        if value == "autoplay":
            if getattr(self.player, 'autoplay_enabled', False):
                self.player.autoplay_enabled = False
                msg = await i18n.t(interaction, "music.ui.settings_autoplay_disabled")
            else:
                self.player.autoplay_enabled = True
                msg = await i18n.t(interaction, "music.ui.settings_autoplay_enabled")
            
            # Update options to reflect new state
            self.options = self._build_options()
            await interaction.response.edit_message(view=self.view)
            await interaction.followup.send(msg, ephemeral=True)
        
        elif value == "247":
            self.player.twenty_four_seven = not getattr(self.player, 'twenty_four_seven', False)
            
            if self.player.twenty_four_seven:
                if cog:
                    cog.cancel_timeout(interaction.guild_id)
                msg = await i18n.t(interaction, "music.ui.settings_247_enabled")
            else:
                if cog and self.player.queue.is_empty and not self.player.is_playing:
                    cog.start_timeout(interaction.guild_id, self.player)
                msg = await i18n.t(interaction, "music.ui.settings_247_disabled")
            
            # Update options to reflect new state
            self.options = self._build_options()
            await interaction.response.edit_message(view=self.view)
            await interaction.followup.send(msg, ephemeral=True)
        
        elif value.startswith("vol_"):
            volume = int(value.split("_")[1])
            await self.player.set_volume(volume)
            msg = await i18n.t(interaction, "music.ui.settings_volume_set", volume=volume)
            await interaction.response.send_message(msg, ephemeral=True)
        
        elif value == "save_user":
            if not self.player.current:
                msg = await i18n.t(interaction, "music.ui.settings_no_track")
                await interaction.response.send_message(msg, ephemeral=True)
                return
            
            track = self.player.current
            track_info = {'title': track.title, 'url': track.uri, 'author': track.author}
            
            playlists = await self.user_model.get_all_playlists(interaction.user.id)
            
            if playlists:
                select = PlaylistSaveSelect(playlists, track_info, self.user_model, self.guild_model, is_user_playlist=True, owner_id=interaction.user.id, locale=self.locale)
                view = discord.ui.View(timeout=60)
                view.add_item(select)
                msg = await i18n.t(interaction, "music.ui.select_playlist")
                await interaction.response.send_message(msg, view=view, ephemeral=True)
            else:
                playlist_name = "Favorites"
                existing = await self.user_model.get_playlist(interaction.user.id, playlist_name)
                if not existing:
                    await self.user_model.create_playlist(interaction.user.id, playlist_name)
                
                success = await self.user_model.add_track_to_playlist(interaction.user.id, playlist_name, track_info)
                if success:
                    msg = await i18n.t(interaction, "music.ui.settings_saved_user", title=track.title, name=playlist_name)
                    await interaction.response.send_message(msg, ephemeral=True)
                else:
                    msg = await i18n.t(interaction, "music.playlist.error", error="Failed to save")
                    await interaction.response.send_message(msg, ephemeral=True)
        
        elif value == "save_server":
            if not self.player.current:
                msg = await i18n.t(interaction, "music.ui.settings_no_track")
                await interaction.response.send_message(msg, ephemeral=True)
                return
            
            if not interaction.user.guild_permissions.manage_guild:
                msg = await i18n.t(interaction, "music.ui.settings_no_permission")
                await interaction.response.send_message(msg, ephemeral=True)
                return
            
            track = self.player.current
            track_info = {'title': track.title, 'url': track.uri, 'author': track.author}

            playlists = await self.guild_model.get_all_playlists(interaction.guild_id)
            
            if playlists:
                select = PlaylistSaveSelect(playlists, track_info, self.user_model, self.guild_model, is_user_playlist=False, owner_id=interaction.guild_id, locale=self.locale)
                view = discord.ui.View(timeout=60)
                view.add_item(select)
                msg = await i18n.t(interaction, "music.ui.select_playlist")
                await interaction.response.send_message(msg, view=view, ephemeral=True)
            else:
                playlist_name = "Server Favorites"
                existing = await self.guild_model.get_playlist(interaction.guild_id, playlist_name)
                if not existing:
                    await self.guild_model.create_playlist(interaction.guild_id, playlist_name)
                
                success = await self.guild_model.add_track_to_playlist(interaction.guild_id, playlist_name, track_info)
                if success:
                    msg = await i18n.t(interaction, "music.ui.settings_saved_server", title=track.title, name=playlist_name)
                    await interaction.response.send_message(msg, ephemeral=True)
                else:
                    msg = await i18n.t(interaction, "music.playlist.error", error="Failed to save")
                    await interaction.response.send_message(msg, ephemeral=True)


class NowPlayingView(discord.ui.View):
    def __init__(self, player: pomice.Player, user_model=None, guild_model=None, locale: str = "en"):
        super().__init__(timeout=None)
        self.player = player
        self.user_model = user_model
        self.guild_model = guild_model
        self.locale = locale

        if user_model and guild_model:
            self.add_item(PlayerSettingsSelect(player, user_model, guild_model, locale=locale))

    async def async_init(self):
        await self.update_buttons()

    async def update_buttons(self):
        locale = self.locale
        gw = self.player.guild
        if gw:
            from utils.i18n import i18n
            guild_locale = await i18n.get_guild_locale(gw.id)
            if guild_locale:
                locale = guild_locale

        from utils.i18n import i18n

        if self.player.is_paused:
            self.play_pause_button.label = i18n.get_text("music.ui.resume", locale)
            self.play_pause_button.style = discord.ButtonStyle.success
            self.play_pause_button.emoji = "▶️"
        else:
            self.play_pause_button.label = i18n.get_text("music.ui.pause", locale)
            self.play_pause_button.style = discord.ButtonStyle.secondary
            self.play_pause_button.emoji = "⏸️"

        if self.player.queue.loop_mode == pomice.LoopMode.TRACK:
            self.loop_button.style = discord.ButtonStyle.primary
            self.loop_button.emoji = "🔂"
            self.loop_button.label = i18n.get_text("music.ui.loop_track", locale)
        elif self.player.queue.loop_mode == pomice.LoopMode.QUEUE:
            self.loop_button.style = discord.ButtonStyle.success
            self.loop_button.emoji = "🔁"
            self.loop_button.label = i18n.get_text("music.ui.loop_queue", locale)
        else:
            self.loop_button.style = discord.ButtonStyle.secondary
            self.loop_button.emoji = "🔁"
            self.loop_button.label = i18n.get_text("music.ui.loop_off", locale)

        self.prev_button.label = i18n.get_text("music.ui.previous", locale)
        self.skip_button.label = i18n.get_text("music.ui.next", locale)
        self.stop_button.label = i18n.get_text("music.ui.stop", locale)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, emoji="⏮️")
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        cog = interaction.client.get_cog("Music")
        if not cog:
            return

        if not hasattr(self.player, 'history') or not self.player.history:
            from utils.i18n import i18n
            msg = await i18n.t(interaction, "music.errors.no_history")
            return await interaction.followup.send(msg, ephemeral=True)

        previous_track = self.player.history.pop()
        if self.player.is_playing:
            current_track = self.player.current
            self.player.queue.put_at(0, current_track)

        self.player.queue.put_at(0, previous_track)
        await self.player.stop()

        # await interaction.followup.send(f"⏮️ Playing previous: {previous_track.title}", ephemeral=True)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, emoji="⏸️")
    async def play_pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.set_pause(not self.player.is_paused)
        await self.update_buttons()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.player.stop()

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.player.destroy()

        # Update static embed
        cog = interaction.client.get_cog("Music")
        if cog:
            await cog.update_static_embed(interaction.guild_id)

    @discord.ui.button(label="Loop", style=discord.ButtonStyle.secondary, emoji="🔁")
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.queue.is_looping:
            self.player.queue.set_loop_mode(pomice.LoopMode.QUEUE)
        elif self.player.queue.loop_mode == pomice.LoopMode.QUEUE:
            self.player.queue.set_loop_mode(pomice.LoopMode.TRACK)
        else:
            self.player.queue.disable_loop()

        await self.update_buttons()
        await interaction.response.edit_message(view=self)


class QueuePaginationView(discord.ui.View):
    def __init__(self, player, title="Music Queue", per_page=10, locale: str = "en"):
        super().__init__(timeout=60)
        self.player = player
        self.title = title
        self.per_page = per_page
        self.locale = locale

        history = list(player.history) if hasattr(player, 'history') else []
        current = [player.current] if player.current else []
        queue = list(player.queue)

        self.full_playlist = history + current + queue
        self.current_index = len(history) if current else -1

        self.total_pages = max(1, (len(self.full_playlist) + per_page - 1) // per_page)

        # Auto-set page to current song
        if self.current_index != -1:
            self.current_page = self.current_index // per_page
        else:
            self.current_page = 0

        self.message = None
        
        # Pre-translate button labels
        self.prev_button.label = i18n.get_text("music.commands.queue_view.previous_btn", locale)
        self.next_button.label = i18n.get_text("music.commands.queue_view.next_btn", locale)
        self.cancel_button.label = i18n.get_text("music.commands.queue_view.cancel_btn", locale)
        
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page == self.total_pages - 1

    def get_embed(self):
        start = self.current_page * self.per_page
        end = start + self.per_page
        current_items = self.full_playlist[start:end]
        
        title_with_count = i18n.get_text("music.commands.queue_view.title_with_count", self.locale, title=self.title, count=len(self.full_playlist))
        embed = discord.Embed(title=title_with_count, color=discord.Color.blue())
        queue_list = ""
        for i, track in enumerate(current_items):
            global_index = start + i
            num = global_index + 1

            if global_index == self.current_index:
                line = f"▶️ **{num}. [{track.title}]({track.uri}) - {track.author}**"
            else:
                line = f"{num}. [{track.title}]({track.uri}) - {track.author}"

            queue_list += line + "\n"
        
        empty_desc = i18n.get_text("music.commands.queue_view.empty_desc", self.locale)
        embed.description = queue_list or empty_desc
        
        page_footer = i18n.get_text("music.commands.queue_view.page_footer", self.locale, current=self.current_page + 1, total=self.total_pages, loop_mode=str(self.player.queue.loop_mode))
        embed.set_footer(text=page_footer)
        return embed

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.delete()
            except:
                pass

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.grey, emoji="⏮️")
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.grey, emoji="⏭️")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.message:
            try:
                await self.message.delete()
            except:
                pass
        self.stop()


class LastFMAuthView(discord.ui.View):
    def __init__(self, user_id, url, token, user_model):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.token = token
        self.url = url
        self.user_model = user_model
        self.add_item(discord.ui.Button(label="🔗 Authorize on Last.fm", url=url))

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="✅ Verify Login", style=discord.ButtonStyle.success)
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            msg = await i18n.t(interaction, "music.ui.not_for_you")
            return await interaction.response.send_message(
                msg, ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        if self.token is None:
            msg = await i18n.t(interaction, "music.ui.login_expired")
            return await interaction.followup.send(
                msg,
                ephemeral=True
            )

        try:
            session_key = await lastfm_handler.get_session_from_token(self.token, self.url)
            username = await lastfm_handler.get_username_from_session(session_key)

            if not session_key or not username:
                msg = await i18n.t(interaction, "music.lastfm.not_authorized")
                return await interaction.followup.send(
                    msg,
                    ephemeral=True)

            await self.user_model.update_lastfm(self.user_id, username, session_key)

            for child in self.children:
                child.disabled = True

            msg = await i18n.t(interaction, "music.lastfm.login_success", username=username)
            await interaction.message.edit(
                content=msg,
                view=self
            )
            self.token = None

        except Exception as e:
            msg = await i18n.t(interaction, "music.lastfm.verification_error", error=str(e))
            await interaction.followup.send(
                msg,
                ephemeral=True
            )

async def setup(bot):
    await bot.add_cog(Music(bot))
