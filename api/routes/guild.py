"""Guild internal HTTP endpoints."""

from collections.abc import Callable
from typing import Any, Optional
from fastapi import FastAPI, Body, HTTPException, Header, Query
from api.dependencies.guild import GuildDependencies
from api.dependencies.validation import validate_dependencies
from config import Config
from utils.greetings import normalize_greetings
from utils.web_validation import parse_integer as _parse_integer
import discord
from utils.permissions import CAPABILITIES


def register_routes(
    app: FastAPI,
    provide: Callable[[], GuildDependencies],
) -> dict[str, Callable[..., Any]]:
    validate_dependencies(provide(), GuildDependencies)

    @app.get('/internal/auth/check-admin')
    async def internal_check_admin(
        user_id: int = Header(..., alias='X-User-Id'),
        query_user_id: Optional[int] = Query(None, alias='user_id'),
    ):
        """Check whether the calling user has access to any server dashboard capability.

        The identity is the authenticated X-User-Id; a query user_id is accepted only when it is the
        same user, so this cannot be used as an oracle about other accounts.
        """
        if query_user_id is not None and int(query_user_id) != int(user_id):
            raise HTTPException(status_code=403, detail='You can only check your own access.')
        services = provide()
        is_admin = int(user_id) in Config.OWNER_IDS
        if not is_admin:
            for guild in services.get_all_guilds():
                for cap in CAPABILITIES:
                    if await services.check_user_guild_capability(user_id, guild.id, cap):
                        is_admin = True
                        break
                if is_admin:
                    break
        return {'is_admin': is_admin}

    @app.get('/internal/guilds/manageable')
    async def internal_get_manageable_guilds(
        user_id: int = Header(..., alias='X-User-Id'),
    ):
        services = provide()
        is_admin = int(user_id) in Config.OWNER_IDS

        manageable = []
        for guild in services.get_all_guilds():
            capabilities = sorted(CAPABILITIES) if is_admin else [cap for cap in sorted(CAPABILITIES) if await services.check_user_guild_capability(user_id, guild.id, cap)]
            if capabilities:
                icon_url = ''
                if guild.icon:
                    icon_format = 'gif' if guild.icon.is_animated() else 'webp'
                    icon_url = str(guild.icon.with_format(icon_format).with_size(128))
                manageable.append(
                    {
                        'id': str(guild.id),
                        'name': guild.name,
                        'icon': icon_url,
                        'capabilities': capabilities,
                    }
                )
        return manageable

    @app.get('/internal/guild/greetings')
    async def internal_get_guild_greetings(
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
    ):
        services = provide()
        if not await services.check_user_guild_permission(user_id, guild_id):
            raise HTTPException(status_code=403, detail='You do not have permission to edit greetings for this server.')

        guild = services._find_guild(guild_id)
        if guild is None:
            raise HTTPException(status_code=404, detail='Server not found.')

        user = {'id': user_id, 'username': str(user_id)}

        greetings = await services.guild_model.get_greetings_config(guild_id)
        for config in greetings.values():
            config['channel_id'] = str(config['channel_id']) if config['channel_id'] else None

        return {
            'guild_id': str(guild_id),
            'guild_name': guild.name,
            'guild_icon_url': str(guild.icon.url) if guild.icon else '',
            'preview_context': services._guild_preview_context(guild, user),
            'can_edit': True,
            'text_channels': [{'id': str(channel.id), 'name': channel.name} for channel in guild.text_channels],
            'greetings': greetings,
        }

    @app.post('/internal/guild/greetings')
    async def internal_save_guild_greetings(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        guild_id = _parse_integer(data.get('guild_id'), 'guild_id', minimum=1)
        if not await services.check_user_guild_permission(user_id, guild_id):
            raise HTTPException(status_code=403, detail='You do not have permission to edit greetings for this server.')

        guild = services._find_guild(guild_id)
        if guild is None:
            raise HTTPException(status_code=404, detail='Server not found.')

        if not isinstance(data.get('greetings'), dict):
            raise HTTPException(status_code=400, detail='greetings must be an object.')

        greetings = normalize_greetings(data['greetings'])
        for kind, config in greetings.items():
            channel_id = config['channel_id']
            # Unbound greetings stay silent until a channel is selected.
            if channel_id is not None:
                channel = guild.get_channel(channel_id)
                if channel is None or not isinstance(channel, discord.TextChannel):
                    raise HTTPException(
                        status_code=400,
                        detail=f'{kind}.channel_id must identify a text channel in this server.',
                    )

        await services.guild_model.set_greetings_config(guild_id, greetings)
        response_greetings = {kind: {**config, 'channel_id': str(config['channel_id']) if config['channel_id'] else None} for kind, config in greetings.items()}
        return {'success': True, 'greetings': response_greetings}

    @app.post('/internal/guild/reaction-roles/generate')
    async def internal_generate_reaction_role_message(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        guild_id = int(data.get('guild_id'))
        channel_id = int(data.get('channel_id'))

        can_edit = await services.check_user_guild_permission(user_id, guild_id)
        if not can_edit:
            raise HTTPException(status_code=403, detail='You do not have permission to edit settings for this server.')

        guild = services._find_guild(guild_id)
        if not guild:
            raise HTTPException(status_code=404, detail='Guild not found')

        channel = guild.get_channel(channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail='Channel not found')

        title = data.get('title') or 'Reaction Roles'
        description = data.get('description') or 'React to this message to get your roles!'

        embed = discord.Embed(title=title, description=description, color=discord.Color.blue())
        try:
            msg = await channel.send(embed=embed)
            return {'message_id': str(msg.id)}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f'Failed to send message: {e}')

    return {
        'internal_check_admin': internal_check_admin,
        'internal_get_manageable_guilds': internal_get_manageable_guilds,
        'internal_get_guild_greetings': internal_get_guild_greetings,
        'internal_save_guild_greetings': internal_save_guild_greetings,
        'internal_generate_reaction_role_message': internal_generate_reaction_role_message,
    }
