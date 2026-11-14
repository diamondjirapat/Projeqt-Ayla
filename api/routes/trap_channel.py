"""Trap channel internal HTTP endpoints."""

from collections.abc import Callable
from typing import Any

import discord
from fastapi import Body, FastAPI, Header, HTTPException, Query

from api.dependencies.trap_channel import TrapChannelDependencies
from api.dependencies.validation import validate_dependencies
from config import Config
from services.trap_channel import missing_permissions, publish_warning
from utils.trap_channel import (
    MAX_TIMEOUT_MINUTES,
    TRAP_ACTIONS,
    TRAP_PLACEHOLDERS,
    normalize_trap_channel_config,
    serialize_trap_channel_config,
)
from utils.web_validation import parse_integer as _parse_integer

# The Discord permission a dashboard editor must hold to arm each penalty.
_ACTION_USER_PERMISSIONS = {'timeout': 'moderate_members', 'kick': 'kick_members', 'ban': 'ban_members'}


def register_routes(
    app: FastAPI,
    provide: Callable[[], TrapChannelDependencies],
) -> dict[str, Callable[..., Any]]:
    validate_dependencies(provide(), TrapChannelDependencies)

    def channel_warnings(guild: discord.Guild, config: dict[str, Any]) -> list[str]:
        channel = guild.get_channel(config['channel_id']) if config['channel_id'] else None
        return missing_permissions(guild, channel, config['action']) if channel else []

    @app.get('/internal/guild/trap-channel')
    async def internal_get_trap_channel(
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
    ):
        services = provide()
        if not await services.check_user_guild_permission(user_id, guild_id):
            raise HTTPException(status_code=403, detail='You do not have permission to edit this server.')
        guild = services._find_guild(guild_id)
        if guild is None:
            raise HTTPException(status_code=404, detail='Server not found.')

        config = await services.guild_model.get_trap_channel_config(guild_id)
        bot_top_role = guild.me.top_role if guild.me else None
        return {
            'guild_id': str(guild_id),
            'guild_name': guild.name,
            'guild_icon_url': str(guild.icon.url) if guild.icon else '',
            'preview_context': services._guild_preview_context(guild, {'id': user_id, 'username': str(user_id)}),
            'can_edit': True,
            'text_channels': [{'id': str(channel.id), 'name': channel.name} for channel in guild.text_channels],
            'roles': [{'id': str(role.id), 'name': role.name, 'color': str(role.color)} for role in reversed(guild.roles) if not role.is_default()],
            'bot_top_role_position': bot_top_role.position if bot_top_role else 0,
            'actions': list(TRAP_ACTIONS),
            'placeholders': list(TRAP_PLACEHOLDERS),
            'max_timeout_minutes': MAX_TIMEOUT_MINUTES,
            'missing_permissions': channel_warnings(guild, config),
            'trap_channel': serialize_trap_channel_config(config),
        }

    @app.post('/internal/guild/trap-channel')
    async def internal_save_trap_channel(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        guild_id = _parse_integer(data.get('guild_id'), 'guild_id', minimum=1)
        if not await services.check_user_guild_permission(user_id, guild_id):
            raise HTTPException(status_code=403, detail='You do not have permission to edit this server.')
        guild = services._find_guild(guild_id)
        if guild is None:
            raise HTTPException(status_code=404, detail='Server not found.')
        if not isinstance(data.get('trap_channel'), dict):
            raise HTTPException(status_code=400, detail='trap_channel must be an object.')

        previous = await services.guild_model.get_trap_channel_config(guild_id)
        config = normalize_trap_channel_config(data['trap_channel'])
        # The warning message id is server-owned: a client-supplied id could point the bot at
        # any of its other messages and overwrite them.
        same_channel = config['channel_id'] == previous['channel_id']
        config['message_id'] = previous['message_id'] if same_channel else None

        if config['channel_id'] is not None:
            channel = guild.get_channel(config['channel_id'])
            if not isinstance(channel, discord.TextChannel):
                raise HTTPException(status_code=400, detail='channel_id must identify a text channel in this server.')
        elif config['enabled']:
            raise HTTPException(status_code=400, detail='Choose a trap channel before enabling it.')
        config['exempt_role_ids'] = [role_id for role_id in config['exempt_role_ids'] if guild.get_role(role_id)]

        # Arming a harsher penalty needs the matching Discord permission; unchanged settings stay
        # editable by any configurer, like other dashboard settings.
        required = _ACTION_USER_PERMISSIONS.get(config['action'])
        newly_armed = config['enabled'] and (not previous['enabled'] or config['action'] != previous['action'])
        if required and newly_armed and int(user_id) not in Config.OWNER_IDS and int(user_id) != guild.owner_id:
            member = guild.get_member(int(user_id))
            if member is None:
                try:
                    member = await guild.fetch_member(int(user_id))
                except discord.HTTPException:
                    member = None
            if member is None or not getattr(member.guild_permissions, required, False):
                raise HTTPException(
                    status_code=403,
                    detail=f'You need the {required.replace("_", " ").title()} permission to use this penalty.',
                )

        config = await services.guild_model.set_trap_channel_config(guild_id, config)
        publish_error = None
        if config['enabled'] and config['channel_id']:
            try:
                message_id = await publish_warning(
                    guild,
                    config,
                    previous_channel_id=previous['channel_id'],
                    previous_message_id=previous['message_id'],
                )
            except discord.HTTPException:
                message_id = config['message_id']
                publish_error = 'Settings saved, but the warning could not be posted. Check the bot permissions.'
            if message_id != config['message_id']:
                config['message_id'] = message_id
                config = await services.guild_model.set_trap_channel_config(guild_id, config)

        return {
            'success': True,
            'trap_channel': serialize_trap_channel_config(config),
            'missing_permissions': channel_warnings(guild, config),
            'publish_error': publish_error,
        }

    return {
        'internal_get_trap_channel': internal_get_trap_channel,
        'internal_save_trap_channel': internal_save_trap_channel,
    }
