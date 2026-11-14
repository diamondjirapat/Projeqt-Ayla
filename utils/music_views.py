import logging

import discord

from config import Config
from utils.i18n import i18n
from utils.lastfm import lastfm_handler
from utils.queue import CustomPlayer, LoopMode, apply_session_volume, remember_session_volume

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


class IdlePlaylistView(discord.ui.View):
    """View shown when the player is idle, with buttons to play from playlists"""

    def __init__(self, guild_id: int, user_model, guild_model, bot, locale: str = 'en'):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.user_model = user_model
        self.guild_model = guild_model
        self.bot = bot
        self.locale = locale

        # Pre-translate button
        self.user_playlist_button.label = i18n.get_text('music.ui.my_playlists_button', locale)
        self.server_playlist_button.label = i18n.get_text('music.ui.server_playlists_button', locale)

        self.add_item(
            discord.ui.Button(
                label=i18n.get_text('music.ui.web_player', locale),
                style=discord.ButtonStyle.link,
                url=Config.WEB_URL,
                emoji='🌐',
                row=1,
            )
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.error(f'IdlePlaylistView error: {error}', exc_info=True)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    await i18n.t(interaction, 'music.errors.command_error', error=str(error)),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    await i18n.t(interaction, 'music.errors.command_error', error=str(error)),
                    ephemeral=True,
                )
        except Exception:
            pass

    @discord.ui.button(label='📂 My Playlists', style=discord.ButtonStyle.primary, row=0, custom_id='idle_playlist:user')
    async def user_playlist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            playlists = await self.user_model.get_all_playlists(interaction.user.id)
            if not playlists:
                msg = await i18n.t(interaction, 'music.ui.no_user_playlists')
                await interaction.response.send_message(msg, ephemeral=True)
                return

            select = PlaylistSelect(
                playlists,
                self.user_model,
                self.guild_model,
                self.bot,
                is_user_playlist=True,
                owner_id=interaction.user.id,
                locale=self.locale,
            )
            view = discord.ui.View(timeout=60)
            view.add_item(select)

            msg = await i18n.t(interaction, 'music.ui.select_playlist')
            await interaction.response.send_message(msg, view=view, ephemeral=True)
        except Exception as e:
            logger.error(f'Error in user_playlist_button: {e}', exc_info=True)
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        await i18n.t(interaction, 'music.errors.command_error', error=str(e)),
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        await i18n.t(interaction, 'music.errors.command_error', error=str(e)),
                        ephemeral=True,
                    )
            except Exception:
                pass

    @discord.ui.button(
        label='📁 Server Playlists',
        style=discord.ButtonStyle.secondary,
        row=0,
        custom_id='idle_playlist:server',
    )
    async def server_playlist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            playlists = await self.guild_model.get_all_playlists(interaction.guild_id)
            if not playlists:
                msg = await i18n.t(interaction, 'music.ui.no_server_playlists')
                await interaction.response.send_message(msg, ephemeral=True)
                return

            select = PlaylistSelect(
                playlists,
                self.user_model,
                self.guild_model,
                self.bot,
                is_user_playlist=False,
                owner_id=interaction.guild_id,
                locale=self.locale,
            )
            view = discord.ui.View(timeout=60)
            view.add_item(select)

            msg = await i18n.t(interaction, 'music.ui.select_playlist')
            await interaction.response.send_message(msg, view=view, ephemeral=True)
        except Exception as e:
            logger.error(f'Error in server_playlist_button: {e}', exc_info=True)
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        await i18n.t(interaction, 'music.errors.command_error', error=str(e)),
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        await i18n.t(interaction, 'music.errors.command_error', error=str(e)),
                        ephemeral=True,
                    )
            except Exception:
                pass


