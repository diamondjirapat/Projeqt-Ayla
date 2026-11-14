"""Tickets internal HTTP endpoints."""

from collections.abc import Callable
from typing import Any, Optional
from fastapi import FastAPI, Body, HTTPException, Header, Query, Response
from api.dependencies.tickets import TicketsDependencies
from api.dependencies.validation import validate_dependencies
from datetime import UTC, datetime, timedelta
from services.serialization import _serialize_api_value, _serialize_ticket
from utils.permissions import has_capability
from utils.tickets import TICKET_PRIORITIES, render_transcript, safe_channel_name
from utils.web_validation import parse_integer as _parse_integer
import asyncio
import discord


def register_routes(
    app: FastAPI,
    provide: Callable[[], TicketsDependencies],
) -> dict[str, Callable[..., Any]]:
    validate_dependencies(provide(), TicketsDependencies)

    @app.get('/internal/guild/tickets')
    async def internal_get_tickets(
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
        status: Optional[str] = None,
        category: Optional[str] = None,
        assignee: Optional[int] = None,
        priority: Optional[str] = None,
        member: Optional[int] = None,
        age_hours: Optional[int] = Query(None, ge=1, le=8760),
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=100),
    ):
        services = provide()
        user, guild = await services._require_ticket_access(user_id, guild_id)
        if status and status not in {'open', 'closed', 'deleted'}:
            raise HTTPException(status_code=400, detail='Unknown ticket status.')
        if priority and priority not in TICKET_PRIORITIES:
            raise HTTPException(status_code=400, detail='Unknown ticket priority.')
        reveal_identity = await services.check_user_guild_capability(user_id, guild_id, 'view_private_logs')
        if member is not None and not reveal_identity:
            raise HTTPException(status_code=403, detail='Private-log access is required to filter by member.')
        older_than = datetime.now(UTC) - timedelta(hours=age_hours) if age_hours else None
        result = await services.ticket_model.list_tickets(
            guild_id,
            status=status,
            category=category,
            assignee=assignee,
            priority=priority,
            member=member,
            older_than=older_than,
            page=page,
            limit=limit,
        )
        result['items'] = [_serialize_ticket(item, reveal_identity) for item in result['items']]
        panels, statistics = await asyncio.gather(
            services.ticket_panel_model.list_panels(guild_id),
            services.ticket_model.statistics(guild_id),
        )
        result['panels'] = _serialize_api_value(panels)
        result['statistics'] = _serialize_api_value(statistics)
        result['roles'] = [{'id': str(role.id), 'name': role.name} for role in guild.roles if not role.is_default()]
        result['channels'] = [{'id': str(channel.id), 'name': channel.name} for channel in guild.text_channels]
        result['categories'] = [{'id': str(channel.id), 'name': channel.name} for channel in guild.categories]
        result['can_view_restricted_identity'] = reveal_identity
        return result

    @app.get('/internal/guild/tickets/{ticket_id}')
    async def internal_get_ticket(
        ticket_id: int,
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
    ):
        services = provide()
        user, _ = await services._require_ticket_access(user_id, guild_id)
        ticket = await services.ticket_model.get_ticket(guild_id, ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail='Ticket not found.')
        reveal_identity = await services.check_user_guild_capability(user_id, guild_id, 'view_private_logs')
        events = _serialize_api_value(await services.ticket_model.list_events(guild_id, ticket_id))
        if ticket.get('anonymous') and not reveal_identity:
            for event in events:
                if event.get('actor_user_id') == ticket.get('member_user_id'):
                    event['actor_user_id'] = None
        return {
            'ticket': _serialize_ticket(ticket, reveal_identity),
            'events': events,
        }

    @app.post('/internal/guild/tickets/panels')
    async def internal_create_ticket_panel(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        guild_id = _parse_integer(data.get('guild_id'), 'guild_id', minimum=1)
        user, guild = await services._require_ticket_access(user_id, guild_id)
        incoming = data.get('panel', {})
        if not isinstance(incoming, dict):
            raise HTTPException(status_code=400, detail='panel must be an object.')
        panel = await services.ticket_panel_model.create_panel(
            guild_id,
            user['id'],
            services._validate_ticket_panel_for_guild(guild, incoming),
        )
        return {'success': True, 'panel': _serialize_api_value(panel)}

    @app.patch('/internal/guild/tickets/panels/{panel_id}')
    async def internal_update_ticket_panel(
        panel_id: str,
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        guild_id = _parse_integer(data.get('guild_id'), 'guild_id', minimum=1)
        user, guild = await services._require_ticket_access(user_id, guild_id)
        existing = await services.ticket_panel_model.get_panel(guild_id, panel_id)
        if not existing or existing.get('deleted'):
            raise HTTPException(status_code=404, detail='Ticket panel not found.')
        incoming = data.get('panel', {})
        if not isinstance(incoming, dict):
            raise HTTPException(status_code=400, detail='panel must be an object.')
        panel = services._validate_ticket_panel_for_guild(guild, {**existing, **incoming})
        await services.ticket_panel_model.update_panel(guild_id, panel_id, panel)
        return {
            'success': True,
            'panel': _serialize_api_value(await services.ticket_panel_model.get_panel(guild_id, panel_id)),
        }

    @app.delete('/internal/guild/tickets/panels/{panel_id}')
    async def internal_delete_ticket_panel(
        panel_id: str,
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
    ):
        services = provide()
        user, _ = await services._require_ticket_access(user_id, guild_id)
        existing = await services.ticket_panel_model.get_panel(guild_id, panel_id)
        if not existing or existing.get('deleted'):
            raise HTTPException(status_code=404, detail='Ticket panel not found.')
        await services.ticket_panel_model.delete_panel(guild_id, panel_id)
        return {'success': True}

    @app.post('/internal/guild/tickets/panels/{panel_id}/publish')
    async def internal_publish_ticket_panel(
        panel_id: str,
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        guild_id = _parse_integer(data.get('guild_id'), 'guild_id', minimum=1)
        channel_id = _parse_integer(data.get('channel_id'), 'channel_id', minimum=1)
        user, guild = await services._require_ticket_access(user_id, guild_id)
        panel = await services.ticket_panel_model.get_panel(guild_id, panel_id)
        channel = guild.get_channel(channel_id)
        if not panel or panel.get('deleted'):
            raise HTTPException(status_code=404, detail='Ticket panel not found.')
        if not isinstance(channel, discord.TextChannel):
            raise HTTPException(status_code=400, detail='The publishing channel does not belong to this server.')
        embed = discord.Embed(title=panel['title'], description=panel['description'], color=discord.Color.blurple())
        try:
            message = await channel.send(embed=embed, view=services.create_ticket_panel_view(panel))
        except discord.Forbidden as exc:
            raise HTTPException(status_code=403, detail='Ayla cannot publish in that channel.') from exc
        await services.ticket_panel_model.bind_message(guild_id, panel_id, channel.id, message.id)
        return {'success': True, 'channel_id': str(channel.id), 'message_id': str(message.id)}

    @app.patch('/internal/guild/tickets/{ticket_id}')
    async def internal_update_ticket(
        ticket_id: int,
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        guild_id = _parse_integer(data.get('guild_id'), 'guild_id', minimum=1)
        user, guild = await services._require_ticket_access(user_id, guild_id)
        ticket = await services.ticket_model.get_ticket(guild_id, ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail='Ticket not found.')
        action = str(data.get('action') or 'update')
        channel = guild.get_channel(ticket['channel_id'])
        cog = services._cog_for_guild(guild_id, 'Tickets')
        if action == 'close':
            if not cog:
                raise HTTPException(status_code=503, detail='Ticket worker is unavailable.')
            updated = await cog.close_ticket(guild, ticket, user['id'])
            if not updated:
                raise HTTPException(status_code=409, detail='Ticket is not open.')
        elif action == 'reopen':
            updated = await services.ticket_model.reopen_ticket(guild_id, ticket_id, user['id'])
            if not updated:
                raise HTTPException(status_code=409, detail='Ticket is not closed.')
            member = guild.get_member(ticket['member_user_id'])
            if isinstance(channel, discord.TextChannel) and member and not ticket.get('anonymous'):
                await channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
            await services.ticket_model.record_event(guild_id, ticket_id, 'reopened', user['id'])
        elif action == 'delete':
            if ticket.get('status') != 'closed':
                raise HTTPException(status_code=409, detail='Close the ticket before deleting it.')
            await services.ticket_model.update_ticket(guild_id, ticket_id, {'status': 'deleted'})
            await services.ticket_model.record_event(guild_id, ticket_id, 'deleted', user['id'])
            if isinstance(channel, discord.TextChannel):
                await channel.delete(reason=f'Ticket #{ticket_id} deleted from Ayla dashboard')
        elif action == 'note':
            if not str(data.get('note') or '').strip():
                raise HTTPException(status_code=400, detail='A private note is required.')
            await services.ticket_model.add_note(guild_id, ticket_id, user['id'], data['note'])
        else:
            values = {}
            if 'priority' in data:
                if data['priority'] not in TICKET_PRIORITIES:
                    raise HTTPException(status_code=400, detail='Unknown ticket priority.')
                values['priority'] = data['priority']
            if 'category_name' in data:
                values['category_name'] = str(data['category_name']).strip()[:80]
            if 'assigned_staff_id' in data:
                assignee = _parse_integer(data['assigned_staff_id'], 'assigned_staff_id', minimum=1)
                member = guild.get_member(assignee)
                if member is None:
                    raise HTTPException(status_code=400, detail='The assignee is not a server member.')
                panel = await services.ticket_panel_model.get_panel(guild_id, ticket['panel_id'])
                staff_roles = {int(role_id) for role_id in (panel or {}).get('staff_role_ids', [])}
                grants = await services.guild_permission_model.get_role_grants(guild_id)
                if not has_capability(member, 'manage_tickets', grants) and not staff_roles.intersection(
                    role.id for role in member.roles
                ):
                    raise HTTPException(status_code=400, detail='The assignee is not ticket staff.')
                values['assigned_staff_id'] = assignee
            if 'name' in data and isinstance(channel, discord.TextChannel):
                await channel.edit(name=safe_channel_name(ticket_id, str(data['name'])))
            if not values and 'name' not in data:
                raise HTTPException(status_code=400, detail='No supported ticket update was provided.')
            if values:
                await services.ticket_model.update_ticket(guild_id, ticket_id, values)
            await services.ticket_model.record_event(guild_id, ticket_id, 'updated', user['id'], values)
        current = await services.ticket_model.get_ticket(guild_id, ticket_id)
        reveal_identity = await services.check_user_guild_capability(user['id'], guild_id, 'view_private_logs')
        return {'success': True, 'ticket': _serialize_ticket(current, reveal_identity)}

    @app.get('/internal/guild/tickets/{ticket_id}/transcript')
    async def internal_get_ticket_transcript(
        ticket_id: int,
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
        format: str = Query('html', pattern='^(html|markdown|json)$'),
    ):
        services = provide()
        user, _ = await services._require_ticket_access(user_id, guild_id)
        transcript = await services.db_manager.get_collection('ticket_transcripts').find_one(
            {
                'guild_id': int(guild_id),
                'ticket_id': int(ticket_id),
            }
        )
        if not transcript:
            raise HTTPException(status_code=404, detail='Transcript not found.')
        ticket = await services.ticket_model.get_ticket(guild_id, ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail='Ticket not found.')
        reveal_identity = await services.check_user_guild_capability(user_id, guild_id, 'view_private_logs')
        ticket_payload = _serialize_ticket(ticket, reveal_identity)
        messages = [
            _serialize_api_value(message)
            async for message in services.db_manager.get_collection('ticket_transcript_messages')
            .find(
                {
                    'guild_id': int(guild_id),
                    'ticket_id': int(ticket_id),
                }
            )
            .sort('sequence', 1)
        ]
        media_types = {'html': 'text/html', 'markdown': 'text/markdown', 'json': 'application/json'}
        return Response(
            content=render_transcript(ticket_payload, messages, format),
            media_type=media_types[format],
            headers=services.NO_CACHE_HEADERS,
        )

    return {
        'internal_get_tickets': internal_get_tickets,
        'internal_get_ticket': internal_get_ticket,
        'internal_create_ticket_panel': internal_create_ticket_panel,
        'internal_update_ticket_panel': internal_update_ticket_panel,
        'internal_delete_ticket_panel': internal_delete_ticket_panel,
        'internal_publish_ticket_panel': internal_publish_ticket_panel,
        'internal_update_ticket': internal_update_ticket,
        'internal_get_ticket_transcript': internal_get_ticket_transcript,
    }
