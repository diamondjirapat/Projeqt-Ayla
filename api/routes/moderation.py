"""Moderation internal HTTP endpoints."""

from collections.abc import Callable
from typing import Any, Optional
from fastapi import FastAPI, Body, HTTPException, Header, Query, Request, Response
from api.dependencies.moderation import ModerationDependencies
from api.dependencies.validation import validate_dependencies
from config import Config
from database.models import ModerationAppealModel, ModerationCaseModel
from services.serialization import _serialize_api_value
from utils.moderation import normalize_casebook_config, select_crossed_escalation_rule
from utils.web_validation import (
    detect_evidence_extension as _detect_evidence_extension,
    parse_integer as _parse_integer,
    parse_iso_datetime as _parse_iso_datetime,
)
import csv
import discord
import io
import json
import logging
from services.moderation_actions import undo_case_action

logger = logging.getLogger(__name__)

# Per-guild caps on stored evidence blobs (shared MongoDB), on top of the per-upload size limit.
MAX_EVIDENCE_PER_GUILD = 2000
MAX_EVIDENCE_BYTES_PER_GUILD = 512 * 1024 * 1024
MAX_UNATTACHED_EVIDENCE_PER_GUILD = 50


def _evidence_url(guild_id: int, evidence_id: str) -> str:
    return f'{Config.WEB_URL.rstrip("/")}/api/guild/moderation/evidence/{evidence_id}?guild_id={guild_id}'


def _evidence_ids_from_urls(guild_id: int, urls: Any) -> list[str]:
    """Evidence IDs of this guild's own uploaded evidence referenced by a case's evidence links."""
    prefix = f'{Config.WEB_URL.rstrip("/")}/api/guild/moderation/evidence/'
    suffix = f'?guild_id={int(guild_id)}'
    ids = []
    for url in urls if isinstance(urls, list) else []:
        if isinstance(url, str) and url.startswith(prefix) and url.endswith(suffix):
            evidence_id = url[len(prefix) : -len(suffix)]
            if evidence_id.isalnum():
                ids.append(evidence_id)
    return ids