class PlaylistSelect(discord.ui.Select):
    """Select menu for choosing a playlist to play (multi-track version)"""

    def __init__(
        self,
        playlists: dict,
        user_model,
        guild_model,
        bot,
        is_user_playlist: bool,
        owner_id: int,
        locale: str = 'en',
    ):
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
                val_text = i18n.get_text('music.ui.tracks_count', locale, count=count)
                if additions > 0:
                    val_text += f' (+{additions})'
            else:
                # Regular playlist with manually added tracks
                track_count = len(playlist.get('tracks', []))
                val_text = i18n.get_text('music.ui.tracks_count', locale, count=track_count)

            name = playlist.get('name', key)
            options.append(discord.SelectOption(label=name[:100], value=key, description=val_text))

        placeholder = i18n.get_text('music.ui.choose_playlist', locale)
        no_playlists_label = i18n.get_text('music.ui.no_playlist_selected', locale)[:100]
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options if options else [discord.SelectOption(label=no_playlists_label, value='none')],
        )

    async def callback(self, interaction: discord.Interaction):
        playlist_key = self.values[0]
        if playlist_key == 'none':
            msg = await i18n.t(interaction, 'music.ui.no_playlist_selected')
            await interaction.response.send_message(msg, ephemeral=True)
            return

        playlist = self.playlists.get(playlist_key)
        if not playlist:
            msg = await i18n.t(interaction, 'music.ui.playlist_not_found')
            await interaction.response.send_message(msg, ephemeral=True)
            return

        is_imported = playlist.get('type') == 'imported'
        has_tracks = len(playlist.get('tracks', [])) > 0

        if not is_imported and not has_tracks:
            msg = await i18n.t(interaction, 'music.ui.playlist_empty')
            await interaction.response.send_message(msg, ephemeral=True)
            return

        if not interaction.user.voice or not interaction.user.voice.channel:
            msg = await i18n.t(interaction, 'music.ui.not_in_voice')
            await interaction.response.send_message(msg, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        from runtime.bots import all_bots

        voice_id = interaction.user.voice.channel.id
        candidates = [bot for bot in (all_bots or [self.bot]) if bot.get_guild(interaction.guild_id)]
        playback_bot = next(
            (bot for bot in candidates if (vc := bot.get_guild(interaction.guild_id).voice_client) and vc.is_connected and vc.channel and vc.channel.id == voice_id),
            None,
        )
        if playback_bot is None:
            playback_bot = next((bot for bot in candidates if not bot.get_guild(interaction.guild_id).voice_client), None)
        if playback_bot is None:
            msg = await i18n.t(interaction, 'music.errors.voice_channel_mismatch')
            await interaction.followup.send(msg, ephemeral=True)
            return
        guild = playback_bot.get_guild(interaction.guild_id)
        player = guild.voice_client
        if not player or not player.is_connected:
            try:
                player = await guild.get_channel(voice_id).connect(cls=CustomPlayer)
                player.home_channel = interaction.channel
                cog = playback_bot.get_cog('Music')
                if cog:
                    await cog._init_player(player, interaction.guild_id, interaction.channel)
            except Exception as e:
                msg = await i18n.t(interaction, 'music.ui.connect_failed', error=str(e))
                await interaction.followup.send(msg, ephemeral=True)
                return

        playlist_name = playlist.get('name', playlist_key)

        loading_msg = await i18n.t(interaction, 'music.ui.loading_playlist', name=playlist_name)
        status_msg = await interaction.followup.send(loading_msg, ephemeral=True)

        try:
            count = 0

            if is_imported:
                # Load imported playlist using PlaylistLoader
                from utils.playlist_loader import PlaylistLoader

                tracks = await PlaylistLoader.load_playlist(playlist, player)
                if not tracks:
                    failed_msg = await i18n.t(interaction, 'music.ui.loading_failed_url')
                    await status_msg.edit(content=failed_msg)
                    return

                for track in tracks:
                    track.requester = interaction.user.mention
                    player.queue.put(track)
                    count += 1

                if not player.is_playing:
                    await apply_session_volume(player, await self.guild_model.get_default_volume(interaction.guild_id))

                    await player.play(player.queue.get(force_next=True))

                added_msg = await i18n.t(interaction, 'music.ui.added_tracks', count=count, name=playlist_name)

                # Handle additions for imported playlists
                additions = playlist.get('modifications', {}).get('additions', [])
                if additions:
                    additions_msg = await i18n.t(interaction, 'music.ui.loading_additions_custom', count=len(additions))
                    msg_content = f'{added_msg}\n{additions_msg}'
                    await status_msg.edit(content=msg_content)

                    locale = self.locale

                    _done = {'value': False}

                    async def progress_callback(loaded, total):
                        try:
                            if not _done['value']:
                                if loaded % 5 == 0:
                                    progress_msg = i18n.get_text(
                                        'music.ui.loading_additions_progress',
                                        locale,
                                        count=count,
                                        loaded=loaded,
                                        total=total,
                                    )
                                    await status_msg.edit(content=progress_msg)
                            if not _done['value'] and (loaded == total or loaded % 5 != 0 or loaded == 0):
                                _done['value'] = True
                                final_msg = i18n.get_text(
                                    'music.ui.added_with_additions_custom',
                                    locale,
                                    count=count,
                                    additions=loaded,
                                    name=playlist_name,
                                )
                                await status_msg.edit(content=final_msg)
                        except Exception:
                            pass

                    self.bot.loop.create_task(PlaylistLoader.load_additions_background(additions, player, progress_callback))
                else:
                    await status_msg.edit(content=added_msg)
            else:
                # Load regular playlist with manually saved tracks
                from utils.playlist_loader import PlaylistLoader

                saved_tracks = playlist.get('tracks', [])
                total_tracks = len(saved_tracks)

                for i, track_data in enumerate(saved_tracks):
                    try:
                        # A single bad entry is skipped; the rest of the
                        # playlist keeps loading instead of being dropped.
                        url = track_data.get('url', '') if isinstance(track_data, dict) else ''
                        if not url:
                            continue
                        search_result = await player.get_tracks(url)
                        if search_result:
                            track = PlaylistLoader.first_track(search_result)
                            track.requester = interaction.user.mention
                            player.queue.put(track)
                            count += 1

                            # Start playing after first track is loaded
                            if count == 1 and not player.is_playing:
                                await apply_session_volume(player, await self.guild_model.get_default_volume(interaction.guild_id))

                                await player.play(player.queue.get(force_next=True))

                            # Update progress every 5 tracks
                            if count % 5 == 0:
                                progress_msg = await i18n.t(
                                    interaction,
                                    'music.ui.loading_progress',
                                    loaded=count,
                                    total=total_tracks,
                                )
                                await status_msg.edit(content=progress_msg)
                    except Exception as e:
                        logger.warning(f'Failed to load track {url}: {e}')
                        continue

                if count == 0:
                    failed_msg = await i18n.t(interaction, 'music.ui.loading_failed_url')
                    await status_msg.edit(content=failed_msg)
                    return

                added_msg = await i18n.t(interaction, 'music.ui.added_tracks', count=count, name=playlist_name)
                await status_msg.edit(content=added_msg)

        except Exception as e:
            logger.error(f'Error loading playlist: {e}', exc_info=True)
            error_msg = await i18n.t(interaction, 'music.ui.playlist_load_error', error=str(e))
            await status_msg.edit(content=error_msg)


class PlaylistSaveSelect(discord.ui.Select):
    """Select menu for choosing which playlist to save a track to"""

    def __init__(
        self,
        playlists: dict,
        track_info: dict,
        user_model,
        guild_model,
        is_user_playlist: bool,
        owner_id: int,
        locale: str = 'en',
    ):
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
                val_text = i18n.get_text('music.ui.tracks_count', locale, count=count)
                if additions > 0:
                    val_text += f' (+{additions})'
            else:
                val_text = i18n.get_text('music.ui.unknown_playlist_type', locale)
            name = playlist.get('name', key)
            options.append(discord.SelectOption(label=name[:100], value=key, description=val_text))

        placeholder = i18n.get_text('music.ui.choose_playlist_save', locale)
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        playlist_key = self.values[0]
        playlist = self.playlists.get(playlist_key)

        if not playlist:
            msg = await i18n.t(interaction, 'music.ui.playlist_not_found')
            await interaction.response.send_message(msg, ephemeral=True)
            return

        playlist_name = playlist.get('name', playlist_key)

        if self.is_user_playlist:
            success = await self.user_model.add_track_to_playlist(self.owner_id, playlist_name, self.track_info)
        else:
            success = await self.guild_model.add_track_to_playlist(self.owner_id, playlist_name, self.track_info)

        if success:
            track_title = self.track_info.get('title') or await i18n.t(interaction, 'general.unknown')
            if self.is_user_playlist:
                msg = await i18n.t(interaction, 'music.ui.settings_saved_user', title=track_title, name=playlist_name)
            else:
                msg = await i18n.t(interaction, 'music.ui.settings_saved_server', title=track_title, name=playlist_name)
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            error = await i18n.t(interaction, 'music.ui.save_failed')
            msg = await i18n.t(interaction, 'music.playlist.error', error=error)
            await interaction.response.send_message(msg, ephemeral=True)


class PlayerSettingsSelect(discord.ui.Select):
    def __init__(self, player: CustomPlayer, user_model, guild_model, locale: str = 'en'):
        self.player = player
        self.user_model = user_model
        self.guild_model = guild_model
        self.locale = locale

        options = self._build_options()
        placeholder = i18n.get_text('music.ui.settings_placeholder', locale)
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options, row=1)

    def _build_options(self):
        """Build options dynamically based on current player state"""
        locale = self.locale

        autoplay_on = getattr(self.player, 'autoplay_enabled', True)
        autoplay_label = i18n.get_text('music.ui.settings_autoplay_on', locale) if autoplay_on else i18n.get_text('music.ui.settings_autoplay_off', locale)
        autoplay_desc = i18n.get_text('music.ui.settings_desc_autoplay', locale)

        is_247 = getattr(self.player, 'twenty_four_seven', True)
        twenty_four_seven_label = i18n.get_text('music.ui.settings_247_on', locale) if is_247 else i18n.get_text('music.ui.settings_247_off', locale)
        twenty_four_seven_desc = i18n.get_text('music.ui.settings_desc_247', locale)

        options = [
            discord.SelectOption(label=autoplay_label, value='autoplay', description=autoplay_desc),
            discord.SelectOption(
                label=twenty_four_seven_label,
                value='247',
                description=twenty_four_seven_desc,
            ),
            discord.SelectOption(
                label=i18n.get_text('music.ui.settings_volume_5', locale),
                value='vol_5',
                description=i18n.get_text('music.ui.settings_desc_volume', locale, volume=5),
            ),
            discord.SelectOption(
                label=i18n.get_text('music.ui.settings_volume_10', locale),
                value='vol_10',
                description=i18n.get_text('music.ui.settings_desc_volume', locale, volume=10),
            ),
            discord.SelectOption(
                label=i18n.get_text('music.ui.settings_volume_25', locale),
                value='vol_25',
                description=i18n.get_text('music.ui.settings_desc_volume', locale, volume=25),
            ),
            discord.SelectOption(
                label=i18n.get_text('music.ui.settings_volume_50', locale),
                value='vol_50',
                description=i18n.get_text('music.ui.settings_desc_volume', locale, volume=50),
            ),
            discord.SelectOption(
                label=i18n.get_text('music.ui.settings_volume_75', locale),
                value='vol_75',
                description=i18n.get_text('music.ui.settings_desc_volume', locale, volume=75),
            ),
            discord.SelectOption(
                label=i18n.get_text('music.ui.settings_volume_100', locale),
                value='vol_100',
                description=i18n.get_text('music.ui.settings_desc_volume', locale, volume=100),
            ),
            discord.SelectOption(
                label=i18n.get_text('music.ui.settings_save_user', locale),
                value='save_user',
                description=i18n.get_text('music.ui.settings_desc_save_user', locale),
            ),
            discord.SelectOption(
                label=i18n.get_text('music.ui.settings_save_server', locale),
                value='save_server',
                description=i18n.get_text('music.ui.settings_desc_save_server', locale),
            ),
        ]
        return options

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        cog = interaction.client.get_cog('Music')

        if value == 'autoplay':
            if getattr(self.player, 'autoplay_enabled', True):
                self.player.autoplay_enabled = False
                msg = await i18n.t(interaction, 'music.ui.settings_autoplay_disabled')
            else:
                self.player.autoplay_enabled = True
                msg = await i18n.t(interaction, 'music.ui.settings_autoplay_enabled')

            if cog and hasattr(cog, 'guild_model') and interaction.guild_id:
                await cog.guild_model.set_autoplay_enabled(interaction.guild_id, self.player.autoplay_enabled)

            # Update options to reflect new state
            self.options = self._build_options()
            await interaction.response.edit_message(view=self.view)
            await interaction.followup.send(msg, ephemeral=True)

        elif value == '247':
            self.player.twenty_four_seven = not getattr(self.player, 'twenty_four_seven', True)
            if cog and hasattr(cog, 'guild_model') and interaction.guild_id:
                await cog.guild_model.set_247_enabled(interaction.guild_id, self.player.twenty_four_seven)

            if self.player.twenty_four_seven:
                if cog:
                    cog.cancel_timeout(interaction.guild_id)
                msg = await i18n.t(interaction, 'music.ui.settings_247_enabled')
            else:
                if cog and self.player.queue.is_empty and not self.player.is_playing:
                    cog.start_timeout(interaction.guild_id, self.player)
                msg = await i18n.t(interaction, 'music.ui.settings_247_disabled')

            # Update options to reflect new state
            self.options = self._build_options()
            await interaction.response.edit_message(view=self.view)
            await interaction.followup.send(msg, ephemeral=True)

        elif value.startswith('vol_'):
            volume = int(value.split('_')[1])
            await self.player.set_volume(volume)
            remember_session_volume(self.player, volume)
            msg = await i18n.t(interaction, 'music.ui.settings_volume_set', volume=volume)
            await interaction.response.send_message(msg, ephemeral=True)

        elif value == 'save_user':
            if not self.player.current:
                msg = await i18n.t(interaction, 'music.ui.settings_no_track')
                await interaction.response.send_message(msg, ephemeral=True)
                return

            track = self.player.current
            track_info = {'title': track.title, 'url': track.uri, 'author': track.author}

            playlists = await self.user_model.get_all_playlists(interaction.user.id)

            if playlists:
                select = PlaylistSaveSelect(
                    playlists,
                    track_info,
                    self.user_model,
                    self.guild_model,
                    is_user_playlist=True,
                    owner_id=interaction.user.id,
                    locale=self.locale,
                )
                view = discord.ui.View(timeout=60)
                view.add_item(select)
                msg = await i18n.t(interaction, 'music.ui.select_playlist')
                await interaction.response.send_message(msg, view=view, ephemeral=True)
            else:
                playlist_name = 'Favorites'
                existing = await self.user_model.get_playlist(interaction.user.id, playlist_name)
                if not existing:
                    await self.user_model.create_playlist(interaction.user.id, playlist_name)

                success = await self.user_model.add_track_to_playlist(interaction.user.id, playlist_name, track_info)
                if success:
                    display_name = await i18n.t(interaction, 'music.ui.favorites_name')
                    msg = await i18n.t(
                        interaction,
                        'music.ui.settings_saved_user',
                        title=track.title,
                        name=display_name,
                    )
                    await interaction.response.send_message(msg, ephemeral=True)
                else:
                    error = await i18n.t(interaction, 'music.ui.save_failed')
                    msg = await i18n.t(interaction, 'music.playlist.error', error=error)
                    await interaction.response.send_message(msg, ephemeral=True)

        elif value == 'save_server':
            if not self.player.current:
                msg = await i18n.t(interaction, 'music.ui.settings_no_track')
                await interaction.response.send_message(msg, ephemeral=True)
                return

            if not interaction.user.guild_permissions.manage_guild:
                msg = await i18n.t(interaction, 'music.ui.settings_no_permission')
                await interaction.response.send_message(msg, ephemeral=True)
                return

            track = self.player.current
            track_info = {'title': track.title, 'url': track.uri, 'author': track.author}

            playlists = await self.guild_model.get_all_playlists(interaction.guild_id)

            if playlists:
                select = PlaylistSaveSelect(
                    playlists,
                    track_info,
                    self.user_model,
                    self.guild_model,
                    is_user_playlist=False,
                    owner_id=interaction.guild_id,
                    locale=self.locale,
                )
                view = discord.ui.View(timeout=60)
                view.add_item(select)
                msg = await i18n.t(interaction, 'music.ui.select_playlist')
                await interaction.response.send_message(msg, view=view, ephemeral=True)
            else:
                playlist_name = 'Server Favorites'
                existing = await self.guild_model.get_playlist(interaction.guild_id, playlist_name)
                if not existing:
                    await self.guild_model.create_playlist(interaction.guild_id, playlist_name)

                success = await self.guild_model.add_track_to_playlist(interaction.guild_id, playlist_name, track_info)
                if success:
                    display_name = await i18n.t(interaction, 'music.ui.server_favorites_name')
                    msg = await i18n.t(
                        interaction,
                        'music.ui.settings_saved_server',
                        title=track.title,
                        name=display_name,
                    )
                    await interaction.response.send_message(msg, ephemeral=True)
                else:
                    error = await i18n.t(interaction, 'music.ui.save_failed')
                    msg = await i18n.t(interaction, 'music.playlist.error', error=error)
                    await interaction.response.send_message(msg, ephemeral=True)


