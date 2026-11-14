"""Scheduler internal HTTP endpoints."""

from collections.abc import Callable
from typing import Any, Optional
from fastapi import FastAPI, Body, HTTPException, Header, Query
from api.dependencies.scheduler import SchedulerDependencies
from api.dependencies.validation import validate_dependencies
from database.scheduler_models import serialize_scheduler_item, validate_scheduler_payload
from utils.web_validation import parse_integer as _parse_integer, parse_iso_datetime as _parse_iso_datetime
import discord


def register_routes(
    app: FastAPI,
    provide: Callable[[], SchedulerDependencies],
) -> dict[str, Callable[..., Any]]:
    validate_dependencies(provide(), SchedulerDependencies)

    @app.get('/internal/guild/scheduler')
    async def internal_get_scheduler_items(
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
        start: Optional[str] = None,
        end: Optional[str] = None,
    ):
        services = provide()
        can_manage = await services.check_user_guild_permission(user_id, guild_id)
        member = None if can_manage else services._get_guild_member(guild_id, int(user_id))
        if not can_manage and member is None:
            raise HTTPException(status_code=403, detail='You are not a member of this server.')
        parsed_start = _parse_iso_datetime(start, 'start') if start else None
        parsed_end = _parse_iso_datetime(end, 'end') if end else None
        items = await services.scheduler_model.list_guild_items(guild_id, start=parsed_start, end=parsed_end)
        if can_manage:
            return {'items': [serialize_scheduler_item(item) for item in items], 'can_manage': True}
        # Read-only members only see items posted to channels they can view, without manager-only fields.
        guild = services._find_guild(int(guild_id))
        visible = []
        for item in items:
            channel = guild.get_channel(int(item.get('channel_id') or 0)) if guild else None
            if channel is None or not channel.permissions_for(member).view_channel:
                continue
            safe = serialize_scheduler_item(item)
            for key in ('last_error', 'creator_id', 'content_author_id', 'delivery_attempt', 'delivery_marker'):
                safe.pop(key, None)
            visible.append(safe)
        return {'items': visible, 'can_manage': False}

    @app.post('/internal/guild/scheduler')
    async def internal_create_scheduler_item(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        guild_id = _parse_integer(data.get('guild_id'), 'guild_id', minimum=1)
        if not await services.check_user_guild_permission(user_id, guild_id):
            raise HTTPException(status_code=403, detail='Permission denied to manage schedules in this server.')
        guild = services._find_guild(guild_id)
        if guild is None:
            raise HTTPException(status_code=404, detail='Server not found.')
        channel_id = _parse_integer(data.get('channel_id'), 'channel_id', minimum=1)
        channel = guild.get_channel(channel_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            raise HTTPException(status_code=400, detail='channel_id must identify a text channel in this server.')
        try:
            payload = validate_scheduler_payload(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not services.bot_instance:
            raise HTTPException(status_code=503, detail='Bot is not ready.')
        cog = services.bot_instance.get_cog('Scheduler')
        if cog:
            try:
                item = await cog.schedule_item(guild, channel, int(user_id), payload)
            except (discord.Forbidden, discord.HTTPException) as exc:
                raise HTTPException(status_code=502, detail=f'Discord rejected the scheduled event: {exc}') from exc
        else:
            item = await services.scheduler_model.create_item(guild_id, channel_id, int(user_id), payload)
        return {'success': True, 'item': serialize_scheduler_item(item)}

    @app.patch('/internal/guild/scheduler/{item_id}')
    async def internal_update_scheduler_item(
        item_id: str,
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        guild_id = _parse_integer(data.get('guild_id'), 'guild_id', minimum=1)
        if not await services.check_user_guild_permission(user_id, guild_id):
            raise HTTPException(status_code=403, detail='Permission denied to manage schedules in this server.')
        guild = services._find_guild(guild_id)
        if guild is None:
            raise HTTPException(status_code=404, detail='Server not found.')
        try:
            changes = validate_scheduler_payload(data, partial=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if 'channel_id' in data:
            channel_id = _parse_integer(data.get('channel_id'), 'channel_id', minimum=1)
            channel = guild.get_channel(channel_id)
            if channel is None or not isinstance(channel, discord.TextChannel):
                raise HTTPException(status_code=400, detail='channel_id must identify a text channel in this server.')
            changes['channel_id'] = channel_id
        existing = await services.scheduler_model.get_item(item_id)
        if not existing or int(existing.get('guild_id') or 0) != guild_id:
            raise HTTPException(status_code=404, detail='Scheduled item not found.')
        try:
            updated = await services.scheduler_model.update_item(item_id, guild_id, changes, editor_id=int(user_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not updated:
            raise HTTPException(status_code=409, detail='Scheduled item is no longer editable.')
        if services.bot_instance and any(key in changes for key in ('kind', 'title', 'content', 'starts_at', 'event_end_at', 'location', 'channel_id')):
            cog = services.bot_instance.get_cog('Scheduler')
            if cog:
                await cog.sync_native_event(guild, updated)
        return {'success': True, 'item': serialize_scheduler_item(updated)}

    @app.delete('/internal/guild/scheduler/{item_id}')
    async def internal_delete_scheduler_item(
        item_id: str,
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
    ):
        services = provide()
        if not await services.check_user_guild_permission(user_id, guild_id):
            raise HTTPException(status_code=403, detail='Permission denied to manage schedules in this server.')
        existing = await services.scheduler_model.get_item(item_id)
        if not existing or int(existing.get('guild_id') or 0) != int(guild_id):
            raise HTTPException(status_code=404, detail='Scheduled item not found.')
        guild = services._find_guild(int(guild_id))
        if guild is None:
            raise HTTPException(status_code=404, detail='Server not found.')
        cancelled = None
        if services.bot_instance:
            cog = services.bot_instance.get_cog('Scheduler')
            if cog:
                cancelled = await cog.cancel_item(guild, existing)
        if cancelled is None:
            cancelled = await services.scheduler_model.cancel_item(item_id, int(guild_id))
        return {'success': cancelled is not None}

    return {
        'internal_get_scheduler_items': internal_get_scheduler_items,
        'internal_create_scheduler_item': internal_create_scheduler_item,
        'internal_update_scheduler_item': internal_update_scheduler_item,
        'internal_delete_scheduler_item': internal_delete_scheduler_item,
    }