def register_routes(
    app: FastAPI,
    provide: Callable[[], ModerationDependencies],
) -> dict[str, Callable[..., Any]]:
    validate_dependencies(provide(), ModerationDependencies)

    async def reverse_sanction(services, guild_id, user_id, case, reason):
        guild = services._find_guild(guild_id)
        if guild is None:
            raise HTTPException(status_code=404, detail='Server not found.')
        actor = guild.get_member(user_id)
        if actor is None:
            try:
                actor = await guild.fetch_member(user_id)
            except discord.NotFound:
                actor = None
        try:
            await undo_case_action(guild, actor, case, reason)
        except (PermissionError, discord.Forbidden) as exc:
            raise HTTPException(status_code=403, detail='Permission denied to reverse this sanction.') from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except discord.HTTPException as exc:
            raise HTTPException(status_code=502, detail='Discord could not reverse this sanction. Please retry.') from exc

    @app.get('/internal/guild/moderation/cases')
    async def internal_get_moderation_cases(
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
        target_user_id: Optional[int] = None,
        moderator_user_id: Optional[int] = None,
        action: Optional[str] = None,
        status: Optional[str] = None,
        case_id: Optional[int] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=100),
    ):
        services = provide()
        if not await services.check_user_guild_capability(user_id, guild_id, 'manage_moderation'):
            raise HTTPException(status_code=403, detail='You do not have permission to view moderation cases.')
        if action and action not in ModerationCaseModel.ACTIONS:
            raise HTTPException(status_code=400, detail='Unknown moderation action.')
        if status and status not in ModerationCaseModel.STATUSES:
            raise HTTPException(status_code=400, detail='Unknown moderation case status.')
        result = await services.moderation_case_model.list_cases(
            guild_id,
            target_user_id=target_user_id,
            moderator_user_id=moderator_user_id,
            action=action,
            status=status,
            case_id=case_id,
            created_after=_parse_iso_datetime(created_after, 'created_after'),
            created_before=_parse_iso_datetime(created_before, 'created_before'),
            page=page,
            limit=limit,
        )
        return _serialize_api_value(result)

    @app.post('/internal/guild/moderation/cases')
    async def internal_create_moderation_case(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        guild_id = _parse_integer(data.get('guild_id'), 'guild_id', minimum=1)
        target_user_id = _parse_integer(data.get('target_user_id'), 'target_user_id', minimum=1)
        if not await services.check_user_guild_capability(user_id, guild_id, 'manage_moderation'):
            raise HTTPException(status_code=403, detail='You do not have permission to create moderation cases.')
        action = data.get('action')
        if action not in {'warn', 'note', 'timeout', 'kick', 'ban', 'unban'}:
            raise HTTPException(status_code=400, detail='Unsupported moderation action.')
        guild = services._find_guild(guild_id)
        if guild is None:
            raise HTTPException(status_code=404, detail='Server not found.')
        target = guild.get_member(target_user_id)
        if action == 'unban':
            try:
                target = (await guild.fetch_ban(discord.Object(id=target_user_id))).user
            except discord.NotFound as exc:
                raise HTTPException(status_code=404, detail='This user is not banned from the server.') from exc
            except (discord.Forbidden, discord.HTTPException) as exc:
                raise HTTPException(status_code=502, detail='Could not check the server ban list.') from exc
        elif target is None:
            raise HTTPException(status_code=404, detail='The selected member is not in this server.')
        actor = guild.get_member(user_id)
        if actor is None:
            try:
                actor = await guild.fetch_member(user_id)
            except Exception:
                pass
        if not services._actor_has_action_permission(actor, action):
            raise HTTPException(status_code=403, detail='You do not have permission to perform this action.')
        if action != 'unban':
            services._validate_web_moderation_target(guild, actor, target, actor_is_owner=int(user_id) in Config.OWNER_IDS)

        reason = str(data.get('reason') or 'No reason provided')[:1024]
        internal_note = str(data.get('internal_note') or '')[:4096]
        evidence = data.get('evidence', [])
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            raise HTTPException(status_code=400, detail='evidence must be an array of links.')
        evidence = [item.strip()[:2048] for item in evidence[:20] if item.strip().startswith(('http://', 'https://'))]
        points = _parse_integer(data.get('points', 1), 'points', minimum=1, maximum=100) if action == 'warn' else 0
        module = await services.guild_module_model.get_module(guild_id, 'moderation_casebook')
        casebook_config = normalize_casebook_config(module.get('config', {}) if module else {})
        duration_seconds = None
        if action == 'timeout':
            duration_seconds = _parse_integer(
                data.get('duration_seconds'),
                'duration_seconds',
                minimum=60,
                maximum=28 * 24 * 60 * 60,
            )
        try:
            if action == 'unban':
                await guild.unban(target, reason=reason)
                expires_at = None
            else:
                expires_at = await services._execute_web_moderation_action(action, target, reason, duration_seconds)
        except discord.Forbidden as exc:
            raise HTTPException(status_code=403, detail='Ayla does not have permission to perform this action.') from exc
        except discord.HTTPException as exc:
            raise HTTPException(status_code=502, detail='Discord rejected the moderation action.') from exc
        case = await services.moderation_case_model.create_case(
            guild_id,
            action,
            target.id,
            user_id,
            reason='' if action == 'note' else reason,
            internal_note=reason if action == 'note' and not internal_note else internal_note,
            duration_seconds=duration_seconds,
            expires_at=expires_at,
            target_snapshot=services._discord_user_snapshot(target),
            moderator_snapshot=(services._discord_user_snapshot(actor) if actor else {'user_id': str(user_id), 'username': str(user_id)}),
            evidence=evidence,
            points=points,
            source='web',
            retention_days=casebook_config['case_retention_days'],
        )
        await services.moderation_evidence_model.attach_to_case(guild_id, _evidence_ids_from_urls(guild_id, evidence), casebook_config['case_retention_days'])
        if bool(data.get('notify', True)) and action not in {'note', 'unban'}:
            await services._notify_web_case(target, case, reason)
        escalation_case = None
        escalation_error = None
        if action == 'warn':
            config = casebook_config
            summary = await services.moderation_case_model.warning_summary(
                guild_id,
                target.id,
                decay_days=config['warning_decay_days'],
            )
            rule = select_crossed_escalation_rule(config, max(0, summary['points'] - points), summary['points'])
            if rule and not services._actor_has_action_permission(actor, rule['action']):
                logger.warning(
                    'Skipping warning escalation action %s in guild %s: actor %s lacks the native permission',
                    rule['action'],
                    guild_id,
                    user_id,
                )
                escalation_error = 'The warning was saved, but you lack the permission for the configured escalation.'
                rule = None
            if rule:
                escalation_reason = f'Automatic escalation after reaching {summary["points"]} active warning points'
                escalation_duration = rule.get('duration_seconds') if rule['action'] == 'timeout' else None
                try:
                    escalation_expires = await services._execute_web_moderation_action(
                        rule['action'],
                        target,
                        escalation_reason,
                        escalation_duration,
                    )
                    escalation_case = await services.moderation_case_model.create_case(
                        guild_id,
                        rule['action'],
                        target.id,
                        user_id,
                        reason=escalation_reason,
                        internal_note=f'Escalated from warning case #{case["case_id"]}',
                        duration_seconds=escalation_duration,
                        expires_at=escalation_expires,
                        target_snapshot=services._discord_user_snapshot(target),
                        moderator_snapshot=services._discord_user_snapshot(actor)
                        if actor
                        else {
                            'user_id': str(user_id),
                            'username': str(user_id),
                        },
                        source='web',
                        retention_days=casebook_config['case_retention_days'],
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception('Could not apply web warning escalation in guild %s', guild_id)
                    escalation_error = 'The warning was saved, but the configured escalation failed.'
        return {
            'success': True,
            'case': _serialize_api_value(case),
            'escalation_case': _serialize_api_value(escalation_case),
            'escalation_error': escalation_error,
        }

    @app.get('/internal/guild/moderation/members')
    async def internal_search_moderation_members(
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
        query: str = '',
    ):
        services = provide()
        if not await services.check_user_guild_capability(user_id, guild_id, 'manage_moderation'):
            raise HTTPException(status_code=403, detail='You do not have permission to search server members.')
        guild = services._find_guild(guild_id)
        if guild is None:
            raise HTTPException(status_code=404, detail='Server not found.')
        needle = query.strip().casefold()
        members = []
        for member in guild.members:
            searchable = f'{member.id} {member.name} {member.display_name}'.casefold()
            if member.bot or (needle and needle not in searchable):
                continue
            members.append(
                {
                    'id': str(member.id),
                    'username': member.name,
                    'display_name': member.display_name,
                    'avatar_url': str(member.display_avatar.url),
                }
            )
            if len(members) >= 100:
                break
        return {'items': members}

    @app.post('/internal/guild/moderation/evidence/upload')
    async def internal_upload_moderation_evidence(
        request: Request,
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(..., ge=1),
    ):
        services = provide()
        if not await services.check_user_guild_capability(user_id, guild_id, 'manage_moderation'):
            raise HTTPException(status_code=403, detail='You do not have permission to upload moderation evidence.')
        if services._find_guild(guild_id) is None:
            raise HTTPException(status_code=404, detail='Server not found.')
        chunks = []
        total_size = 0
        async for chunk in request.stream():
            total_size += len(chunk)
            if total_size > services.EVIDENCE_UPLOAD_MAX_BYTES:
                raise HTTPException(status_code=413, detail='Evidence must be 8 MB or smaller.')
            chunks.append(chunk)
        evidence_bytes = b''.join(chunks)
        extension = _detect_evidence_extension(evidence_bytes)
        if extension is None:
            raise HTTPException(status_code=400, detail='Upload a PNG, JPEG, GIF, WebP, or PDF file.')
        usage = await services.moderation_evidence_model.guild_usage(guild_id)
        if usage['count'] >= MAX_EVIDENCE_PER_GUILD or usage['bytes'] + total_size > MAX_EVIDENCE_BYTES_PER_GUILD or usage['unattached'] >= MAX_UNATTACHED_EVIDENCE_PER_GUILD:
            raise HTTPException(status_code=413, detail='Evidence storage quota reached for this server.')
        module = await services.guild_module_model.get_module(guild_id, 'moderation_casebook')
        config = normalize_casebook_config(module.get('config', {}) if module else {})
        mime_type = {
            'png': 'image/png',
            'jpg': 'image/jpeg',
            'gif': 'image/gif',
            'webp': 'image/webp',
            'pdf': 'application/pdf',
        }[extension]
        evidence = await services.moderation_evidence_model.create_evidence(
            guild_id,
            user_id,
            evidence_bytes,
            mime_type,
            extension,
            config['case_retention_days'],
        )
        public_url = _evidence_url(guild_id, evidence['evidence_id'])
        return {'success': True, 'url': public_url, 'size': total_size, 'expires_at': evidence['expires_at']}

    @app.get('/internal/guild/moderation/evidence/{evidence_id}')
    async def internal_get_moderation_evidence(
        evidence_id: str,
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(..., ge=1),
    ):
        services = provide()
        evidence = await services.moderation_evidence_model.get_evidence(guild_id, evidence_id)
        if not evidence:
            raise HTTPException(status_code=404, detail='Moderation evidence not found or expired.')
        allowed = await services.check_user_guild_capability(user_id, guild_id, 'manage_moderation')
        if not allowed:
            expected_url = _evidence_url(guild_id, evidence_id)
            # Note cases are moderator-only, so their evidence never becomes visible to the member.
            owned_case = await services.moderation_case_model.collection.find_one(
                {
                    'guild_id': int(guild_id),
                    'target_user_id': int(user_id),
                    'action': {'$ne': 'note'},
                    'evidence': expected_url,
                }
            )
            allowed = owned_case is not None
        if not allowed:
            raise HTTPException(status_code=403, detail='You do not have permission to view this evidence.')
        return Response(
            content=evidence['content'],
            media_type=evidence['mime_type'],
            headers={
                'Content-Disposition': f'inline; filename="evidence-{evidence_id}.{evidence["extension"]}"',
                'Cache-Control': 'private, no-store',
                'X-Content-Type-Options': 'nosniff',
            },
        )

    @app.get('/internal/guild/moderation/my-cases')
    async def internal_get_my_moderation_cases(
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
    ):
        services = provide()
        if services._find_guild(guild_id) is None:
            raise HTTPException(status_code=404, detail='Server not found.')
        module = await services.guild_module_model.get_module(guild_id, 'moderation_casebook')
        config = normalize_casebook_config(module.get('config', {}) if module else {})
        if not await services.guild_module_model.is_enabled(guild_id, 'appeals', default=True) or not config['appeals_enabled']:
            raise HTTPException(status_code=404, detail='Appeals are disabled for this server.')
        result = await services.moderation_case_model.list_cases(
            guild_id,
            target_user_id=user_id,
            status='active',
            page=1,
            limit=100,
        )
        # Notes are private moderator records about the member, not sanctions they can see or appeal.
        result['items'] = [case for case in result['items'] if case.get('action') != 'note']
        result['total'] = len(result['items'])
        for case in result['items']:
            case.pop('internal_note', None)
            case.pop('revisions', None)
            case.pop('moderator_snapshot', None)
            case.pop('moderator_user_id', None)
        return _serialize_api_value(result)

    @app.patch('/internal/guild/moderation/cases/{case_id}')
    async def internal_update_moderation_case(
        case_id: int,
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        guild_id = _parse_integer(data.get('guild_id'), 'guild_id', minimum=1)
        if not await services.check_user_guild_capability(user_id, guild_id, 'manage_moderation'):
            raise HTTPException(status_code=403, detail='You do not have permission to edit moderation cases.')
        existing = await services.moderation_case_model.get_case(guild_id, case_id)
        if not existing:
            raise HTTPException(status_code=404, detail='Moderation case not found.')
        changes = {key: data[key] for key in ('reason', 'internal_note', 'evidence', 'status', 'points') if key in data}
        if 'status' in changes and changes['status'] not in ModerationCaseModel.STATUSES:
            raise HTTPException(status_code=400, detail='Unknown moderation case status.')
        if 'evidence' in changes and (not isinstance(changes['evidence'], list) or not all(isinstance(item, str) for item in changes['evidence'])):
            raise HTTPException(status_code=400, detail='evidence must be an array of links.')
        if 'points' in changes:
            changes['points'] = _parse_integer(changes['points'], 'points', minimum=0, maximum=100)
        if not changes:
            raise HTTPException(status_code=400, detail='No supported case changes were provided.')
        if changes.get('status') == 'revoked' and existing.get('status') == 'active':
            await reverse_sanction(services, guild_id, user_id, existing, str(changes.get('reason') or 'Case revoked'))
        updated = await services.moderation_case_model.update_case(guild_id, case_id, user_id, changes)
        if not updated:
            raise HTTPException(status_code=409, detail='Moderation case was not changed.')
        if 'evidence' in changes:
            module = await services.guild_module_model.get_module(guild_id, 'moderation_casebook')
            casebook_config = normalize_casebook_config(module.get('config', {}) if module else {})
            await services.moderation_evidence_model.attach_to_case(guild_id, _evidence_ids_from_urls(guild_id, changes['evidence']), casebook_config['case_retention_days'])
        return {
            'success': True,
            'case': _serialize_api_value(await services.moderation_case_model.get_case(guild_id, case_id)),
        }

    @app.post('/internal/guild/moderation/cases/{case_id}/cleanup')
    async def internal_cleanup_case_messages(
        case_id: int,
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        guild_id = _parse_integer(data.get('guild_id'), 'guild_id', minimum=1)
        amount = _parse_integer(data.get('amount'), 'amount', minimum=1, maximum=100)
        if not await services.check_user_guild_capability(user_id, guild_id, 'manage_moderation'):
            raise HTTPException(status_code=403, detail='Permission denied to clean up case messages.')
        guild = services._find_guild(guild_id)
        if guild is None:
            raise HTTPException(status_code=404, detail='Server not found.')
        actor = guild.get_member(user_id)
        channel = guild.get_channel(_parse_integer(data.get('channel_id'), 'channel_id', minimum=1))
        if not isinstance(channel, discord.TextChannel) or actor is None:
            raise HTTPException(status_code=400, detail='Select a text channel in this server.')
        perms = channel.permissions_for(actor)
        if not (perms.view_channel and perms.read_message_history and perms.manage_messages and actor.guild_permissions.moderate_members):
            raise HTTPException(status_code=403, detail='Message cleanup requires channel access and moderation permissions.')
        case = await services.moderation_case_model.get_case(guild_id, case_id)
        if not case:
            raise HTTPException(status_code=404, detail='Moderation case not found.')
        matched = 0

        def belongs_to_target(message):
            nonlocal matched
            if message.author.id == int(case['target_user_id']) and matched < amount:
                matched += 1
                return True
            return False

        try:
            deleted = await channel.purge(limit=min(2000, max(100, amount * 10)), check=belongs_to_target)
        except discord.Forbidden as exc:
            raise HTTPException(status_code=403, detail='Ayla cannot delete messages in this channel.') from exc
        except discord.HTTPException as exc:
            raise HTTPException(status_code=502, detail='Discord rejected the message cleanup.') from exc
        await services.moderation_case_model.record_revision(
            guild_id,
            case_id,
            user_id,
            'messages.cleanup',
            {'channel_id': channel.id, 'deleted_count': len(deleted)},
        )
        return {'success': True, 'deleted_count': len(deleted)}

    @app.get('/internal/guild/moderation/warnings/{target_user_id}')
    async def internal_get_warning_summary(
        target_user_id: int,
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
        decay_days: int = 90,
    ):
        services = provide()
        if not await services.check_user_guild_capability(user_id, guild_id, 'manage_moderation'):
            raise HTTPException(status_code=403, detail='You do not have permission to view warning points.')
        return _serialize_api_value(await services.moderation_case_model.warning_summary(guild_id, target_user_id, decay_days=max(1, min(3650, decay_days))))

    @app.get('/internal/guild/moderation/cases/export')
    async def internal_export_moderation_cases(
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
        format: str = 'json',
        case_id: Optional[int] = Query(None, ge=1),
        target_user_id: Optional[int] = None,
        moderator_user_id: Optional[int] = None,
        action: Optional[str] = None,
        status: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ):
        services = provide()
        can_manage = await services.check_user_guild_capability(user_id, guild_id, 'manage_moderation')
        can_export = await services.check_user_guild_capability(user_id, guild_id, 'export_data')
        if not can_manage or not can_export:
            raise HTTPException(status_code=403, detail='You do not have permission to export moderation cases.')
        if format not in {'json', 'csv'}:
            raise HTTPException(status_code=400, detail='format must be json or csv.')
        cases = await services.moderation_case_model.export_cases(
            guild_id,
            case_id=case_id,
            target_user_id=target_user_id,
            moderator_user_id=moderator_user_id,
            action=action,
            status=status,
            created_after=_parse_iso_datetime(created_after, 'created_after'),
            created_before=_parse_iso_datetime(created_before, 'created_before'),
        )
        serialized = _serialize_api_value(cases)
        filename = f'ayla-moderation-{guild_id}.{format}'
        if format == 'json':
            return Response(
                content=json.dumps(serialized, ensure_ascii=False, indent=2),
                media_type='application/json',
                headers={'Content-Disposition': f'attachment; filename="{filename}"'},
            )
        output = io.StringIO()
        fields = [
            'case_id',
            'action',
            'status',
            'target_user_id',
            'moderator_user_id',
            'points',
            'reason',
            'internal_note',
            'created_at',
            'expires_at',
        ]

        def _csv_safe(value):
            if isinstance(value, str) and value[:1] in ('=', '+', '-', '@'):
                return "'" + value
            return value

        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for case in serialized:
            writer.writerow({field: _csv_safe(case.get(field)) for field in fields})
        return Response(
            content=output.getvalue(),
            media_type='text/csv; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'},
        )

    @app.get('/internal/guild/moderation/appeals')
    async def internal_get_moderation_appeals(
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
        status: Optional[str] = None,
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=100),
    ):
        services = provide()
        if not await services.check_user_guild_capability(user_id, guild_id, 'review_appeals'):
            raise HTTPException(status_code=403, detail='You do not have permission to review appeals.')
        if not await services.guild_module_model.is_enabled(guild_id, 'appeals', default=True):
            raise HTTPException(status_code=404, detail='Appeals are disabled for this server.')
        if status and status not in ModerationAppealModel.STATUSES:
            raise HTTPException(status_code=400, detail='Unknown appeal status.')
        return _serialize_api_value(await services.moderation_appeal_model.list_appeals(guild_id, status=status, page=page, limit=limit))

    @app.post('/internal/guild/moderation/appeals')
    async def internal_submit_moderation_appeal(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        guild_id = _parse_integer(data.get('guild_id'), 'guild_id', minimum=1)
        case_id = _parse_integer(data.get('case_id'), 'case_id', minimum=1)
        statement = data.get('statement')
        if not isinstance(statement, str) or not statement.strip():
            raise HTTPException(status_code=400, detail='statement is required.')
        if services._find_guild(guild_id) is None:
            raise HTTPException(status_code=404, detail='Server not found.')
        module = await services.guild_module_model.get_module(guild_id, 'moderation_casebook')
        config = normalize_casebook_config(module.get('config', {}) if module else {})
        if not await services.guild_module_model.is_enabled(guild_id, 'appeals', default=True) or not config['appeals_enabled']:
            raise HTTPException(status_code=404, detail='Appeals are disabled for this server.')
        case = await services.moderation_case_model.get_case(guild_id, case_id)
        if not case or case['target_user_id'] != int(user_id) or case.get('status') != 'active' or case.get('action') == 'note':
            raise HTTPException(status_code=404, detail='An appealable moderation case was not found.')
        appeal = await services.moderation_appeal_model.create_appeal(
            guild_id,
            case_id,
            user_id,
            statement.strip(),
            retention_days=config['case_retention_days'],
        )
        return {'success': True, 'appeal': _serialize_api_value(appeal)}

    @app.patch('/internal/guild/moderation/appeals/{case_id}')
    async def internal_review_moderation_appeal(
        case_id: int,
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        guild_id = _parse_integer(data.get('guild_id'), 'guild_id', minimum=1)
        status = data.get('status')
        reason = data.get('reason', '')
        if status not in {'reviewing', 'accepted', 'declined'} or not isinstance(reason, str):
            raise HTTPException(status_code=400, detail='A valid review status and reason are required.')
        if not await services.check_user_guild_capability(user_id, guild_id, 'review_appeals'):
            raise HTTPException(status_code=403, detail='You do not have permission to review appeals.')
        if not await services.guild_module_model.is_enabled(guild_id, 'appeals', default=True):
            raise HTTPException(status_code=404, detail='Appeals are disabled for this server.')
        if status == 'accepted':
            # Look up the pending appeal directly so pagination cannot exclude it.
            appeal = await services.moderation_appeal_model.get_pending_appeal(guild_id, case_id)
            if not appeal:
                raise HTTPException(status_code=404, detail='A pending appeal was not found.')
            case = await services.moderation_case_model.get_case(guild_id, case_id)
            if case and case.get('status') == 'active':
                await reverse_sanction(services, guild_id, user_id, case, reason or 'Appeal accepted')
                saved = await services.moderation_case_model.update_case(
                    guild_id,
                    case_id,
                    user_id,
                    {'status': 'revoked'},
                    revision_action='appeal_accept',
                )
                if not saved:
                    raise HTTPException(status_code=409, detail='Could not revoke the case; please retry the review.')
        updated = await services.moderation_appeal_model.review_appeal(guild_id, case_id, user_id, status, reason)
        if not updated:
            raise HTTPException(status_code=404, detail='A pending appeal was not found.')
        return {'success': True, 'case_id': case_id, 'status': status}

    return {
        'internal_get_moderation_cases': internal_get_moderation_cases,
        'internal_create_moderation_case': internal_create_moderation_case,
        'internal_search_moderation_members': internal_search_moderation_members,
        'internal_upload_moderation_evidence': internal_upload_moderation_evidence,
        'internal_get_moderation_evidence': internal_get_moderation_evidence,
        'internal_get_my_moderation_cases': internal_get_my_moderation_cases,
        'internal_update_moderation_case': internal_update_moderation_case,
        'internal_cleanup_case_messages': internal_cleanup_case_messages,
        'internal_get_warning_summary': internal_get_warning_summary,
        'internal_export_moderation_cases': internal_export_moderation_cases,
        'internal_get_moderation_appeals': internal_get_moderation_appeals,
        'internal_submit_moderation_appeal': internal_submit_moderation_appeal,
        'internal_review_moderation_appeal': internal_review_moderation_appeal,
    }