class NowPlayingView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        voice = getattr(interaction.user, 'voice', None)
        if not self.player.is_connected or interaction.guild.voice_client is not self.player or not voice or voice.channel != self.player.channel:
            await interaction.response.send_message(await i18n.t(interaction, 'music.errors.voice_channel_mismatch'), ephemeral=True)
            return False
        return True

    def __init__(self, player: CustomPlayer, user_model=None, guild_model=None, locale: str = 'en'):
        super().__init__(timeout=None)
        self.player = player
        self.user_model = user_model
        self.guild_model = guild_model
        self.locale = locale

        if user_model and guild_model:
            self.add_item(PlayerSettingsSelect(player, user_model, guild_model, locale=locale))

        self.add_item(
            discord.ui.Button(
                label=i18n.get_text('music.ui.web_player', locale),
                style=discord.ButtonStyle.link,
                url=Config.WEB_URL,
                emoji='🌐',
                row=2,
            )
        )

    async def async_init(self):
        await self.update_buttons()

    async def update_buttons(self):
        locale = self.locale
        gw = self.player.guild
        if gw:
            guild_locale = await i18n.get_guild_locale(gw.id)
            if guild_locale:
                locale = guild_locale

        if self.player.is_paused:
            self.play_pause_button.label = i18n.get_text('music.ui.resume', locale)
            self.play_pause_button.style = discord.ButtonStyle.success
            self.play_pause_button.emoji = '▶️'
        else:
            self.play_pause_button.label = i18n.get_text('music.ui.pause', locale)
            self.play_pause_button.style = discord.ButtonStyle.secondary
            self.play_pause_button.emoji = '⏸️'

        if self.player.queue.loop_mode == LoopMode.TRACK:
            self.loop_button.style = discord.ButtonStyle.primary
            self.loop_button.emoji = '🔂'
            self.loop_button.label = i18n.get_text('music.ui.loop_track', locale)
        elif self.player.queue.loop_mode == LoopMode.QUEUE:
            self.loop_button.style = discord.ButtonStyle.success
            self.loop_button.emoji = '🔁'
            self.loop_button.label = i18n.get_text('music.ui.loop_queue', locale)
        else:
            self.loop_button.style = discord.ButtonStyle.secondary
            self.loop_button.emoji = '🔁'
            self.loop_button.label = i18n.get_text('music.ui.loop_off', locale)

        self.prev_button.label = i18n.get_text('music.ui.previous', locale)
        self.skip_button.label = i18n.get_text('music.ui.next', locale)
        self.stop_button.label = i18n.get_text('music.ui.stop', locale)

    @discord.ui.button(label='Previous', style=discord.ButtonStyle.secondary, emoji='⏮️')
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        cog = interaction.client.get_cog('Music')
        if not cog:
            return

        if not hasattr(self.player, 'history') or not self.player.history:
            msg = await i18n.t(interaction, 'music.errors.no_history')
            return await interaction.followup.send(msg, ephemeral=True)

        previous_track = self.player.history.pop()
        if self.player.queue.loop_mode == LoopMode.QUEUE:
            await self.player.play(previous_track)
            return await interaction.followup.send(await i18n.t(interaction, 'music.player.playing_previous'), ephemeral=True)

        if self.player.is_playing:
            current_track = self.player.current
            self.player.queue.put_at(0, current_track)

        self.player.queue.put_at(0, previous_track)
        await self.player.stop()

    @discord.ui.button(label='Pause', style=discord.ButtonStyle.secondary, emoji='⏸️')
    async def play_pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.set_pause(not self.player.is_paused)
        await self.update_buttons()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label='Skip', style=discord.ButtonStyle.secondary, emoji='⏭️')
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.player.stop()

    @discord.ui.button(label='Stop', style=discord.ButtonStyle.danger, emoji='⏹️')
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await self.player.destroy()
        except Exception:
            try:
                await self.player.disconnect(force=True)
            except Exception:
                pass

        # Update static embed
        cog = interaction.client.get_cog('Music')
        if cog:
            await cog.update_static_embed(interaction.guild_id)

    @discord.ui.button(label='Loop', style=discord.ButtonStyle.secondary, emoji='🔁')
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.queue.is_looping:
            self.player.queue.set_loop_mode(LoopMode.QUEUE)
        elif self.player.queue.loop_mode == LoopMode.QUEUE:
            self.player.queue.set_loop_mode(LoopMode.TRACK)
        else:
            self.player.queue.disable_loop()

        await self.update_buttons()
        await interaction.response.edit_message(view=self)

        # Refresh the static embed so {loop_mode} placeholder stays accurate
        cog = interaction.client.get_cog('Music')
        if cog and hasattr(cog, 'update_static_embed'):
            await cog.update_static_embed(interaction.guild_id)


