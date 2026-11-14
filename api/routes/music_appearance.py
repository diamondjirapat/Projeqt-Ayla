"""Music Appearance internal HTTP endpoints."""

from collections.abc import Callable
from typing import Any, Dict, Optional, Literal
from fastapi import FastAPI, Body, HTTPException, Header, Query
from api.dependencies.music_appearance import MusicAppearanceDependencies
from api.dependencies.validation import validate_dependencies
from utils.music_embed import (
    build_custom_music_embed,
    build_music_channel_embed,
    get_default_music_embed_config,
    get_default_music_channel_embed_config,
    normalize_http_url,
    normalize_music_embed_config,
    normalize_music_channel_embed_config,
    refresh_music_embed_config_attachments,
)
import asyncio
from utils.i18n import i18n


def register_routes(
    app: FastAPI,
    provide: Callable[[], MusicAppearanceDependencies],
) -> dict[str, Callable[..., Any]]:
    validate_dependencies(provide(), MusicAppearanceDependencies)

    async def get_config(services, guild_id, target):
        if target == 'channel':
            return await services.guild_model.get_music_channel_embed_config(guild_id)
        return await services.guild_model.get_music_embed_config(guild_id)

    async def get_preview_channel(services, guild_id, target):
        primary_bot = services.bot_instance or services.bot
        bots = getattr(primary_bot, 'all_bots', [primary_bot])
        if target == 'voice':
            # Voice controls live in the connected player's channel chat, not
            # the configured text channel that contains the player directory.
            for bot in bots:
                guild = bot.get_guild(guild_id) if bot else None
                player = getattr(guild, 'voice_client', None)
                if player and player.is_connected and player.channel:
                    return player.channel
            return None
        channel_id = await services.guild_model.get_music_channel(guild_id)
        if channel_id:
            for bot in bots:
                channel = bot.get_channel(channel_id) if bot else None
                if channel:
                    return channel
        guild = services._find_guild(guild_id)
        return getattr(guild, 'system_channel', None) or next(iter(getattr(guild, 'text_channels', [])), None)

    @app.get('/internal/guild/music-embed')
    async def internal_get_music_embed(
        user_id: Optional[str] = Header(None, alias='X-User-ID'),
        guild_id: Optional[int] = Query(None),
        target: Literal['voice', 'channel'] = Query('voice'),
    ):
        services = provide()
        if guild_id is None:
            raise HTTPException(status_code=400, detail='guild_id is required.')
        if not user_id:
            raise HTTPException(status_code=403, detail='You do not have permission to manage this server.')
        try:
            uid = int(user_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=403, detail='You do not have permission to manage this server.')

        if not await services.has_guild_permission(guild_id, uid, 'manage_guild'):
            raise HTTPException(status_code=403, detail='You do not have permission to manage this server.')

        config = await get_config(services, guild_id, target)
        await refresh_music_embed_config_attachments(config)
        channel = await get_preview_channel(services, guild_id, target)
        return {
            'success': True,
            'preview_channel': {
                'id': str(channel.id),
                'name': str(channel.name),
            }
            if channel
            else None,
            'config': config,
            'defaults': get_default_music_channel_embed_config()
            if target == 'channel'
            else get_default_music_embed_config(),
            'banner_url': await services._resolve_music_banner_url(),
            'bar_url': await services._resolve_music_bar_url(),
        }

    @app.post('/internal/guild/music-embed')
    async def internal_save_music_embed(
        data: Dict[str, Any] = Body(...),
        x_user_id: Optional[str] = Header(None, alias='X-User-ID'),
    ):
        services = provide()
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail='Request body must be a JSON object.')

        guild_id_raw = data.get('guild_id')
        if guild_id_raw is None or guild_id_raw == '':
            raise HTTPException(status_code=400, detail='guild_id is required.')
        try:
            guild_id = int(guild_id_raw)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail='guild_id must be an integer.')

        if not x_user_id:
            raise HTTPException(status_code=403, detail='You do not have permission to manage this server.')
        try:
            uid = int(x_user_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=403, detail='You do not have permission to manage this server.')

        if not await services.has_guild_permission(guild_id, uid, 'manage_guild'):
            raise HTTPException(status_code=403, detail='You do not have permission to manage this server.')

        config = data.get('config')
        if not isinstance(config, dict):
            raise HTTPException(status_code=400, detail='config must be an object.')

        await refresh_music_embed_config_attachments(config)
        target = data.get('target', 'voice')
        if target == 'channel':
            await services.guild_model.set_music_channel_embed_config(guild_id, config)
        else:
            await services.guild_model.set_music_embed_config(guild_id, config)

        target_bot = services.bot_instance or services.bot
        if target_bot:
            for b in getattr(target_bot, 'all_bots', [target_bot]):
                music_cog = b.get_cog('Music')
                update_method = '_update_static_embed' if target == 'channel' else 'update_static_embed'
                if music_cog and hasattr(music_cog, update_method):
                    try:
                        loop = b.loop
                    except AttributeError:
                        loop = asyncio.get_event_loop()
                    loop.create_task(getattr(music_cog, update_method)(guild_id))

        saved = await get_config(services, guild_id, target)
        return {
            'success': True,
            'config': saved,
            'banner_url': await services._resolve_music_banner_url(),
            'bar_url': await services._resolve_music_bar_url(),
        }

    @app.post('/internal/guild/music-embed/reset')
    async def internal_reset_music_embed(
        data: Dict[str, Any] = Body(...),
        x_user_id: Optional[str] = Header(None, alias='X-User-ID'),
    ):
        services = provide()
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail='Request body must be a JSON object.')

        guild_id_raw = data.get('guild_id')
        if guild_id_raw is None or guild_id_raw == '':
            raise HTTPException(status_code=400, detail='guild_id is required.')
        try:
            guild_id = int(guild_id_raw)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail='guild_id must be an integer.')

        if not x_user_id:
            raise HTTPException(status_code=403, detail='You do not have permission to manage this server.')
        try:
            uid = int(x_user_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=403, detail='You do not have permission to manage this server.')

        if not await services.has_guild_permission(guild_id, uid, 'manage_guild'):
            raise HTTPException(status_code=403, detail='You do not have permission to manage this server.')

        target = data.get('target', 'voice')
        if target == 'channel':
            await services.guild_model.reset_music_channel_embed_config(guild_id)
        else:
            await services.guild_model.reset_music_embed_config(guild_id)

        target_bot = services.bot_instance or services.bot
        if target_bot:
            for b in getattr(target_bot, 'all_bots', [target_bot]):
                music_cog = b.get_cog('Music')
                update_method = '_update_static_embed' if target == 'channel' else 'update_static_embed'
                if music_cog and hasattr(music_cog, update_method):
                    try:
                        loop = b.loop
                    except AttributeError:
                        loop = asyncio.get_event_loop()
                    loop.create_task(getattr(music_cog, update_method)(guild_id))

        saved = await get_config(services, guild_id, target)
        return {
            'success': True,
            'config': saved,
            'banner_url': await services._resolve_music_banner_url(),
            'bar_url': await services._resolve_music_bar_url(),
        }

    @app.post('/internal/guild/music-embed/preview')
    async def internal_preview_music_embed(
        data: Dict[str, Any] = Body(...),
        x_user_id: Optional[str] = Header(None, alias='X-User-ID'),
    ):
        """Render the exact Discord embed the bot would send, so the dashboard preview matches Discord.

        Uses the same ``build_custom_music_embed`` + placeholder resolution as
        ``Music.update_static_embed`` (including ``MUSIC_BANNER_URL``/``BAR_URL``),
        so an empty ``image_url`` falls back to the default banner/bar instead of
        rendering blank on one side only.
        """
        services = provide()
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail='Request body must be a JSON object.')
        guild_id_raw = data.get('guild_id')
        try:
            guild_id = int(guild_id_raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail='guild_id must be an integer.')
        if not x_user_id:
            raise HTTPException(status_code=403, detail='You do not have permission to manage this server.')
        try:
            uid = int(x_user_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=403, detail='You do not have permission to manage this server.')
        if not await services.has_guild_permission(guild_id, uid, 'manage_guild'):
            raise HTTPException(status_code=403, detail='You do not have permission to manage this server.')

        guild = services._find_guild(guild_id)
        if guild is None:
            raise HTTPException(status_code=404, detail='Server not found.')

        target = data.get('target', 'voice')
        raw_config = data.get('config')
        if raw_config is None:
            config = await get_config(services, guild_id, target)
        elif not isinstance(raw_config, dict):
            raise HTTPException(status_code=400, detail='config must be an object.')
        else:
            config = (
                normalize_music_channel_embed_config(raw_config)
                if target == 'channel'
                else normalize_music_embed_config(raw_config)
            )

        await refresh_music_embed_config_attachments(config)

        requested_state = str(data.get('state') or '').strip().lower()
        states = [requested_state] if requested_state in ('idle', 'now_playing') else ['idle', 'now_playing']
        if target == 'channel':
            states = ['idle']

        channel = await get_preview_channel(services, guild_id, target)

        channel_name = str(getattr(channel, 'name', '') or '')
        channel_mention = getattr(channel, 'mention', None) or (f'#{channel_name}' if channel_name else '')
        guild_name = str(getattr(guild, 'name', '') or '')
        guild_icon = getattr(guild, 'icon', None)
        server_icon = str(guild_icon.url) if guild_icon else ''
        bot_user = getattr(services.bot_instance or services.bot, 'user', None)
        bot_name = str(getattr(bot_user, 'name', '') or 'Projeqt Ayla')
        display_avatar = getattr(bot_user, 'display_avatar', None)
        bot_avatar = str(getattr(display_avatar, 'url', '') or '')

        resolved_banner = await services._resolve_music_banner_url()
        resolved_bar = await services._resolve_music_bar_url()
        # Sample artwork keeps the now-playing thumbnail slot visible, matching the
        # live embed layout (which shows the real track art) and the dashboard preview.
        sample_artwork = normalize_http_url('/music-track-artwork.png')

        base_placeholders: Dict[str, str] = {
            'server': guild_name,
            'guild': guild_name,
            'server_icon': server_icon,
            'channel': channel_name,
            'channel_mention': str(channel_mention),
            'prefix': '!',
            'bot_name': bot_name,
            'bot_avatar': bot_avatar,
            'banner': resolved_banner,
            'banner_url': resolved_banner,
            'music_banner': resolved_banner,
            'music_banner_url': resolved_banner,
            'bar': resolved_bar,
            'bar_url': resolved_bar,
            'volume': '100',
            'loop_mode': 'Off',
            'queue_length': '0',
        }
        if target == 'channel':
            locale = await i18n.get_guild_locale(guild_id) or 'en'
            sample_url = f'https://discord.com/channels/{guild_id}/{getattr(channel, "id", guild_id)}'
            base_placeholders.update(
                {
                    'directory_title': i18n.get_text('music.player.directory.title', locale),
                    'directory_description': i18n.get_text('music.player.directory.description', locale),
                    'players': i18n.get_text(
                        'music.player.directory.entry', locale, bot=bot_name, channel='#Music Lounge', url=sample_url
                    ),
                    'active_player_count': '1',
                }
            )

        # Idle matches the live idle send in Music._update_static_embed_inner, where
        # track placeholders resolve to empty strings (there is no track).
        idle_placeholders = {
            **base_placeholders,
            'title': '',
            'artist': '',
            'author': '',
            'duration': '',
            'url': '',
            'uri': '',
            'source': '',
            'requester': '',
            'requester_mention': '',
            'artwork': '',
            'thumbnail': '',
        }
        now_playing_placeholders = {
            **base_placeholders,
            'title': 'Echoes of Ayla',
            'artist': 'Ayla',
            'author': 'Ayla',
            'duration': '3:33',
            'url': 'https://youtu.be/dQw4w9WgXcQ?si=aNcP1Mcr3yp200ZC',
            'uri': 'https://youtu.be/dQw4w9WgXcQ?si=aNcP1Mcr3yp200ZC',
            'source': 'YouTube',
            'requester': 'Preview',
            'requester_mention': 'Preview',
            'artwork': sample_artwork,
            'thumbnail': sample_artwork,
        }

        previews: Dict[str, Any] = {}
        for state in states:
            placeholders = idle_placeholders if state == 'idle' else now_playing_placeholders
            embed, content = (
                build_music_channel_embed(guild_id, config, placeholders)
                if target == 'channel'
                else build_custom_music_embed(guild_id, state, config, placeholders)
            )
            previews[state] = {'embed': embed.to_dict(), 'content': content}

        return {
            'success': True,
            'previews': previews,
            'banner_url': resolved_banner,
            'bar_url': resolved_bar,
        }

    return {
        'internal_get_music_embed': internal_get_music_embed,
        'internal_save_music_embed': internal_save_music_embed,
        'internal_reset_music_embed': internal_reset_music_embed,
        'internal_preview_music_embed': internal_preview_music_embed,
    }
