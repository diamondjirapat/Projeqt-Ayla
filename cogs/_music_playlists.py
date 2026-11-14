"""Playlists behavior extracted from the music cog."""

import logging
from datetime import datetime
from typing import Optional, cast
import discord
import lava_lyra
from discord import app_commands
from discord.ext import commands
from utils.i18n import i18n
from utils.queue import (
    CustomPlayer,
    apply_session_volume,
    assert_safe_track_query,
)
from utils.autoplay import clean_artist_name as clean_artist_name
from utils.autoplay import normalize_core_title as normalize_core_title

logger = logging.getLogger(__name__)


class PlaylistCommands:
    @commands.hybrid_group(name='playlist', aliases=['pl'])
    async def playlist(self, ctx):
        """Manage personal playlists"""
        if ctx.invoked_subcommand is None:
            await self.playlist_list(ctx)

    @playlist.command(name='create')
    @app_commands.describe(name='Name for the new playlist')
    async def playlist_create(self, ctx, *, name: str):
        """Create a new empty playlist"""
        success = await self.user_model.create_playlist(ctx.author.id, name)
        if success:
            msg = await i18n.t(ctx, 'music.playlist.created', name=name)
        else:
            msg = await i18n.t(ctx, 'music.playlist.create_failed', name=name)
        await ctx.send(msg, delete_after=10)

    @playlist.command(name='add')
    @app_commands.describe(
        playlist_name='Playlist to add to',
        url='URL of the song (optional, uses current track if omitted)',
    )
    async def playlist_add(self, ctx, playlist_name: str, url: str = None):
        """Add the current song or a URL to a playlist"""
        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)

        if url:
            # Search for the track
            try:
                tracks = await self.node.get_tracks(await assert_safe_track_query(url))
                if not tracks:
                    msg = await i18n.t(ctx, 'music.playlist.track_not_found')
                    return await ctx.send(msg, delete_after=10)
                track = tracks[0] if not isinstance(tracks, lava_lyra.Playlist) else tracks.tracks[0]
                track_info = {'title': track.title, 'url': track.uri, 'author': track.author}
            except Exception as e:
                msg = await i18n.t(ctx, 'music.playlist.error', error=str(e))
                return await ctx.send(msg, delete_after=10)
        elif player and player.current:
            track = player.current
            track_info = {'title': track.title, 'url': track.uri, 'author': track.author}
        else:
            msg = await i18n.t(ctx, 'music.playlist.no_track_to_add')
            return await ctx.send(msg, delete_after=10)

        success = await self.user_model.add_track_to_playlist(ctx.author.id, playlist_name, track_info)
        if success:
            msg = await i18n.t(ctx, 'music.playlist.track_added', title=track_info['title'], playlist=playlist_name)
        else:
            msg = await i18n.t(ctx, 'music.playlist.playlist_not_found', name=playlist_name)
        await ctx.send(msg, delete_after=10)

    @playlist.command(name='remove')
    @app_commands.describe(playlist_name='Playlist to remove from', index='Track number to remove')
    async def playlist_remove(self, ctx, playlist_name: str, index: int):
        """Remove a track from a playlist by its number"""
        success, result = await self.user_model.remove_track_from_playlist(ctx.author.id, playlist_name, index - 1)

        if success:
            track_title = result.get('title') or await i18n.t(ctx, 'general.unknown')
            msg = await i18n.t(ctx, 'music.playlist.track_removed_title', title=track_title, playlist=playlist_name)
        else:
            error = result.get('error', 'unknown') if result else 'unknown'
            if error == 'playlist_not_found':
                msg = await i18n.t(ctx, 'music.playlist.playlist_not_found', name=playlist_name)
            elif error == 'empty_playlist':
                msg = await i18n.t(ctx, 'music.playlist.empty_playlist')
            elif error == 'no_additions':
                msg = await i18n.t(ctx, 'music.playlist.remove_no_additions')
            elif error == 'invalid_index':
                max_index = result.get('max_index', 0)
                msg = await i18n.t(ctx, 'music.playlist.remove_invalid_index', index=index, max=max_index)
            else:
                msg = await i18n.t(ctx, 'music.playlist.remove_failed')

        await ctx.send(msg, delete_after=10)

    @playlist.command(name='view')
    @app_commands.describe(name='Playlist to view')
    async def playlist_view(self, ctx, *, name: str):
        """View info about a playlist"""
        playlist = await self.user_model.get_playlist(ctx.author.id, name)
        if not playlist:
            msg = await i18n.t(ctx, 'music.playlist.playlist_not_found', name=name)
            return await ctx.send(msg, delete_after=10)

        is_imported = playlist.get('type') == 'imported'

        embed = discord.Embed(title=f'📂 {playlist.get("name", name)}', color=discord.Color.blue())

        if is_imported:
            source_url = playlist.get('source_url') or await i18n.t(ctx, 'general.unknown')
            track_count = playlist.get('source_track_count', 0)
            created_at = playlist.get('created_at', datetime.now()).strftime('%Y-%m-%d')

            imported_desc = await i18n.t(
                ctx,
                'music.playlist.imported_desc',
                source_url=source_url,
                track_count=track_count,
            )
            embed.description = imported_desc
            created_field = await i18n.t(ctx, 'music.playlist.created_field')
            embed.add_field(name=created_field, value=created_at, inline=True)

            modifications = playlist.get('modifications', {})
            additions = len(modifications.get('additions', []))
            removals = len(modifications.get('removals', []))

            modifications_field = await i18n.t(ctx, 'music.playlist.modifications_field')
            modifications_value = await i18n.t(
                ctx,
                'music.playlist.modifications_value',
                additions=additions,
                removals=removals,
            )
            embed.add_field(name=modifications_field, value=modifications_value, inline=True)

            if additions > 0:
                modifications_footer = await i18n.t(ctx, 'music.playlist.modifications_footer')
                embed.set_footer(text=modifications_footer)
        else:
            tracks = playlist.get('tracks', [])
            if tracks:
                track_lines = []
                for i, track in enumerate(tracks[:25], 1):
                    title = track.get('title') or await i18n.t(ctx, 'general.unknown')
                    author = track.get('author') or await i18n.t(ctx, 'general.unknown')
                    track_lines.append(f'**{i}.** {title} — {author}')
                embed.description = '\n'.join(track_lines)
                if len(tracks) > 25:
                    footer_text = await i18n.t(
                        ctx,
                        'music.playlist.view_more_tracks',
                        count=len(tracks) - 25,
                    )
                    embed.set_footer(text=footer_text)
            else:
                embed.description = await i18n.t(ctx, 'music.playlist.view_empty_desc')

        if playlist.get('cover'):
            embed.set_thumbnail(url=playlist.get('cover'))
        elif not is_imported and playlist.get('tracks') and playlist.get('tracks')[0].get('artwork'):
            embed.set_thumbnail(url=playlist.get('tracks')[0].get('artwork'))

        await ctx.send(embed=embed)

    @playlist.command(name='setcover', aliases=['cover', 'thumbnail'])
    @app_commands.describe(
        playlist_name='Playlist to set thumbnail for',
        url='Image URL for the cover (leave empty to remove)',
    )
    async def playlist_setcover(self, ctx, playlist_name: str, url: str = None):
        """Set or remove a custom thumbnail for a playlist"""
        cover_url = url.strip() if url else ''
        success = await self.user_model.set_playlist_cover(ctx.author.id, playlist_name, cover_url)
        if not success:
            msg = await i18n.t(ctx, 'music.playlist.playlist_not_found', name=playlist_name)
            return await ctx.send(msg, delete_after=10)
        if cover_url:
            await ctx.send(f'✅ Set thumbnail for playlist **{playlist_name}** to {cover_url}')
        else:
            await ctx.send(f'✅ Removed custom thumbnail for playlist **{playlist_name}**')

    @playlist.command(name='list')
    async def playlist_list(self, ctx):
        """List all your playlists"""
        playlists = await self.user_model.get_all_playlists(ctx.author.id)
        if not playlists:
            msg = await i18n.t(ctx, 'music.playlist.no_personal')
            return await ctx.send(msg, delete_after=10)

        title = await i18n.t(ctx, 'music.playlist.personal_title')
        embed = discord.Embed(title=title, color=discord.Color.blue())

        for key, playlist in list(playlists.items())[:25]:
            is_imported = playlist.get('type') == 'imported'

            if is_imported:
                count = playlist.get('source_track_count', 0)
                additions = len(playlist.get('modifications', {}).get('additions', []))
                val_text = await i18n.t(ctx, 'music.playlist.list_imported_type', count=count)
                if additions > 0:
                    additions_text = await i18n.t(ctx, 'music.playlist.list_imported_additions', additions=additions)
                    val_text += additions_text
            else:
                count = len(playlist.get('tracks', []))
                val_text = await i18n.t(ctx, 'music.playlist.list_regular_type', count=count)

            embed.add_field(name=f'📂 {playlist.get("name", key)}', value=val_text, inline=True)

        footer_text = await i18n.t(ctx, 'music.playlist.view_footer')
        embed.set_footer(text=footer_text)
        await ctx.send(embed=embed)

    @playlist.command(name='delete')
    @app_commands.describe(name='Playlist to delete')
    async def playlist_delete(self, ctx, *, name: str):
        """Delete an entire playlist"""
        success = await self.user_model.delete_playlist(ctx.author.id, name)
        if success:
            msg = await i18n.t(ctx, 'music.playlist.deleted_personal', name=name)
        else:
            msg = await i18n.t(ctx, 'music.playlist.playlist_not_found', name=name)
        await ctx.send(msg, delete_after=10)

    @playlist.command(name='play')
    @app_commands.describe(name='Playlist to play')
    async def playlist_play(self, ctx, *, name: str):
        """Play all tracks from a playlist"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        if not await self.check_voice_channel(ctx, response_channel, redirected):
            return

        playlist = await self.user_model.get_playlist(ctx.author.id, name)
        if not playlist:
            msg = await i18n.t(ctx, 'music.playlist.playlist_not_found', name=name)
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=10)

        is_imported = playlist.get('type') == 'imported'
        tracks_list = playlist.get('tracks', [])
        if not is_imported and not tracks_list:
            msg = await i18n.t(ctx, 'music.playlist.empty_playlist')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=10)
        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            if not ctx.author.voice:
                msg = await i18n.t(ctx, 'music.commands.play.not_in_voice')
                return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
            player = await ctx.author.voice.channel.connect(cls=CustomPlayer)
            await self._init_player(player, ctx.guild.id, response_channel)

        playlist_name = playlist.get('name', name)

        if is_imported:
            from utils.playlist_loader import PlaylistLoader

            loading_msg = await i18n.t(ctx, 'music.playlist.loading_imported', name=playlist_name)
            status_msg = await self.send_response(ctx, response_channel, redirected, content=loading_msg)

            try:
                # Load source
                tracks = await PlaylistLoader.load_playlist(playlist, player)
                if not tracks:
                    load_failed_msg = await i18n.t(ctx, 'music.playlist.load_failed')
                    return await status_msg.edit(content=load_failed_msg)

                # Add to queue
                count = 0
                for track in tracks:
                    track.requester = ctx.author.mention
                    player.queue.put(track)
                    count += 1

                if not player.is_playing:
                    await apply_session_volume(player, await self.guild_model.get_default_volume(ctx.guild.id))

                    await player.play(player.queue.get(force_next=True))

                msg_content = await i18n.t(ctx, 'music.playlist.added_tracks', count=count, name=playlist_name)

                # Handle additions in background
                additions = playlist.get('modifications', {}).get('additions', [])
                if additions:
                    loading_additions_msg = await i18n.t(ctx, 'music.playlist.loading_additions', count=len(additions))
                    msg_content += '\n' + loading_additions_msg
                    await status_msg.edit(content=msg_content)

                    _done = {'value': False}

                    async def progress_callback(loaded, total):
                        try:
                            if not _done['value']:
                                if loaded % 5 == 0:
                                    progress_msg = await i18n.t(
                                        ctx,
                                        'music.playlist.loading_additions_progress',
                                        count=count,
                                        loaded=loaded,
                                        total=total,
                                    )
                                    await status_msg.edit(content=progress_msg)

                            if not _done['value'] and (loaded == total or loaded % 5 != 0 or loaded == 0):
                                _done['value'] = True
                                final_msg = await i18n.t(
                                    ctx,
                                    'music.playlist.added_with_additions',
                                    count=count,
                                    additions=loaded,
                                    name=playlist_name,
                                )
                                await status_msg.edit(content=final_msg)
                        except Exception:
                            pass

                    self.bot.loop.create_task(
                        PlaylistLoader.load_additions_background(additions, player, progress_callback)
                    )
                else:
                    await status_msg.edit(content=msg_content)

                await self.update_static_embed(ctx.guild.id)

            except Exception as e:
                logger.error(f'Error loading imported playlist: {e}')
                error_msg = await i18n.t(ctx, 'music.playlist.load_error', error=str(e))
                await status_msg.edit(content=error_msg)
        else:
            count = 0
            failed = 0
            loading_msg = await i18n.t(ctx, 'music.playlist.loading_imported', name=playlist_name)
            status_msg = await self.send_response(ctx, response_channel, redirected, content=loading_msg)

            try:
                from utils.playlist_loader import PlaylistLoader

                # A track that fails to load is skipped on its own; the rest
                # of the playlist keeps loading instead of being dropped.
                resolved_tracks, failed = await PlaylistLoader.resolve_saved_tracks(
                    tracks_list, player, requester=ctx.author.mention
                )
                count = 0
                for t in resolved_tracks:
                    player.queue.put(t)
                    count += 1

                if count == 0:
                    load_failed_msg = await i18n.t(ctx, 'music.playlist.load_failed')
                    return await status_msg.edit(content=load_failed_msg)

                if not player.is_playing:
                    await apply_session_volume(player, await self.guild_model.get_default_volume(ctx.guild.id))

                    await player.play(player.queue.get(force_next=True))

                msg_content = await i18n.t(ctx, 'music.playlist.added_tracks', count=count, name=playlist_name)
                if failed > 0:
                    failed_text = await i18n.t(ctx, 'music.playlist.tracks_failed_to_load', count=failed)
                    msg_content += failed_text
                await status_msg.edit(content=msg_content)
                await self.update_static_embed(ctx.guild.id)
            except Exception as e:
                logger.error(f'Error loading regular playlist: {e}')
                error_msg = await i18n.t(ctx, 'music.playlist.load_error', error=str(e))
                await status_msg.edit(content=error_msg)

    @playlist.command(name='import')
    @app_commands.describe(
        url='YouTube/Spotify playlist URL',
        name='Custom name for the playlist (optional, uses source name if not provided)',
    )
    async def playlist_import(self, ctx, url: str, name: Optional[str] = None):
        """Import a YouTube/Spotify playlist to your saved playlists"""
        logger.info(f'[PLAYLIST] User {ctx.author.id} importing playlist from: {url[:80]}')
        status_msg = None

        try:
            msg = await i18n.t(ctx, 'music.playlist.importing')
            status_msg = await ctx.send(msg)
            try:
                tracks = await self.node.get_tracks(await assert_safe_track_query(url))
            except Exception as e:
                logger.error(f'Search failed during import: {e}')
                tracks = None

            if not tracks:
                if status_msg:
                    await status_msg.delete()
                msg = await i18n.t(ctx, 'music.playlist.import_failed')
                return await ctx.send(msg, delete_after=10)

            track_count = 0
            source_name = 'Imported Playlist'

            if isinstance(tracks, lava_lyra.Playlist):
                track_count = len(tracks.tracks)
                source_name = tracks.name
            elif isinstance(tracks, list):
                track_count = len(tracks)
                if track_count > 0:
                    source_name = 'Imported Tracks'
            else:
                track_count = 1
                source_name = tracks.title

            if track_count == 0:
                if status_msg:
                    await status_msg.delete()
                empty_msg = await i18n.t(ctx, 'music.playlist.empty_import')
                return await ctx.send(empty_msg, delete_after=10)

            playlist_name = name or source_name

            # Save as imported playlist
            success = await self.user_model.import_playlist(ctx.author.id, playlist_name, url, track_count)

            if status_msg:
                await status_msg.delete()

            if success:
                imported_msg = await i18n.t(
                    ctx,
                    'music.playlist.imported_linked',
                    name=playlist_name,
                    source=source_name,
                    count=track_count,
                )
                await ctx.send(imported_msg, delete_after=15)
            else:
                msg = await i18n.t(ctx, 'music.playlist.import_failed')
                await ctx.send(msg, delete_after=10)

        except Exception as e:
            logger.error(f'Error importing playlist: {e}', exc_info=True)
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            msg = await i18n.t(ctx, 'music.playlist.import_failed')
            await ctx.send(msg, delete_after=10)

    @commands.hybrid_group(name='serverplaylist', aliases=['spl'])
    @commands.has_permissions(manage_guild=True)
    async def serverplaylist(self, ctx):
        """Manage server playlists (Admin only)"""
        if ctx.invoked_subcommand is None:
            await self.server_list(ctx)

    @serverplaylist.command(name='create')
    # Hybrid groups force invoke_without_command=True, so group-level checks
    # never execute for subcommands - mutating ones repeat manage_guild here.
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(name='Name for the new server playlist')
    async def server_create(self, ctx, *, name: str):
        """Create a new empty server playlist"""
        success = await self.guild_model.create_playlist(ctx.guild.id, name)
        if success:
            msg = await i18n.t(ctx, 'music.playlist.created_server', name=name)
        else:
            msg = await i18n.t(ctx, 'music.playlist.create_failed', name=name)
        await ctx.send(msg, delete_after=10)

    @serverplaylist.command(name='add')
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(playlist_name='Playlist to add to', url='URL of the song')
    async def server_add(self, ctx, playlist_name: str, url: str = None):
        """Add a song to a server playlist"""
        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)

        if url:
            try:
                tracks = await self.node.get_tracks(await assert_safe_track_query(url))
                if not tracks:
                    msg = await i18n.t(ctx, 'music.playlist.track_not_found')
                    return await ctx.send(msg, delete_after=10)
                track = tracks[0] if not isinstance(tracks, lava_lyra.Playlist) else tracks.tracks[0]
                track_info = {'title': track.title, 'url': track.uri, 'author': track.author}
            except Exception as e:
                msg = await i18n.t(ctx, 'music.playlist.error', error=str(e))
                return await ctx.send(msg, delete_after=10)
        elif player and player.current:
            track = player.current
            track_info = {'title': track.title, 'url': track.uri, 'author': track.author}
        else:
            msg = await i18n.t(ctx, 'music.playlist.no_track_to_add')
            return await ctx.send(msg, delete_after=10)

        success = await self.guild_model.add_track_to_playlist(ctx.guild.id, playlist_name, track_info)
        if success:
            msg = await i18n.t(
                ctx,
                'music.playlist.track_added_server',
                title=track_info['title'],
                playlist=playlist_name,
            )
        else:
            msg = await i18n.t(ctx, 'music.playlist.playlist_not_found', name=playlist_name)
        await ctx.send(msg, delete_after=10)

    @serverplaylist.command(name='remove')
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(playlist_name='Playlist to remove from', index='Track number to remove')
    async def server_remove(self, ctx, playlist_name: str, index: int):
        """Remove a track from a server playlist"""
        success, result = await self.guild_model.remove_track_from_playlist(ctx.guild.id, playlist_name, index - 1)

        if success:
            track_title = result.get('title') or await i18n.t(ctx, 'general.unknown')
            msg = await i18n.t(ctx, 'music.playlist.track_removed_title', title=track_title, playlist=playlist_name)
        else:
            error = result.get('error', 'unknown') if result else 'unknown'
            if error == 'playlist_not_found':
                msg = await i18n.t(ctx, 'music.playlist.playlist_not_found', name=playlist_name)
            elif error == 'empty_playlist':
                msg = await i18n.t(ctx, 'music.playlist.empty_playlist')
            elif error == 'no_additions':
                msg = await i18n.t(ctx, 'music.playlist.remove_no_additions')
            elif error == 'invalid_index':
                max_index = result.get('max_index', 0)
                msg = await i18n.t(ctx, 'music.playlist.remove_invalid_index', index=index, max=max_index)
            else:
                msg = await i18n.t(ctx, 'music.playlist.remove_failed')
        await ctx.send(msg, delete_after=10)

    @serverplaylist.command(name='view')
    @app_commands.describe(name='Server playlist to view')
    async def server_view(self, ctx, *, name: str):
        """View info about a server playlist"""
        playlist = await self.guild_model.get_playlist(ctx.guild.id, name)
        if not playlist:
            msg = await i18n.t(ctx, 'music.playlist.playlist_not_found', name=name)
            return await ctx.send(msg, delete_after=10)

        is_imported = playlist.get('type') == 'imported'

        embed = discord.Embed(
            title=await i18n.t(ctx, 'music.playlist.server_view_title', name=playlist.get('name', name)),
            color=discord.Color.gold(),
        )

        if is_imported:
            source_url = playlist.get('source_url') or await i18n.t(ctx, 'general.unknown')
            track_count = playlist.get('source_track_count', 0)
            created_at = playlist.get('created_at', datetime.now()).strftime('%Y-%m-%d')

            imported_desc = await i18n.t(
                ctx,
                'music.playlist.imported_desc',
                source_url=source_url,
                track_count=track_count,
            )
            embed.description = imported_desc
            created_field = await i18n.t(ctx, 'music.playlist.created_field')
            embed.add_field(name=created_field, value=created_at, inline=True)

            modifications = playlist.get('modifications', {})
            additions = len(modifications.get('additions', []))
            removals = len(modifications.get('removals', []))

            modifications_field = await i18n.t(ctx, 'music.playlist.modifications_field')
            modifications_value = await i18n.t(
                ctx,
                'music.playlist.modifications_value',
                additions=additions,
                removals=removals,
            )
            embed.add_field(name=modifications_field, value=modifications_value, inline=True)

            if additions > 0:
                modifications_footer = await i18n.t(ctx, 'music.playlist.modifications_footer')
                embed.set_footer(text=modifications_footer)
        else:
            tracks = playlist.get('tracks', [])
            if tracks:
                track_lines = []
                for i, track in enumerate(tracks[:25], 1):
                    title = track.get('title') or await i18n.t(ctx, 'general.unknown')
                    author = track.get('author') or await i18n.t(ctx, 'general.unknown')
                    track_lines.append(f'**{i}.** {title} — {author}')
                embed.description = '\n'.join(track_lines)
                if len(tracks) > 25:
                    footer_text = await i18n.t(
                        ctx,
                        'music.playlist.view_more_tracks',
                        count=len(tracks) - 25,
                    )
                    embed.set_footer(text=footer_text)
            else:
                embed.description = await i18n.t(ctx, 'music.playlist.view_empty_server')

        if playlist.get('cover'):
            embed.set_thumbnail(url=playlist.get('cover'))
        elif not is_imported and playlist.get('tracks') and playlist.get('tracks')[0].get('artwork'):
            embed.set_thumbnail(url=playlist.get('tracks')[0].get('artwork'))

        await ctx.send(embed=embed)

    @serverplaylist.command(name='setcover', aliases=['cover', 'thumbnail'])
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        playlist_name='Server playlist to set thumbnail for',
        url='Image URL for the cover (leave empty to remove)',
    )
    async def server_setcover(self, ctx, playlist_name: str, url: str = None):
        """Set or remove a custom thumbnail for a server playlist"""
        cover_url = url.strip() if url else ''
        success = await self.guild_model.set_playlist_cover(ctx.guild.id, playlist_name, cover_url)
        if not success:
            msg = await i18n.t(ctx, 'music.playlist.playlist_not_found', name=playlist_name)
            return await ctx.send(msg, delete_after=10)
        if cover_url:
            await ctx.send(f'✅ Set thumbnail for server playlist **{playlist_name}** to {cover_url}')
        else:
            await ctx.send(f'✅ Removed custom thumbnail for server playlist **{playlist_name}**')

    @serverplaylist.command(name='list')
    async def server_list(self, ctx):
        """List all server playlists"""
        playlists = await self.guild_model.get_all_playlists(ctx.guild.id)
        if not playlists:
            msg = await i18n.t(ctx, 'music.playlist.no_server')
            return await ctx.send(msg, delete_after=10)

        title = await i18n.t(ctx, 'music.playlist.server_title')
        embed = discord.Embed(title=title, color=discord.Color.gold())

        for key, playlist in list(playlists.items())[:25]:
            is_imported = playlist.get('type') == 'imported'

            if is_imported:
                count = playlist.get('source_track_count', 0)
                additions = len(playlist.get('modifications', {}).get('additions', []))
                val_text = await i18n.t(ctx, 'music.playlist.list_imported_type', count=count)
                if additions > 0:
                    additions_text = await i18n.t(ctx, 'music.playlist.list_imported_additions', additions=additions)
                    val_text += additions_text
            else:
                count = len(playlist.get('tracks', []))
                val_text = await i18n.t(ctx, 'music.playlist.list_regular_type', count=count)

            embed.add_field(name=f'📁 {playlist.get("name", key)}', value=val_text, inline=True)

        await ctx.send(embed=embed)

    @serverplaylist.command(name='delete')
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(name='Server playlist to delete')
    async def server_delete(self, ctx, *, name: str):
        """Delete an entire server playlist"""
        success = await self.guild_model.delete_playlist(ctx.guild.id, name)
        if success:
            msg = await i18n.t(ctx, 'music.playlist.deleted_server', name=name)
        else:
            msg = await i18n.t(ctx, 'music.playlist.playlist_not_found', name=name)
        await ctx.send(msg, delete_after=10)

    @serverplaylist.command(name='play')
    @app_commands.describe(name='Server playlist to play')
    async def server_play(self, ctx, *, name: str):
        """Play all tracks from a server playlist"""
        await self.handle_command_cleanup(ctx)
        response_channel = await self.get_response_channel(ctx)
        redirected = await self.acknowledge_static_redirect(ctx)

        if not await self.check_voice_channel(ctx, response_channel, redirected):
            return

        playlist = await self.guild_model.get_playlist(ctx.guild.id, name)
        if not playlist:
            msg = await i18n.t(ctx, 'music.playlist.playlist_not_found', name=name)
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=10)

        is_imported = playlist.get('type') == 'imported'
        tracks_list = playlist.get('tracks', [])
        if not is_imported and not tracks_list:
            msg = await i18n.t(ctx, 'music.playlist.empty_playlist')
            return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=10)

        player: CustomPlayer = cast(CustomPlayer, ctx.voice_client)
        if not player:
            if not ctx.author.voice:
                msg = await i18n.t(ctx, 'music.commands.play.not_in_voice')
                return await self.send_response(ctx, response_channel, redirected, content=msg, delete_after=5)
            player = await ctx.author.voice.channel.connect(cls=CustomPlayer)
            await self._init_player(player, ctx.guild.id, response_channel)

        playlist_name = playlist.get('name', name)

        if is_imported:
            from utils.playlist_loader import PlaylistLoader

            loading_msg = await i18n.t(ctx, 'music.playlist.loading_imported', name=playlist_name)
            status_msg = await self.send_response(ctx, response_channel, redirected, content=loading_msg)

            try:
                tracks = await PlaylistLoader.load_playlist(playlist, player)
                if not tracks:
                    load_failed_msg = await i18n.t(ctx, 'music.playlist.load_failed')
                    await status_msg.edit(content=load_failed_msg)
                    return

                count = 0
                for track in tracks:
                    track.requester = ctx.author.mention
                    player.queue.put(track)
                    count += 1

                if not player.is_playing:
                    await apply_session_volume(player, await self.guild_model.get_default_volume(ctx.guild.id))

                    await player.play(player.queue.get(force_next=True))

                msg_content = await i18n.t(ctx, 'music.playlist.added_tracks', count=count, name=playlist_name)

                additions = playlist.get('modifications', {}).get('additions', [])
                if additions:
                    loading_additions_msg = await i18n.t(ctx, 'music.playlist.loading_additions', count=len(additions))
                    msg_content += '\n' + loading_additions_msg
                    await status_msg.edit(content=msg_content)

                    _done = {'value': False}

                    async def progress_callback(loaded, total):
                        try:
                            if not _done['value']:
                                if loaded % 5 == 0:
                                    progress_msg = await i18n.t(
                                        ctx,
                                        'music.playlist.loading_additions_progress',
                                        count=count,
                                        loaded=loaded,
                                        total=total,
                                    )
                                    await status_msg.edit(content=progress_msg)
                            if not _done['value'] and (loaded == total or loaded % 5 != 0 or loaded == 0):
                                _done['value'] = True
                                final_msg = await i18n.t(
                                    ctx,
                                    'music.playlist.added_with_additions',
                                    count=count,
                                    additions=loaded,
                                    name=playlist_name,
                                )
                                await status_msg.edit(content=final_msg)
                        except Exception:
                            pass

                    self.bot.loop.create_task(
                        PlaylistLoader.load_additions_background(additions, player, progress_callback)
                    )
                else:
                    await status_msg.edit(content=msg_content)

                await self.update_static_embed(ctx.guild.id)
            except Exception as e:
                logger.error(f'Error loading imported server playlist: {e}')
                error_msg = await i18n.t(ctx, 'music.playlist.load_error', error=str(e))
                await status_msg.edit(content=error_msg)
        else:
            count = 0
            failed = 0
            loading_msg = await i18n.t(ctx, 'music.playlist.loading_imported', name=playlist_name)
            status_msg = await self.send_response(ctx, response_channel, redirected, content=loading_msg)

            try:
                from utils.playlist_loader import PlaylistLoader

                # A track that fails to load is skipped on its own; the rest
                # of the playlist keeps loading instead of being dropped.
                resolved_tracks, failed = await PlaylistLoader.resolve_saved_tracks(
                    tracks_list, player, requester=ctx.author.mention
                )
                count = 0
                for t in resolved_tracks:
                    player.queue.put(t)
                    count += 1

                if count == 0:
                    load_failed_msg = await i18n.t(ctx, 'music.playlist.load_failed')
                    return await status_msg.edit(content=load_failed_msg)

                if not player.is_playing:
                    await apply_session_volume(player, await self.guild_model.get_default_volume(ctx.guild.id))

                    await player.play(player.queue.get(force_next=True))

                msg_content = await i18n.t(ctx, 'music.playlist.added_tracks', count=count, name=playlist_name)
                if failed > 0:
                    failed_text = await i18n.t(ctx, 'music.playlist.tracks_failed_to_load', count=failed)
                    msg_content += failed_text
                await status_msg.edit(content=msg_content)
                await self.update_static_embed(ctx.guild.id)
            except Exception as e:
                logger.error(f'Error loading regular server playlist: {e}')
                error_msg = await i18n.t(ctx, 'music.playlist.load_error', error=str(e))
                await status_msg.edit(content=error_msg)

    @serverplaylist.command(name='import')
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        url='YouTube/Spotify playlist URL',
        name='Custom name for the playlist (optional, uses source name if not provided)',
    )
    async def server_import(self, ctx, url: str, name: Optional[str] = None):
        """Import a YouTube/Spotify playlist to server playlists"""
        status_msg = None

        try:
            msg = await i18n.t(ctx, 'music.playlist.importing')
            status_msg = await ctx.send(msg)

            try:
                tracks = await self.node.get_tracks(await assert_safe_track_query(url))
            except Exception as e:
                logger.error(f'Search failed during server import: {e}')
                tracks = None

            if not tracks:
                if status_msg:
                    await status_msg.delete()
                msg = await i18n.t(ctx, 'music.playlist.import_failed')
                return await ctx.send(msg, delete_after=10)

            track_count = 0
            source_name = 'Imported Playlist'

            if isinstance(tracks, lava_lyra.Playlist):
                track_count = len(tracks.tracks)
                source_name = tracks.name
            elif isinstance(tracks, list):
                track_count = len(tracks)
                if track_count > 0:
                    source_name = 'Imported Tracks'
            else:
                track_count = 1
                source_name = tracks.title

            if track_count == 0:
                if status_msg:
                    await status_msg.delete()
                empty_msg = await i18n.t(ctx, 'music.playlist.empty_import')
                return await ctx.send(empty_msg, delete_after=10)

            playlist_name = name or source_name

            success = await self.guild_model.import_playlist(ctx.guild.id, playlist_name, url, track_count)

            if status_msg:
                await status_msg.delete()

            if success:
                imported_msg = await i18n.t(
                    ctx,
                    'music.playlist.imported_linked_server',
                    name=playlist_name,
                    source=source_name,
                    count=track_count,
                )
                await ctx.send(imported_msg, delete_after=15)
            else:
                msg = await i18n.t(ctx, 'music.playlist.import_failed')
                await ctx.send(msg, delete_after=10)

        except Exception as e:
            logger.error(f'Error importing server playlist: {e}', exc_info=True)
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            msg = await i18n.t(ctx, 'music.playlist.import_failed')
            await ctx.send(msg, delete_after=10)