class QueuePaginationView(discord.ui.View):
    def __init__(self, player, title='Music Queue', per_page=10, locale: str = 'en'):
        super().__init__(timeout=60)
        self.player = player
        self.title = title
        self.per_page = per_page
        self.locale = locale

        self.full_playlist, self.current_index, _ = build_queue_timeline(player)

        self.total_pages = max(1, (len(self.full_playlist) + per_page - 1) // per_page)

        # Auto-set page to current song
        if self.current_index != -1:
            self.current_page = self.current_index // per_page
        else:
            self.current_page = 0

        self.message = None

        # Pre-translate button labels
        self.prev_button.label = i18n.get_text('music.commands.queue_view.previous_btn', locale)
        self.next_button.label = i18n.get_text('music.commands.queue_view.next_btn', locale)
        self.cancel_button.label = i18n.get_text('music.commands.queue_view.cancel_btn', locale)

        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page == self.total_pages - 1

    def get_embed(self):
        start = self.current_page * self.per_page
        end = start + self.per_page
        current_items = self.full_playlist[start:end]

        title_with_count = i18n.get_text(
            'music.commands.queue_view.title_with_count',
            self.locale,
            title=self.title,
            count=len(self.full_playlist),
        )
        embed = discord.Embed(title=title_with_count, color=discord.Color.blue())
        queue_list = ''
        for i, (track, status) in enumerate(current_items):
            global_index = start + i
            num = global_index + 1

            if global_index == self.current_index:
                line = f'[{status}] **{num}. [{track.title}]({track.uri}) - {track.author}**'
            else:
                line = f'[{status}] {num}. [{track.title}]({track.uri}) - {track.author}'

            queue_list += line + '\n'

        empty_desc = i18n.get_text('music.commands.queue_view.empty_desc', self.locale)
        embed.description = queue_list or empty_desc

        page_footer = i18n.get_text(
            'music.commands.queue_view.page_footer',
            self.locale,
            current=self.current_page + 1,
            total=self.total_pages,
            loop_mode=str(self.player.queue.loop_mode),
        )
        embed.set_footer(text=page_footer)
        return embed

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.delete()
            except Exception:
                pass

    @discord.ui.button(label='Previous', style=discord.ButtonStyle.grey, emoji='⏮️')
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label='Next', style=discord.ButtonStyle.grey, emoji='⏭️')
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.danger, emoji='✖️')
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.message:
            try:
                await self.message.delete()
            except Exception:
                pass
        self.stop()


