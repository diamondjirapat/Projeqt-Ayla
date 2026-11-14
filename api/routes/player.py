"""Player internal HTTP endpoints."""

from collections.abc import Callable
from typing import Any, Optional, cast
from fastapi import FastAPI, Body, HTTPException, Header, Query
from api.dependencies.player import PlayerDependencies
from api.dependencies.validation import validate_dependencies
from api.contracts.player import ControlResult, PlayerControl, PlayerListItem, PlayerState, SearchResult
from config import Config
from services.serialization import serialize_track
from services.music.gapless import sync_gapless_queue
from services.music.state import timeline_revision as player_timeline_revision
from utils.queue import (
    CustomPlayer,
    LoopMode,
    apply_session_volume,
    move_current_to_history,
    move_current_to_queue,
    remember_session_volume,
)
from utils.web_validation import assert_public_url_host, split_url as _split_url
from utils.discord_members import fetch_missing_member
import asyncio
import lava_lyra
import logging
import re

logger = logging.getLogger(__name__)


def register_routes(
    app: FastAPI,
    provide: Callable[[], PlayerDependencies],
) -> dict[str, Callable[..., Any]]:
    validate_dependencies(provide(), PlayerDependencies)

    @app.get('/internal/player/list', response_model=list[PlayerListItem], response_model_exclude_unset=True)
    async def internal_player_list(user_id: int = Header(..., alias='X-User-Id')):
        services = provide()
        if not services.bot_instance:
            return []

        is_admin = int(user_id) in Config.OWNER_IDS
        limiter = asyncio.Semaphore(5)

        async def describe(player):
            if not isinstance(player, CustomPlayer):
                return None
            member = player.guild.get_member(user_id)
            if not member and not is_admin:
                try:
                    async with limiter:
                        member = await fetch_missing_member(player.guild, user_id)
                except Exception:
                    return None
            user_has_perm = is_admin or (member and (member.guild_permissions.administrator or member.guild_permissions.manage_guild))
            if user_has_perm:
                return {
                    'guild_id': str(player.guild.id),
                    'guild_name': player.guild.name,
                    'channel_name': player.channel.name,
                    'current_track': player.current.title if player.current else None,
                    'is_playing': player.is_playing,
                }
            return None

        players = await asyncio.gather(*(describe(player) for player in services.get_all_voice_clients()))
        return [player for player in players if player is not None]

    @app.get('/internal/player/state', response_model=PlayerState, response_model_exclude_unset=True)
    async def internal_player_state(
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: Optional[int] = None,
        timeline_revision: Optional[str] = Query(None, max_length=64),
    ):
        services = provide()
        if not services.bot_instance:
            return {'connected': False, 'msg': 'Bot is starting...'}

        is_admin = int(user_id) in Config.OWNER_IDS
        active_player: Optional[CustomPlayer] = None

        # 1. Try to find player in the voice channel the user is currently in
        for player in services.get_all_voice_clients():
            if not isinstance(player, CustomPlayer):
                continue
            voice_state = services._get_guild_voice_state(player.guild, user_id)
            if voice_state and voice_state.channel and voice_state.channel.id == player.channel.id:
                active_player = player
                break

        # 2. Only if user is admin, check target guild or fallback to first active player
        if not active_player and is_admin:
            if guild_id:
                for player in services.get_all_voice_clients():
                    if not isinstance(player, CustomPlayer):
                        continue
                    if player.guild.id == guild_id:
                        active_player = player
                        break

            if not active_player:
                for player in services.get_all_voice_clients():
                    if not isinstance(player, CustomPlayer):
                        continue
                    active_player = player
                    break

        if not active_player:
            # Check if user is in any voice channel at all to offer connection
            user_voice_channel = None
            for guild in services.get_all_guilds():
                voice_state = services._get_guild_voice_state(guild, user_id)
                if voice_state and voice_state.channel:
                    user_voice_channel = {
                        'guild_id': str(guild.id),
                        'guild_name': guild.name,
                        'channel_id': str(voice_state.channel.id),
                        'channel_name': voice_state.channel.name,
                    }
                    break
            default_vol = 25
            if user_voice_channel:
                default_vol = await services.guild_model.get_default_volume(int(user_voice_channel['guild_id']))
            elif guild_id:
                default_vol = await services.guild_model.get_default_volume(guild_id)

            return {
                'connected': False,
                'guild_id': None,
                'guild_name': '',
                'channel_id': None,
                'channel_name': '',
                'bitrate': None,
                'is_playing': False,
                'is_paused': False,
                'volume': default_vol,
                'position': 0,
                'loop_mode': 'OFF',
                'autoplay': True,
                'current_track': None,
                'queue': [],
                'history': [],
                'user_voice': user_voice_channel,
            }

        # Get current track details
        track_info = serialize_track(active_player.current) if active_player.current else None

        revision = player_timeline_revision(active_player)
        timeline = {'timeline_revision': revision}
        if timeline_revision != revision:
            timeline['queue'] = [serialize_track(track) for track in active_player.queue._queue]
            timeline['history'] = [serialize_track(track) for track in getattr(active_player, 'history', [])]

        bitrate = min(getattr(active_player.channel, 'bitrate', 64000) // 1000, 160)
        return {
            'connected': True,
            'guild_id': str(active_player.guild.id),
            'guild_name': active_player.guild.name,
            'channel_id': str(active_player.channel.id),
            'channel_name': active_player.channel.name,
            'bitrate': bitrate,
            'is_playing': active_player.is_playing,
            'is_paused': active_player.is_paused,
            'volume': active_player.volume,
            'position': active_player.position,
            'loop_mode': active_player.queue.loop_mode.name,
            'autoplay': getattr(active_player, 'autoplay_enabled', True),
            'gapless': getattr(active_player, 'gapless_enabled', True),
            'filters': [],
            'current_track': track_info,
            **timeline,
        }

    @app.post('/internal/player/control', response_model=ControlResult, response_model_exclude_unset=True)
    async def internal_player_control(
        user_id: int = Header(..., alias='X-User-Id'),
        payload: PlayerControl = Body(...),
    ):
        services = provide()
        data = payload.model_dump(exclude_unset=True)
        if not services.bot_instance:
            raise HTTPException(status_code=503, detail='Bot is starting')

        action = data.get('action')
        value = data.get('value')

        # Locate active player
        active_player: Optional[CustomPlayer] = None
        for player in services.get_all_voice_clients():
            if not isinstance(player, CustomPlayer):
                continue
            voice_state = services._get_guild_voice_state(player.guild, user_id)
            if voice_state and voice_state.channel and voice_state.channel.id == player.channel.id:
                active_player = player
                break

        guild_id = data.get('guild_id')
        is_admin = int(user_id) in Config.OWNER_IDS
        if not active_player and is_admin and guild_id:
            for player in services.get_all_voice_clients():
                if not isinstance(player, CustomPlayer):
                    continue
                if str(player.guild.id) == str(guild_id) or player.guild.id == guild_id:
                    active_player = player
                    break

        if not active_player and action != 'play':
            raise HTTPException(status_code=400, detail="You must be in the bot's voice channel to control playback.")

        try:
            if action == 'play':
                query_str = None
                if isinstance(value, dict):
                    query_str = value.get('uri') or value.get('url') or value.get('query')
                    if not query_str:
                        title = value.get('title')
                        author = value.get('author')
                        if title and author:
                            query_str = f'ytsearch:{title} {author}'
                        elif title:
                            query_str = f'ytsearch:{title}'
                        elif author:
                            query_str = f'ytsearch:{author}'
                elif isinstance(value, str):
                    query_str = value.strip()
                elif value is not None:
                    query_str = str(value).strip()

                if not query_str:
                    raise HTTPException(status_code=400, detail='No track or URL provided to play.')
                # Find user voice channel to connect if not already connected
                voice_channel = None
                searched_guilds = []
                for guild in services.get_all_guilds():
                    searched_guilds.append(f'{guild.name}({guild.id})')
                    voice_state = services._get_guild_voice_state(guild, user_id)
                    if voice_state and voice_state.channel:
                        voice_channel = voice_state.channel
                        break

                if not voice_channel:
                    logger.warning(f'[WEB] Voice channel not found for user_id={user_id}. Searched guilds: {searched_guilds}')
                    for guild in services.get_all_guilds():
                        if guild._voice_states:
                            logger.warning(f'[WEB] Guild {guild.name} ({guild.id}) voice states: {list(guild._voice_states.keys())}')
                    raise HTTPException(status_code=400, detail='You must be in a Discord voice channel to play music.')

                if not active_player:
                    # Find if already in that guild across all bots
                    for pc in services.get_all_voice_clients():
                        if pc.guild.id == voice_channel.guild.id:
                            active_player = cast(CustomPlayer, pc)
                            break

                if not active_player:
                    all_bots = getattr(services.bot_instance, 'all_bots', [services.bot_instance])
                    guild_bots = [b for b in all_bots if b.get_guild(voice_channel.guild.id) is not None]
                    if not guild_bots:
                        raise HTTPException(status_code=400, detail='No bots are present in this server.')

                    selected_bot = None
                    for b in guild_bots:
                        is_busy = any(vc.guild.id == voice_channel.guild.id for vc in b.voice_clients)
                        if not is_busy:
                            selected_bot = b
                            break

                    if not selected_bot:
                        selected_bot = guild_bots[0]

                    bot_guild = selected_bot.get_guild(voice_channel.guild.id)
                    bot_voice_channel = bot_guild.get_channel(voice_channel.id) if bot_guild else None

                    if not bot_voice_channel:
                        raise HTTPException(status_code=400, detail='Could not connect bot to your voice channel.')

                    logger.info(f'[WEB] Connecting {selected_bot.user} to voice channel {bot_voice_channel.name} in guild {bot_guild.name}')
                    active_player = await bot_voice_channel.connect(cls=CustomPlayer)
                    active_player.home_channel = bot_voice_channel

                    # Initialize autoplay_enabled from user settings
                    db_user = await services.user_model.get_user(user_id)
                    if db_user and db_user.get('settings', {}).get('autoplay') is not None:
                        active_player.autoplay_enabled = bool(db_user['settings']['autoplay'])

                if _split_url(query_str):
                    await assert_public_url_host(query_str)

                tracks = await active_player.get_tracks(query_str)
                if not tracks:
                    raise HTTPException(status_code=404, detail='No results found for query')

                # Resolve the web user's display name for the requester field
                # instead of showing a raw Discord user ID in the embed.
                web_member = voice_channel.guild.get_member(user_id)
                if not web_member:
                    try:
                        web_member = await voice_channel.guild.fetch_member(user_id)
                    except Exception:
                        web_member = None
                requester_name = web_member.display_name if web_member else f'{user_id} (Web)'

                if isinstance(tracks, lava_lyra.Playlist):
                    for track in tracks.tracks:
                        track.requester = requester_name
                        active_player.queue.put(track)
                else:
                    track = tracks[0]
                    track.requester = requester_name
                    active_player.queue.put(track)

                if not active_player.is_playing:
                    await apply_session_volume(active_player, await services.guild_model.get_default_volume(active_player.guild.id))

                    await active_player.play(active_player.queue.get(force_next=True))

            elif action == 'pause':
                await active_player.set_pause(True)

            elif action == 'resume':
                await active_player.set_pause(False)

            elif action == 'skip':
                if active_player.queue.is_empty:
                    # Save current to history before stopping
                    if active_player.current:
                        if not hasattr(active_player, 'history'):
                            active_player.history = []
                        if not active_player.history or active_player.history[-1] is not active_player.current:
                            active_player.history.append(active_player.current)
                    await active_player.stop()
                else:
                    # Save current to history before playing next
                    if active_player.current:
                        if not hasattr(active_player, 'history'):
                            active_player.history = []
                        if not active_player.history or active_player.history[-1] is not active_player.current:
                            active_player.history.append(active_player.current)
                    next_track = active_player.queue.get(force_next=True)
                    await active_player.play(next_track)

            elif action == 'previous':
                if active_player.history:
                    prev_track = active_player.history.pop()
                    if active_player.current:
                        active_player.queue.put_at_front(active_player.current)
                    await active_player.play(prev_track)
                else:
                    raise HTTPException(status_code=400, detail='No history to go back to')

            elif action == 'volume':
                vol = int(value)
                vol = max(0, min(vol, 150))
                await active_player.set_volume(vol)
                remember_session_volume(active_player, vol)

            elif action == 'seek':
                try:
                    pos = int(value)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail='Seek position must be an integer.')
                current = active_player.current
                if not current:
                    raise HTTPException(status_code=400, detail='No track currently playing')
                if getattr(current, 'is_stream', False):
                    raise HTTPException(status_code=400, detail='This track does not support seeking')
                if getattr(current, 'is_seekable', True) is False:
                    raise HTTPException(status_code=400, detail='This track does not support seeking')
                # Bound against the resolved track length (what player.seek
                # enforces) so provider lengths that differ from it fail here
                # instead of erroring mid-seek.
                resolved = getattr(current, 'original', None) or current
                track_length = getattr(resolved, 'length', 0) or 0
                if not 0 <= pos <= track_length:
                    raise HTTPException(status_code=400, detail='Seek position out of range')
                try:
                    await active_player.seek(pos)
                except lava_lyra.exceptions.TrackInvalidPosition as e:
                    raise HTTPException(status_code=400, detail='Seek position out of range') from e

            elif action == 'shuffle':
                active_player.queue.shuffle()

            elif action == 'repeat':
                if value == 'track':
                    active_player.queue.set_loop_mode(LoopMode.TRACK)
                elif value == 'queue':
                    active_player.queue.set_loop_mode(LoopMode.QUEUE)
                else:
                    active_player.queue.set_loop_mode(LoopMode.OFF)

            elif action == 'autoplay':
                if value is not None:
                    active_player.autoplay_enabled = bool(value)
                else:
                    active_player.autoplay_enabled = not getattr(active_player, 'autoplay_enabled', True)
                # Sync back to DB settings
                db_user = await services.user_model.get_user(user_id)
                if db_user:
                    await services.user_model.update_user(user_id, {'settings.autoplay': active_player.autoplay_enabled})

            elif action == 'queue_remove':
                active_player.queue.remove_at(int(value))

            elif action == 'queue_jump':
                # Jump to a specific index in the upcoming queue.
                # All tracks before the target index are skipped and saved to history.
                target_index = int(value)
                if not 0 <= target_index < len(active_player.queue._queue):
                    raise HTTPException(status_code=400, detail='Queue index out of range')
                if not hasattr(active_player, 'history'):
                    active_player.history = []
                # Save current track to history
                if active_player.current:
                    if not active_player.history or active_player.history[-1] is not active_player.current:
                        active_player.history.append(active_player.current)
                # Save all tracks being skipped over to history
                for _ in range(target_index):
                    skipped = active_player.queue._queue.pop(0)
                    if not active_player.history or active_player.history[-1] is not skipped:
                        active_player.history.append(skipped)
                # Play the target track
                target_track = active_player.queue.get(force_next=True)
                await active_player.play(target_track)

            elif action == 'history_jump':
                # Replay a track from history by its index.
                # History tracks after the target + current track are pushed back to queue front.
                target_index = int(value)
                if not hasattr(active_player, 'history'):
                    active_player.history = []
                if not 0 <= target_index < len(active_player.history):
                    raise HTTPException(status_code=400, detail='History index out of range')
                # Grab the target track before mutating history
                target_track = active_player.history[target_index]
                # Tracks in history after the target go back to the queue front (in order)
                tracks_to_requeue = active_player.history[target_index + 1 :]
                # Trim history to everything before the target
                active_player.history = active_player.history[:target_index]
                # Push current track to queue front first (it will play after target)
                if active_player.current:
                    active_player.queue.put_at_front(active_player.current)
                # Push orphaned history tracks back to queue front in reverse so order is preserved
                for t in reversed(tracks_to_requeue):
                    active_player.queue.put_at_front(t)
                # Play the target history track
                await active_player.play(target_track)

            elif action == 'queue_clear':
                active_player.queue.clear()
                if hasattr(active_player, 'history'):
                    active_player.history.clear()

            elif action == 'queue_move':
                # Reorder: move track from index_from to index_to within the upcoming queue
                index_from = int(value['from'])
                index_to = int(value['to'])
                active_player.queue.move(index_from, index_to)

            elif action == 'history_move':
                # Reorder: move a track within the history list
                index_from = int(value['from'])
                index_to = int(value['to'])
                if not hasattr(active_player, 'history'):
                    active_player.history = []
                history = active_player.history
                if not (0 <= index_from < len(history) and 0 <= index_to < len(history)):
                    raise HTTPException(status_code=400, detail='History index out of range')
                track = history.pop(index_from)
                history.insert(index_to, track)

            elif action == 'history_to_queue':
                # Move a history track into the upcoming queue at a specific position
                history_index = int(value['history_index'])
                queue_index = int(value['queue_index'])
                if not hasattr(active_player, 'history'):
                    active_player.history = []
                if not (0 <= history_index < len(active_player.history)):
                    raise HTTPException(status_code=400, detail='History index out of range')
                track = active_player.history.pop(history_index)
                insert_at = min(queue_index, len(active_player.queue._queue))
                active_player.queue.put_at_index(insert_at, track)

            elif action == 'queue_to_history':
                # Move an upcoming track back into history at a specific position
                queue_index = int(value['queue_index'])
                history_index = int(value['history_index'])
                if not (0 <= queue_index < len(active_player.queue._queue)):
                    raise HTTPException(status_code=400, detail='Queue index out of range')
                if not hasattr(active_player, 'history'):
                    active_player.history = []
                track = active_player.queue.remove_at(queue_index)
                insert_at = min(history_index, len(active_player.history))
                active_player.history.insert(insert_at, track)

            elif action == 'current_to_queue':
                # Move the playing track toward the upcoming side, keeping playback.
                if not active_player.current:
                    raise HTTPException(status_code=400, detail='No track currently playing')
                queue_index = int(value.get('queue_index', 0)) if isinstance(value, dict) else int(value)
                move_current_to_queue(active_player, queue_index)

            elif action == 'current_to_history':
                # Move the playing track toward the history side, keeping playback.
                if not active_player.current:
                    raise HTTPException(status_code=400, detail='No track currently playing')
                history_index = int(value.get('history_index', 0)) if isinstance(value, dict) else int(value)
                move_current_to_history(active_player, history_index)

            if action in {
                'play',
                'previous',
                'shuffle',
                'repeat',
                'queue_remove',
                'queue_jump',
                'history_jump',
                'queue_clear',
                'queue_move',
                'history_to_queue',
                'queue_to_history',
                'current_to_queue',
                'current_to_history',
            }:
                await sync_gapless_queue(active_player)
            return {'success': True}
        except HTTPException:
            raise
        except lava_lyra.exceptions.NodeRestException as e:
            logger.error(f'[WEB] Control error from node REST: {e}', exc_info=True)
            # If the player was deleted or not found on the node, clean up the stale player
            if '404' in str(e) or 'Player not found' in str(e):
                if active_player:
                    try:
                        await active_player.disconnect(force=True)
                    except Exception:
                        pass
                raise HTTPException(
                    status_code=404,
                    detail='Player session was lost or disconnected. Please re-join voice and try again.',
                )
            raise HTTPException(status_code=502, detail=f'Audio server error: {e}')
        except (lava_lyra.exceptions.NoNodesAvailable, lava_lyra.exceptions.NodeNotAvailable) as e:
            logger.error(f'[WEB] Control error: No nodes available: {e}')
            raise HTTPException(
                status_code=503,
                detail='Audio server is currently offline. Please try again shortly.',
            )
        except lava_lyra.exceptions.TrackLoadError as e:
            logger.error(f'[WEB] Control error: Track load failed: {e}')
            raise HTTPException(
                status_code=400,
                detail='Failed to load the requested track.',
            )
        except Exception as e:
            logger.error(f'[WEB] Control error: {e}', exc_info=True)
            # Do not echo raw exception text to API callers (information leak).
            raise HTTPException(status_code=500, detail='Internal error while controlling playback.')

    @app.post('/internal/player/sync-autoplay')
    async def internal_sync_autoplay(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        if not services.bot_instance:
            return {'synced': False}

        autoplay = bool(data.get('autoplay', True))
        target_player: Optional[CustomPlayer] = None
        for player in services.get_all_voice_clients():
            if not isinstance(player, CustomPlayer):
                continue
            voice_state = services._get_guild_voice_state(player.guild, user_id)
            if voice_state and voice_state.channel and voice_state.channel.id == player.channel.id:
                target_player = player
                break

        guild_id = data.get('guild_id')
        is_admin = int(user_id) in Config.OWNER_IDS
        if not target_player and is_admin and guild_id:
            for player in services.get_all_voice_clients():
                if not isinstance(player, CustomPlayer):
                    continue
                if str(player.guild.id) == str(guild_id) or player.guild.id == guild_id:
                    target_player = player
                    break

        if target_player:
            target_player.autoplay_enabled = autoplay
            return {'synced': True, 'autoplay': target_player.autoplay_enabled}

        return {'synced': False}

    @app.get('/internal/player/search', response_model=SearchResult, response_model_exclude_unset=True)
    async def internal_player_search(
        user_id: int = Header(..., alias='X-User-Id'),
        q: str = Query(..., min_length=1),
        mode: str = 'youtube',
    ):
        services = provide()
        if not services.bot_instance:
            return {'mode': mode, 'is_playlist': False, 'playlist_name': None, 'tracks': [], 'playlists': []}

        try:
            explicit_search_prefixes = (
                'ytsearch:',
                'ytmsearch:',
                'scsearch:',
                'spsearch:',
                'dzsearch:',
                'amsearch:',
                'bcsearch:',
            )
            try:
                node = lava_lyra.NodePool.get_node()
            except Exception:
                node = None

            url_pattern = re.compile(r'https?://(?:www\.)?.+')
            is_url = bool(url_pattern.match(q))

            if is_url:
                await assert_public_url_host(q)

            if not node:
                return {'mode': mode, 'is_playlist': False, 'playlist_name': None, 'tracks': [], 'playlists': []}

            if is_url or q.lower().startswith(explicit_search_prefixes):
                tracks = await node.get_tracks(q)
            elif mode in ('youtubemusic', 'ytm'):
                tracks = await node.get_tracks(f'ytmsearch:{q}')
            else:
                # Default to regular YouTube search
                tracks = await node.get_tracks(f'ytsearch:{q}')

            results = []
            if isinstance(tracks, lava_lyra.Playlist):
                # Return ALL playlist tracks (no cap), with playlist metadata
                for t in tracks.tracks:
                    results.append(serialize_track(t))
                return {
                    'mode': mode,
                    'is_playlist': True,
                    'playlist_name': getattr(tracks, 'name', None) or 'Playlist',
                    'playlist_url': getattr(tracks, 'uri', None) or getattr(tracks, 'url', None) or q,
                    'tracks': results,
                    'playlists': [],
                }
            elif tracks:
                for t in tracks[:15]:
                    results.append(serialize_track(t))
            return {'mode': mode, 'is_playlist': False, 'playlist_name': None, 'tracks': results, 'playlists': []}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f'[WEB] Search error: {e}')
            return {'mode': mode, 'is_playlist': False, 'playlist_name': None, 'tracks': [], 'playlists': []}

    @app.get('/internal/player/lyrics')
    async def internal_player_lyrics(
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: Optional[int] = None,
    ):
        """Fetch lyrics for the current track."""
        services = provide()
        if not services.bot_instance:
            return {'found': False, 'lyrics': None}

        active_player: Optional[CustomPlayer] = None
        for player in services.get_all_voice_clients():
            if not isinstance(player, CustomPlayer):
                continue
            voice_state = services._get_guild_voice_state(player.guild, user_id)
            if voice_state and voice_state.channel and voice_state.channel.id == player.channel.id:
                active_player = player
                break

        if not active_player:
            is_admin = int(user_id) in Config.OWNER_IDS
            if is_admin and guild_id:
                for player in services.get_all_voice_clients():
                    if not isinstance(player, CustomPlayer):
                        continue
                    if player.guild.id == guild_id:
                        active_player = player
                        break

        if not active_player or not active_player.current:
            return {'found': False, 'lyrics': None}

        try:
            lyrics_data = await active_player.fetch_lyrics()
            if not lyrics_data:
                return {'found': False, 'lyrics': None}

            lines = []
            for line in lyrics_data.lines:
                lines.append(
                    {
                        'text': line.text,
                        'time': line.time,
                        'duration': line.duration,
                    }
                )

            return {
                'found': True,
                'lyrics': {
                    'synced': lyrics_data.synced,
                    'provider': lyrics_data.provider or lyrics_data.name or 'Unknown',
                    'lang': lyrics_data.lang,
                    'text': lyrics_data.text,
                    'lines': lines,
                },
            }
        except Exception as e:
            logger.error(f'[WEB] Lyrics error: {e}')
            return {'found': False, 'lyrics': None}

    return {
        'internal_player_list': internal_player_list,
        'internal_player_state': internal_player_state,
        'internal_player_control': internal_player_control,
        'internal_sync_autoplay': internal_sync_autoplay,
        'internal_player_search': internal_player_search,
        'internal_player_lyrics': internal_player_lyrics,
    }
