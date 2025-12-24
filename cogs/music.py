import discord
from discord.ext import commands
from discord import app_commands
import wavelink
import logging
import time
from typing import cast
from config import Config
from database.models import UserModel, GuildModel
from utils.lastfm import lastfm_handler

logger = logging.getLogger(__name__)


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.user_model = UserModel()
        self.guild_model = GuildModel()

    async def cog_load(self):
        nodes = [wavelink.Node(uri=Config.LAVALINK_URI, password=Config.LAVALINK_PASSWORD)]
        await wavelink.Pool.connect(nodes=nodes, client=self.bot, cache_capacity=100)

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload):
        logger.info(f"Wavelink Node connected: {payload.node.identifier}")

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        player: wavelink.Player = payload.player
        track = payload.track

        if hasattr(player, "home_channel"):
            embed = discord.Embed(title="🎵 Now Playing", description=f"**[{track.title}]({track.uri})**",
                                  color=discord.Color.blue())
            if track.artwork: embed.set_thumbnail(url=track.artwork)
            await player.home_channel.send(embed=embed)

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
        if not lastfm_handler.enabled or payload.reason != "FINISHED": return

        player = payload.player
        track = payload.track

        if player.channel:
            member_ids = [m.id for m in player.channel.members if not m.bot]
            timestamp = int(time.time())
            for user_id in member_ids:
                user_data = await self.user_model.get_user(user_id)
                if user_data and user_data.get('lastfm', {}).get('scrobbling'):
                    await lastfm_handler.scrobble(
                        user_data['lastfm']['session_key'],
                        track.author,
                        track.title,
                        timestamp
                    )

    @commands.hybrid_command(name="play")
    @app_commands.describe(query="Song name, URL, or playlist name")
    async def play(self, ctx: commands.Context, *, query: str):
        """Play a song or saved playlist"""
        if not ctx.guild:
            return

        user_data = await self.user_model.get_user(ctx.author.id)
        guild_data = await self.guild_model.get_guild(ctx.guild.id)

        if user_data and 'playlists' in user_data and query in user_data['playlists']:
            query = user_data['playlists'][query]
            await ctx.send(f"📂 Loading personal playlist: **{query}**")
        elif guild_data and 'playlists' in guild_data and query in guild_data['playlists']:
            query = guild_data['playlists'][query]
            await ctx.send(f"📂 Loading server playlist: **{query}**")

        player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
        if not player:
            try:
                player = await ctx.author.voice.channel.connect(cls=wavelink.Player)
                player.home_channel = ctx.channel
            except Exception:
                return await ctx.send("Please join a voice channel first!")

        # Search & Play
        tracks: wavelink.Search = await wavelink.Playable.search(query)
        if not tracks: return await ctx.send("No tracks found.")

        if isinstance(tracks, wavelink.Playlist):
            await player.queue.put_wait(tracks)
            await ctx.send(f"✅ Added playlist **{tracks.name}** ({len(tracks)} songs)")
        else:
            track = tracks[0]
            await player.queue.put_wait(track)
            await ctx.send(f"✅ Added **{track.title}**")

        if not player.playing:
            await player.play(player.queue.get(), volume=30)

    @commands.hybrid_group(name="playlist")
    async def playlist(self, ctx):
        """Manage personal playlists"""
        pass

    @playlist.command(name="save")
    async def playlist_save(self, ctx, name: str, url: str):
        """Save a personal playlist"""
        await self.user_model.add_playlist(ctx.author.id, name, url)
        await ctx.send(f"✅ Saved personal playlist: `{name}`")

    @playlist.command(name="delete")
    async def playlist_delete(self, ctx, name: str):
        """Delete a personal playlist"""
        await self.user_model.remove_playlist(ctx.author.id, name)
        await ctx.send(f"🗑️ Deleted personal playlist: `{name}`")

    @playlist.command(name="list")
    async def playlist_list(self, ctx):
        """List your playlists"""
        data = await self.user_model.get_user(ctx.author.id)
        if not data or 'playlists' not in data:
            return await ctx.send("You have no saved playlists.")

        embed = discord.Embed(title="Your Playlists", color=discord.Color.blue())
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
        await ctx.send(f"✅ Saved server playlist: `{name}`")

    @serverplaylist.command(name="list")
    async def server_list(self, ctx):
        data = await self.guild_model.get_guild(ctx.guild.id)
        if not data or 'playlists' not in data:
            return await ctx.send("No server playlists.")

        embed = discord.Embed(title="Server Playlists", color=discord.Color.gold())
        for name, url in data['playlists'].items():
            embed.add_field(name=name, value=url, inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="skip")
    async def skip(self, ctx: commands.Context):
        """Skip the current song"""
        player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
        if not player:
            return await ctx.send("I am not connected to a voice channel.")

        if not player.playing:
            return await ctx.send("Nothing is playing.")

        await player.skip(force=True)
        await ctx.send("⏭️ Skipped.")

    @commands.hybrid_command(name="stop")
    async def stop(self, ctx: commands.Context):
        """Stop playback and disconnect"""
        player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
        if not player:
            return await ctx.send("I am not connected.")

        await player.disconnect()
        await ctx.send("⏹️ Disconnected.")

    # TODO pagination
    @commands.hybrid_command(name="queue")
    async def queue(self, ctx: commands.Context):
        """Show the current queue"""
        player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
        if not player:
            return await ctx.send("I am not connected.")

        if player.queue.is_empty:
            return await ctx.send("Queue is empty.")

        embed = discord.Embed(title="Current Queue", color=discord.Color.blue())
        queue_list = ""
        for i, track in enumerate(player.queue[:10]):
            queue_list += f"{i + 1}. [{track.title}]({track.uri}) - {track.author}\n"

        embed.description = queue_list
        if len(player.queue) > 10:
            embed.set_footer(text=f"And {len(player.queue) - 10} more...")

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="volume")
    async def volume(self, ctx: commands.Context, volume: int):
        """Set volume (0-100)"""
        player: wavelink.Player = cast(wavelink.Player, ctx.voice_client)
        if not player:
            return await ctx.send("I am not connected.")

        if not 0 <= volume <= 100:
            return await ctx.send("Volume must be between 0 and 100.")

        await player.set_volume(volume)
        await ctx.send(f"🔊 Volume set to {volume}%")

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
            return await ctx.send("❌ You are not logged in to Last.fm.")

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


class LastFMAuthView(discord.ui.View):
    def __init__(self, user_id, url, token, user_model):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.token = token
        self.url = url
        self.user_model = user_model

        # Add Link Button
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