class LastFMAuthView(discord.ui.View):
    def __init__(self, user_id, url, token, user_model, locale: str = 'en'):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.token = token
        self.url = url
        self.user_model = user_model
        self.verify.label = i18n.get_text('music.ui.verify', locale)
        self.add_item(discord.ui.Button(label=i18n.get_text('music.ui.authorize', locale), url=url))

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label='✅ Verify Login', style=discord.ButtonStyle.success)
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            msg = await i18n.t(interaction, 'music.ui.not_for_you')
            return await interaction.response.send_message(msg, ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        if self.token is None:
            msg = await i18n.t(interaction, 'music.ui.login_expired')
            return await interaction.followup.send(msg, ephemeral=True)

        try:
            session_key = await lastfm_handler.get_session_from_token(self.token, self.url)
            username = await lastfm_handler.get_username_from_session(session_key)

            if not session_key or not username:
                msg = await i18n.t(interaction, 'music.lastfm.not_authorized')
                return await interaction.followup.send(msg, ephemeral=True)

            await self.user_model.update_lastfm(self.user_id, username, session_key)

            for child in self.children:
                child.disabled = True

            msg = await i18n.t(interaction, 'music.lastfm.login_success', username=username)
            self.token = None
            try:
                await interaction.message.edit(content=msg, view=self)
            except discord.HTTPException:
                # Ephemeral prompts cannot be edited through the channel; confirm privately instead.
                await interaction.followup.send(msg, ephemeral=True)

        except Exception as e:
            msg = await i18n.t(interaction, 'music.lastfm.verification_error', error=str(e))
            await interaction.followup.send(msg, ephemeral=True)
