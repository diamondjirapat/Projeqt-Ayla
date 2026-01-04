import asyncio
import logging
import re
import time
from typing import cast

import discord
import wavelink
from discord import app_commands
from discord.ext import commands

from config import Config
from database.models import UserModel, GuildModel
from utils.i18n import i18n
from utils.lastfm import lastfm_handler

logger = logging.getLogger(__name__)

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.user_model = UserModel()
        self.guild_model = GuildModel()
        self.timeout_tasks = {}
        self.selecting_users = set()

    def start_timeout(self, guild_id, player):
        """Starts a 3-minute disconnect timer"""
        if guild_id in self.timeout_tasks:
            self.timeout_tasks[guild_id].cancel()

        async def _timeout():
            await asyncio.sleep(180)
            if player:
                await player.disconnect()
                # if player.home_channel:
                #     await player.home_channel.send("💤 I've been idle for too long. Disconnected to save resources.")

        self.timeout_tasks[guild_id] = self.bot.loop.create_task(_timeout())

    def cancel_timeout(self, guild_id):
        """Cancels the disconnect timer"""
        if guild_id in self.timeout_tasks:
            self.timeout_tasks[guild_id].cancel()
            del self.timeout_tasks[guild_id]

    async def cog_load(self):
        nodes = [wavelink.Node(uri=Config.LAVALINK_URI, password=Config.LAVALINK_PASSWORD)]
        await wavelink.Pool.connect(nodes=nodes, client=self.bot, cache_capacity=100)

    async def check_voice_channel(self, ctx: commands.Context) -> bool:
        """Check if the user is in the same voice channel as the bot"""
        if not ctx.voice_client:
            return True

        if not ctx.author.voice or ctx.author.voice.channel != ctx.voice_client.channel:
            msg = await i18n.t(ctx, "music.errors.voice_channel_mismatch")
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

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload):
        logger.info(f"Wavelink Node connected: {payload.node.identifier}")

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        player: wavelink.Player = payload.player
        track = payload.track

        player.current_track_start_time = int(time.time())
        self.cancel_timeout(player.guild.id)

        await self.update_static_embed(player.guild.id)

        static_channel_id = await self.guild_model.get_music_channel(player.guild.id)
        if hasattr(player, "home_channel"):
            if static_channel_id:
                if player.home_channel.id == static_channel_id:
                    return

            title = await i18n.t(player.home_channel, "music.player.now_playing", static_embed=False)
            embed = discord.Embed(title=title, description=f"**[{track.title}]({track.uri})**",
                                  color=discord.Color.from_rgb(255, 255, 255))

            artist_label = await i18n.t(player.home_channel, "music.player.artist", artist=track.author)
            duration_label = await i18n.t(player.home_channel, "music.player.duration_label",
                                          duration=f"{track.length // 1000 // 60}:{track.length // 1000 % 60:02d}")
            requester_label = await i18n.t(player.home_channel, "music.player.requester_label",
                                           user=getattr(track.extras, 'requester', 'Unknown'))

            embed.add_field(name=" ", value=artist_label, inline=True)
            embed.add_field(name=" ", value=duration_label, inline=True)
            embed.add_field(name=" ", value=requester_label, inline=True)

            if track.artwork: embed.set_thumbnail(url=track.artwork)

            view = NowPlayingView(player)
            await player.home_channel.send(embed=embed, view=view)

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
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        player = payload.player
        if not player:
            return

        if not hasattr(player, 'history'):
            player.history = []

        if payload.reason != "LOAD_FAILED" and payload.reason != "CLEANUP":
            player.history.append(payload.track)

        if not player.queue.is_empty:
            await player.play(player.queue.get())
        else:

            self.start_timeout(player.guild.id, player)
            await self.update_static_embed(player.guild.id)

        # logger.info(f"Track ended: {payload.track.title} (Reason: {payload.reason})")

        # if not lastfm_handler.enabled:
        #     logger.info("Last.fm not enabled, skipping scrobble")
        #     return

        # if payload.reason.lower() != "finished":
        #     logger.info(f"Track not finished (Reason: {payload.reason}), skipping scrobble")
        #     return

        track = payload.track

        if player.channel:
            member_ids = [m.id for m in player.channel.members if not m.bot]

            timestamp = getattr(player, 'current_track_start_time', int(time.time() - (track.length / 1000)))
            # logger.info(f"Attempting to scrobble track: {track.title} for members: {member_ids} at {timestamp}")

            for user_id in member_ids:
                user_data = await self.user_model.get_user(user_id)
                if user_data and user_data.get('lastfm', {}).get('scrobbling'):
                    logger.info(f"Scrobbling for user {user_id}")
                    await lastfm_handler.scrobble(
                        user_data['lastfm']['session_key'],
                        track.author,
                        track.title,
                        timestamp
                    )
                # else:
                #     logger.info(f"User {user_id} has no Last.fm data or scrobbling disabled")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        player: wavelink.Player = wavelink.Pool.get_node().get_player(member.guild.id)
        if not player or not player.channel:
            return

        # Check if bot is alone
        if len(player.channel.members) == 1 and player.channel.members[0].id == self.bot.user.id:
            self.start_timeout(member.guild.id, player)
        elif len(player.channel.members) > 1:
            self.cancel_timeout(member.guild.id)

        await self.update_static_embed(member.guild.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        # Check if bot disconnected
        if member.id == self.bot.user.id and before.channel and not after.channel:
            await self.update_static_embed(member.guild.id)
            return

        player: wavelink.Player = wavelink.Pool.get_node().get_player(member.guild.id)
        if not player or not player.channel:
            return

        # Check if bot is alone
        if len(player.channel.members) == 1 and player.channel.members[0].id == self.bot.user.id:
            self.start_timeout(member.guild.id, player)
        elif len(player.channel.members) > 1:
            self.cancel_timeout(member.guild.id)

        await self.update_static_embed(member.guild.id)

    async def update_static_embed(self, guild_id: int):
        """Updates the static music player embed"""
        channel_id = await self.guild_model.get_music_channel(guild_id)
        if not channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        player: wavelink.Player = wavelink.Pool.get_node().get_player(guild_id)

        # Determine state
        embed = discord.Embed(color=discord.Color.blurple())

        if not player or not player.connected:
            embed.title = await i18n.t(channel, "music.player.idle.title", static_embed=True)
            embed.description = await i18n.t(channel, "music.player.idle.description_disconnect", static_embed=True)
            embed.set_image(
                url="https://cdn.discordapp.com/attachments/1392428575461871717/1457324945955885217/P_idel.png")

        elif not player.playing:
            embed.title = await i18n.t(channel, "music.player.idle.title", static_embed=True)
            embed.description = await i18n.t(channel, "music.player.idle.description_empty", static_embed=True)
            embed.set_image(
                url="https://cdn.discordapp.com/attachments/1392428575461871717/1457324945955885217/P_idel.png")
        else:
            track = player.current
            now_playing_title = await i18n.t(channel, "music.player.now_playing", static_embed=True)
            artist_label = await i18n.t(channel, "music.player.artist", artist=track.author, static_embed=True)
            duration_label = await i18n.t(channel, "music.player.duration_label",
                                          duration=f"{track.length // 1000 // 60}:{track.length // 1000 % 60:02d}",
                                          static_embed=True)

            embed.title = now_playing_title
            embed.description = f"**[{track.title}]({track.uri})**\n\n{artist_label}\n{duration_label}"
            if track.artwork: embed.set_thumbnail(url=track.artwork)
            if hasattr(track.extras, 'requester'):
                req_text = await i18n.t(channel, "music.player.requester_label", user=track.extras.requester,
                                        static_embed=True)
                embed.add_field(name=" ", value=req_text)

        # Get existing message
        message_id = await self.guild_model.get_music_message(guild_id)
        message = None

        if message_id:
            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                pass

        view = NowPlayingView(player) if player and player.connected else None
        if view:
            self.bot.loop.create_task(view.async_init())

        if not message:
            message = await channel.send(embed=embed, view=view)
            await self.guild_model.set_music_message(guild_id, message.id)
        else:
            await message.edit(embed=embed, view=view)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return

        # Check static channel
        channel_id = await self.guild_model.get_music_channel(message.guild.id)
        if not channel_id or message.channel.id != channel_id:
            return
            
        if message.author.id in self.selecting_users:
            return
            
        try:
            await message.delete()
        except:
            pass

        ctx = await self.bot.get_context(message)

        query = message.content
        await self._play_logic(ctx, query, message.channel)

    async def _play_logic(self, ctx: commands.Context, query: str, response_channel: discord.TextChannel):
        """Shared logic for play command and static channel"""
        user_data = await self.user_model.get_user(ctx.author.id)
        guild_data = await self.guild_model.get_guild(ctx.guild.id)

        # Check for saved playlists first
        if user_data and 'playlists' in user_data and query in user_data['playlists']:
            query = user_data['playlists'][query]
        elif guild_data and 'playlists' in guild_data and query in guild_data['playlists']:
            query = guild_data['playlists'][query]

        # Connect logic
        player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
        if not player:
            try:
                player = await ctx.author.voice.channel.connect(cls=wavelink.Player)
                player.home_channel = response_channel
            except Exception:
                msg = await i18n.t(ctx, "music.commands.play.not_in_voice")
                return await response_channel.send(msg, delete_after=5)
        else:
            if not await self.check_voice_channel(ctx):
                return

        url_pattern = re.compile(r'https?://(?:www\.)?.+')
        is_url = url_pattern.match(query)

        tracks: wavelink.Search = await wavelink.Playable.search(query)
        if not tracks:
            msg = await i18n.t(ctx, "music.commands.play.no_results", query=query)
            return await response_channel.send(msg, delete_after=5)

        # If it's a URL or a specific Container (Playlist), auto-play/queue
        if is_url or isinstance(tracks, wavelink.Playlist):
            if isinstance(tracks, wavelink.Playlist):
                for track in tracks:
                    track.extras.requester = ctx.author.mention
                await player.queue.put_wait(tracks)
                msg = await i18n.t(ctx, "music.commands.play.playlist_added", count=len(tracks), name=tracks.name)
                await response_channel.send(msg, delete_after=5)
            else:
                track = tracks[0]
                track.extras.requester = ctx.author.mention
                await player.queue.put_wait(track)
                msg = await i18n.t(ctx, "music.commands.play.added_to_queue", title=track.title)
                await response_channel.send(msg, delete_after=5)

            if not player.playing:
                await player.play(player.queue.get(), volume=30)

            await self.update_static_embed(ctx.guild.id)
            return

        tracks_top = tracks[:5]
        select_text = await i18n.t(ctx, "music.ui.select_track")
        instructions = await i18n.t(ctx, "music.ui.selection_instructions")

        options_text = ""
        for i, track in enumerate(tracks_top):
            options_text += f"**{i + 1}.** {track.title} - {track.author} ({track.length // 1000}s)\n"

        full_msg = f"{select_text}\n{options_text}\n{instructions}"
        msg = await response_channel.send(full_msg, delete_after=60)

        def check(m):
            return m.author.id == ctx.author.id and m.channel.id == response_channel.id and m.content.isdigit()

        self.selecting_users.add(ctx.author.id)

        try:
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
                    track.extras.requester = ctx.author.mention
                    await player.queue.put_wait(track)

                    if not player.playing:
                        await player.play(player.queue.get(), volume=30)

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
    @app_commands.describe(query="Song name, URL, or playlist name")
    async def play(self, ctx: commands.Context, *, query: str):
        """Play a song or saved playlist"""
        await self.handle_command_cleanup(ctx)

        if not ctx.guild:
            return

        response_channel = await self.get_response_channel(ctx)

        static_channel_id = await self.guild_model.get_music_channel(ctx.guild.id)
        if static_channel_id and ctx.channel.id != static_channel_id:
            if ctx.interaction:
                # Slash command used outside static channel
                channel = self.bot.get_channel(static_channel_id)
                channel_mention = channel.mention if channel else f"#{static_channel_id}"

                msg_processed = await i18n.t(ctx, "music.static_response.processed", channel=channel_mention)
                msg_tip = await i18n.t(ctx, "music.static_response.tip", channel=channel_mention)

                try:
                    await ctx.interaction.response.send_message(
                        f"{msg_processed}{msg_tip}",
                        ephemeral=True
                    )
                except discord.InteractionResponded:
                    pass

        await self._play_logic(ctx, query, response_channel)

    @commands.hybrid_group(name="playlist")
    async def playlist(self, ctx):
        """Manage personal playlists"""
        pass

    @playlist.command(name="save")
    async def playlist_save(self, ctx, name: str, url: str):
        """Save a personal playlist"""
        await self.user_model.add_playlist(ctx.author.id, name, url)
        msg = await i18n.t(ctx, "music.playlist.saved_personal", name=name)
        await ctx.send(msg)

    @playlist.command(name="delete")
    async def playlist_delete(self, ctx, name: str):
        """Delete a personal playlist"""
        await self.user_model.remove_playlist(ctx.author.id, name)
        msg = await i18n.t(ctx, "music.playlist.deleted_personal", name=name)
        await ctx.send(msg)

    @playlist.command(name="list")
    async def playlist_list(self, ctx):
        """List your playlists"""
        data = await self.user_model.get_user(ctx.author.id)
        if not data or 'playlists' not in data:
            msg = await i18n.t(ctx, "music.playlist.no_personal")
            return await ctx.send(msg)

        title = await i18n.t(ctx, "music.playlist.personal_title")
        embed = discord.Embed(title=title, color=discord.Color.blue())
        for name, url in data['playlists'].items():
            embed.add_field(name=name, value=url, inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_group(name="serverplaylist")
    @commands.has_permissions(manage_guild=True)
    async def serverplaylist(self, ctx):
        """Manage server playlists (Admin only)"""
        pass

    @serverplaylist.command(name="save")
    async def server_save(self, ctx, name: str, url: str):
        await self.guild_model.add_playlist(ctx.guild.id, name, url)
        msg = await i18n.t(ctx, "music.playlist.saved_server", name=name)
        await ctx.send(msg)

    @serverplaylist.command(name="list")
    async def server_list(self, ctx):
        data = await self.guild_model.get_guild(ctx.guild.id)
        if not data or 'playlists' not in data:
            msg = await i18n.t(ctx, "music.playlist.no_server")
            return await ctx.send(msg)

        title = await i18n.t(ctx, "music.playlist.server_title")
        embed = discord.Embed(title=title, color=discord.Color.gold())
        for name, url in data['playlists'].items():
            embed.add_field(name=name, value=url, inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="previous", aliases=["prev", "back"])
    async def previous(self, ctx: commands.Context):
        """Play the previous song"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)

        if not await self.check_voice_channel(ctx):
            return

        player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
            return await response_channel.send(msg, delete_after=5)

        if not hasattr(player, 'history') or not player.history:
            msg = await i18n.t(ctx, "music.errors.no_history")
            return await response_channel.send(msg, delete_after=5)

        previous_track = player.history.pop()

        if player.playing:
            current_track = player.current
            player.queue.put_at(0, current_track)

        player.queue.put_at(0, previous_track)
        await player.skip(force=True)

        await response_channel.send(f"⏮️ Playing previous song: **{previous_track.title}**", delete_after=5)

    @commands.hybrid_command(name="skip", aliases=["s", "next"])
    async def skip(self, ctx: commands.Context):
        """Skip the current song"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)

        if not await self.check_voice_channel(ctx):
            return

        player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
            return await response_channel.send(msg, delete_after=5)

        if not player.playing:
            msg = await i18n.t(ctx, "music.commands.skip.nothing_playing")
            return await response_channel.send(msg, delete_after=5)

        await player.skip(force=True)
        msg = await i18n.t(ctx, "music.commands.skip.skipped")
        await response_channel.send(msg, delete_after=5)

    @commands.hybrid_command(name="stop", aliases=["leave", "dc", "disconnect"])
    async def stop(self, ctx: commands.Context):
        """Stop playback and disconnect"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)

        if not await self.check_voice_channel(ctx):
            return

        player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
            return await response_channel.send(msg, delete_after=5)

        await player.disconnect()
        msg = await i18n.t(ctx, "music.commands.disconnect.disconnected")
        await response_channel.send(msg, delete_after=5)
        await self.update_static_embed(ctx.guild.id)

    @commands.hybrid_command(name="queue", aliases=["q", "list"])
    async def queue(self, ctx: commands.Context):
        """Show the current queue"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)

        player = cast(wavelink.Player, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
            return await response_channel.send(msg, delete_after=5)

        if player.queue.is_empty:
            msg = await i18n.t(ctx, "music.commands.queue.empty")
            return await response_channel.send(msg, delete_after=5)

        title = await i18n.t(ctx, "music.commands.queue.title")
        view = QueuePaginationView(player.queue, title=title)
        msg = await response_channel.send(embed=view.get_embed(), view=view)
        view.message = msg

    @commands.hybrid_command(name="volume", aliases=["v", "vol"])
    async def volume(self, ctx: commands.Context, volume: int):
        """Set volume (0-100)"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)

        if not await self.check_voice_channel(ctx):
            return

        player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
        if not player:
            msg = await i18n.t(ctx, "music.commands.disconnect.not_connected")
            return await response_channel.send(msg, delete_after=5)

        if not 0 <= volume <= 100:
            msg = await i18n.t(ctx, "music.commands.volume.invalid")
            return await response_channel.send(msg, delete_after=5)

        await player.set_volume(volume)
        msg = await i18n.t(ctx, "music.commands.volume.set", volume=volume)
        await response_channel.send(msg, delete_after=5)
        await self.update_static_embed(ctx.guild.id)

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

    @commands.hybrid_group(name="lastfm")
    async def lastfm(self, ctx):
        """Last.fm integration"""
        pass

    @lastfm.command(name="login")
    async def lastfm_login(self, ctx):
        """Login to Last.fm"""
        url, token = await lastfm_handler.get_auth_data()
        if not url or not token:
            return await ctx.send("Last.fm is not configured on this bot.")

        view = LastFMAuthView(ctx.author.id, url, token, self.user_model)
        await ctx.send("Please authorize the bot by clicking the button below, then click **Verify Login**.", view=view)

    @lastfm.command(name="info")
    async def lastfm_info(self, ctx):
        """Check your Last.fm status"""
        user_data = await self.user_model.get_user(ctx.author.id)
        if not user_data or 'lastfm' not in user_data:
            msg = await i18n.t(ctx, "music.lastfm.unlink.not_linked")
            return await ctx.send(msg)

        lfm = user_data['lastfm']
        username = lfm.get('username', 'Unknown')
        scrobbling = lfm.get('scrobbling', True)

        status = "✅ Enabled" if scrobbling else "❌ Disabled"
        await ctx.send(f"👤 **Last.fm Account:** `{username}`\n📡 **Scrobbling:** {status}")

    @lastfm.command(name="logout")
    async def lastfm_logout(self, ctx):
        """Unlink your Last.fm account"""
        await self.user_model.remove_lastfm(ctx.author.id)
        await ctx.send("✅ Successfully unlinked your Last.fm account.")

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
            return await ctx.send("❌ You need to login first with `/lastfm login`.")

        enabled = state.value == "on"
        await self.user_model.toggle_lastfm_scrobbling(ctx.author.id, enabled)

        status_text = "enabled" if enabled else "disabled"
        await ctx.send(f"✅ Scrobbling has been **{status_text}**.")


class NowPlayingView(discord.ui.View):
    def __init__(self, player: wavelink.Player):
        super().__init__(timeout=None)
        self.player = player

    async def async_init(self):
        await self.update_buttons()

    async def update_buttons(self):
        locale = "en"
        gw = self.player.guild
        if gw:
            from utils.i18n import i18n
            locale = await i18n.get_guild_locale(gw.id) or "en"

        from utils.i18n import i18n

        if self.player.paused:
            self.play_pause_button.label = i18n.get_text("music.ui.resume", locale)
            self.play_pause_button.style = discord.ButtonStyle.success
            self.play_pause_button.emoji = "▶️"
        else:
            self.play_pause_button.label = i18n.get_text("music.ui.pause", locale)
            self.play_pause_button.style = discord.ButtonStyle.secondary
            self.play_pause_button.emoji = "⏸️"

        if self.player.queue.mode == wavelink.QueueMode.loop:
            self.loop_button.style = discord.ButtonStyle.primary
            self.loop_button.emoji = "🔂"
            self.loop_button.label = i18n.get_text("music.ui.loop_track", locale)
        elif self.player.queue.mode == wavelink.QueueMode.loop_all:
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
        if not cog: return

        if not hasattr(self.player, 'history') or not self.player.history:
            from utils.i18n import i18n
            msg = await i18n.t(interaction, "music.errors.no_history")
            return await interaction.followup.send(msg, ephemeral=True)

        previous_track = self.player.history.pop()
        if self.player.playing:
            current_track = self.player.current
            self.player.queue.put_at(0, current_track)

        self.player.queue.put_at(0, previous_track)
        await self.player.skip(force=True)

        # await interaction.followup.send(f"⏮️ Playing previous: {previous_track.title}", ephemeral=True)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, emoji="⏸️")
    async def play_pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.pause(not self.player.paused)
        await self.update_buttons()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.player.skip(force=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.player.disconnect()

        # Update static embed
        cog = interaction.client.get_cog("Music")
        if cog:
            await cog.update_static_embed(interaction.guild_id)

    @discord.ui.button(label="Loop", style=discord.ButtonStyle.secondary, emoji="🔁")
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.player.queue.mode == wavelink.QueueMode.normal:
            self.player.queue.mode = wavelink.QueueMode.loop_all
        elif self.player.queue.mode == wavelink.QueueMode.loop_all:
            self.player.queue.mode = wavelink.QueueMode.loop
        else:
            self.player.queue.mode = wavelink.QueueMode.normal

        await self.update_buttons()
        await interaction.response.edit_message(view=self)


class QueuePaginationView(discord.ui.View):
    def __init__(self, queue, title="Music Queue", per_page=10):
        super().__init__(timeout=60)
        self.queue = queue
        self.title = title
        self.per_page = per_page
        self.current_page = 0
        self.total_pages = max(1, (len(queue) + per_page - 1) // per_page)
        self.message = None
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page == self.total_pages - 1

    def get_embed(self):
        start = self.current_page * self.per_page
        end = start + self.per_page
        current_items = self.queue[start:end]

        embed = discord.Embed(title=f"{self.title} ({len(self.queue)} songs)", color=discord.Color.blue())
        queue_list = ""
        for i, track in enumerate(current_items):
            queue_list += f"{start + i + 1}. [{track.title}]({track.uri}) - {track.author}\n"

        embed.description = queue_list or "Queue is empty."
        embed.set_footer(text=f"Page {self.current_page + 1}/{self.total_pages}")
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
            return await interaction.response.send_message(
                "This button is not for you.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        if self.token is None:
            return await interaction.followup.send(
                "❌ This login session has expired. Please run `/lastfm login` again.",
                ephemeral=True
            )

        try:
            session_key = await lastfm_handler.get_session_from_token(self.token, self.url)
            username = await lastfm_handler.get_username_from_session(session_key)

            if not session_key or not username:
                return await interaction.followup.send(
                    "❌ Not authorized yet. Please authorize first.",
                    ephemeral=True)

            await self.user_model.update_lastfm(self.user_id, username, session_key)

            for child in self.children:
                child.disabled = True

            await interaction.message.edit(
                content=f"✅ Successfully logged in as **{username}**!",
                view=self
            )
            self.token = None

        except Exception as e:
            await interaction.followup.send(
                f"❌ Error during verification: `{e}`",
                ephemeral=True
            )

async def setup(bot):
    await bot.add_cog(Music(bot))
