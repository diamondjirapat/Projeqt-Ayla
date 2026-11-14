"""Custom Commands internal HTTP endpoints."""

from collections.abc import Callable
from typing import Any
from fastapi import FastAPI, Body, HTTPException, Header, Query
from api.dependencies.custom_commands import CustomCommandsDependencies
from api.dependencies.validation import validate_dependencies
from datetime import UTC, datetime
from services.serialization import _serialize_custom_command
from utils.web_validation import parse_integer as _parse_integer
import re


def register_routes(
    app: FastAPI,
    provide: Callable[[], CustomCommandsDependencies],
) -> dict[str, Callable[..., Any]]:
    validate_dependencies(provide(), CustomCommandsDependencies)

    @app.get('/internal/guild/custom-commands')
    async def internal_get_custom_commands(
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
    ):
        services = provide()
        can_edit = await services.check_user_guild_permission(user_id, guild_id)
        if not can_edit:
            raise HTTPException(
                status_code=403,
                detail='You do not have permission to view custom commands for this server.',
            )

        commands_list = await services.custom_command_model.get_guild_commands(guild_id)
        return {'commands': [_serialize_custom_command(cmd) for cmd in commands_list]}

    @app.post('/internal/guild/custom-commands/preview')
    async def internal_preview_custom_command(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        guild_id = _parse_integer(data.get('guild_id'), 'guild_id', minimum=1)
        if not await services.check_user_guild_permission(user_id, guild_id):
            raise HTTPException(
                status_code=403, detail='You do not have permission to preview commands for this server.'
            )
        guild = services._find_guild(guild_id)
        if guild is None:
            raise HTTPException(status_code=404, detail='Server not found.')

        user = {'id': user_id, 'username': str(user_id)}
        context = services._guild_preview_context(guild, user)
        author_name = str(context['display_name'])
        user_id_str = str(context['user_id'])
        args_text = str(data.get('args_text', 'sample argument'))

        def parse_preview_str(template: str) -> str:
            if not template:
                return ''
            args_list = args_text.split() if args_text else []
            placeholders = {
                '{user}': author_name,
                '{author}': author_name,
                '{username}': author_name,
                '{author_mention}': f'@{author_name}',
                '{user_mention}': f'@{author_name}',
                '{mention}': f'@{author_name}',
                '{author_id}': user_id_str,
                '{user_id}': user_id_str,
                '{server}': str(context['guild_name']),
                '{guild}': str(context['guild_name']),
                '{server_id}': str(context['guild_id']),
                '{guild_id}': str(context['guild_id']),
                '{channel}': str(context['channel_name']),
                '{channel_mention}': str(context['channel_mention']),
                '{channel_id}': str(context['channel_id']),
                '{member_count}': str(context['member_count']),
                '{avatar}': str(context['avatar_url']),
                '{server_icon}': str(context['guild_icon_url']),
                '{server_banner}': str(context['guild_banner_url']),
                '{date}': datetime.now(UTC).strftime('%Y-%m-%d'),
                '{time}': datetime.now(UTC).strftime('%H:%M:%S UTC'),
                '{args}': args_text,
            }
            for i in range(1, 10):
                placeholders[f'{{arg{i}}}'] = args_list[i - 1] if i - 1 < len(args_list) else ''
            res = template
            for k, v in placeholders.items():
                res = res.replace(k, v)
            return res

        raw_response = data.get('response', '')
        parsed_text = parse_preview_str(raw_response) if raw_response else ''

        is_embed = bool(data.get('is_embed', False))
        raw_embed = data.get('embed_config') or {}

        parsed_embed = None
        if is_embed:
            parsed_embed = {
                'title': parse_preview_str(raw_embed.get('title', '')),
                'description': parse_preview_str(raw_embed.get('description', '')),
                'color': raw_embed.get('color', '#5865F2'),
                'image_url': parse_preview_str(raw_embed.get('image_url', '')),
                'thumbnail_url': parse_preview_str(raw_embed.get('thumbnail_url', '')),
                'footer_text': parse_preview_str(raw_embed.get('footer_text', '')),
                'footer_icon': parse_preview_str(raw_embed.get('footer_icon', '')),
            }

        return {
            'parsed_text': parsed_text,
            'parsed_embed': parsed_embed,
            'preview_context': context,
        }

    @app.post('/internal/guild/custom-commands')
    async def internal_create_custom_command(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        raw_guild_id = data.get('guild_id')
        if raw_guild_id is None:
            raise HTTPException(status_code=400, detail='Missing guild_id.')
        try:
            guild_id = int(raw_guild_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail='Invalid guild_id.')

        can_edit = await services.check_user_guild_permission(user_id, guild_id)
        if not can_edit:
            raise HTTPException(
                status_code=403,
                detail='You do not have permission to create custom commands for this server.',
            )

        name = str(data.get('name', '')).strip().lower()
        if not name or not re.match(r'^[a-z0-9_-]+$', name):
            raise HTTPException(
                status_code=400,
                detail='Invalid command trigger name. Only alphanumeric characters, dashes, and underscores are allowed.',
            )

        if services.bot_instance and services.bot_instance.get_command(name):
            raise HTTPException(status_code=400, detail=f"Command '{name}' conflicts with a built-in bot command.")

        existing = await services.custom_command_model.get_command(guild_id, name)
        if existing:
            raise HTTPException(status_code=400, detail=f"Custom command '{name}' already exists.")

        max_commands = await services.guild_model.get_max_custom_commands(guild_id)
        guild_cmds = await services.custom_command_model.get_guild_commands(guild_id)
        if len(guild_cmds) >= max_commands:
            raise HTTPException(
                status_code=400,
                detail=f'Maximum custom commands limit ({max_commands}) reached for this server.',
            )

        created = await services.custom_command_model.create_command(
            guild_id=guild_id,
            name=name,
            response=str(data.get('response', '')),
            description=str(data.get('description', '')),
            is_embed=bool(data.get('is_embed', False)),
            embed_config=data.get('embed_config') or {},
            created_by=int(user_id),
            created_by_name=str(user_id),
            enabled=bool(data.get('enabled', True)),
        )
        if not created:
            raise HTTPException(status_code=500, detail='Failed to create custom command.')

        return {'success': True, 'command': _serialize_custom_command(created)}

    @app.post('/internal/guild/custom-commands/update')
    async def internal_update_custom_command(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        raw_guild_id = data.get('guild_id')
        if raw_guild_id is None:
            raise HTTPException(status_code=400, detail='Missing guild_id.')
        try:
            guild_id = int(raw_guild_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail='Invalid guild_id.')

        can_edit = await services.check_user_guild_permission(user_id, guild_id)
        if not can_edit:
            raise HTTPException(
                status_code=403,
                detail='You do not have permission to update custom commands for this server.',
            )

        name = str(data.get('name', '')).strip().lower()
        if not name:
            raise HTTPException(status_code=400, detail='Command name is required.')

        existing = await services.custom_command_model.get_command(guild_id, name)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Custom command '{name}' not found.")

        update_data = {
            'response': str(data.get('response', '')),
            'description': str(data.get('description', '')),
            'is_embed': bool(data.get('is_embed', False)),
            'embed_config': data.get('embed_config') or {},
            'enabled': bool(data.get('enabled', True)),
        }

        await services.custom_command_model.update_command(guild_id, name, update_data)
        updated = await services.custom_command_model.get_command(guild_id, name)
        return {'success': True, 'command': _serialize_custom_command(updated)}

    @app.post('/internal/guild/custom-commands/delete')
    async def internal_delete_custom_command(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        raw_guild_id = data.get('guild_id')
        if raw_guild_id is None:
            raise HTTPException(status_code=400, detail='Missing guild_id.')
        try:
            guild_id = int(raw_guild_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail='Invalid guild_id.')

        can_edit = await services.check_user_guild_permission(user_id, guild_id)
        if not can_edit:
            raise HTTPException(
                status_code=403,
                detail='You do not have permission to delete custom commands for this server.',
            )

        name = str(data.get('name', '')).strip().lower()
        if not name:
            raise HTTPException(status_code=400, detail='Command name is required.')

        existing = await services.custom_command_model.get_command(guild_id, name)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Custom command '{name}' not found.")

        success = await services.custom_command_model.delete_command(guild_id, name)
        return {'success': success}

    @app.post('/internal/guild/custom-commands/toggle')
    async def internal_toggle_custom_command(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        raw_guild_id = data.get('guild_id')
        if raw_guild_id is None:
            raise HTTPException(status_code=400, detail='Missing guild_id.')
        try:
            guild_id = int(raw_guild_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail='Invalid guild_id.')

        can_edit = await services.check_user_guild_permission(user_id, guild_id)
        if not can_edit:
            raise HTTPException(
                status_code=403,
                detail='You do not have permission to toggle custom commands for this server.',
            )

        name = str(data.get('name', '')).strip().lower()
        if not name:
            raise HTTPException(status_code=400, detail='Command name is required.')

        existing = await services.custom_command_model.get_command(guild_id, name)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Custom command '{name}' not found.")

        new_status = bool(data.get('enabled', not existing.get('enabled', True)))
        success = await services.custom_command_model.update_command(guild_id, name, {'enabled': new_status})
        return {'success': success, 'enabled': new_status}

    return {
        'internal_get_custom_commands': internal_get_custom_commands,
        'internal_preview_custom_command': internal_preview_custom_command,
        'internal_create_custom_command': internal_create_custom_command,
        'internal_update_custom_command': internal_update_custom_command,
        'internal_delete_custom_command': internal_delete_custom_command,
        'internal_toggle_custom_command': internal_toggle_custom_command,
    }
