"""Administration internal HTTP endpoints."""

from collections.abc import Callable
from typing import Any
from config import Config
from fastapi import FastAPI, Body, HTTPException, Header, Query
from api.dependencies.administration import AdministrationDependencies
from api.dependencies.validation import validate_dependencies
from services.serialization import _serialize_api_value
from utils.moderation import normalize_casebook_config
from utils.modules import module_registry
from utils.permissions import CAPABILITIES
from utils.web_validation import parse_integer as _parse_integer
import logging

logger = logging.getLogger(__name__)


def register_routes(
    app: FastAPI,
    provide: Callable[[], AdministrationDependencies],
) -> dict[str, Callable[..., Any]]:
    validate_dependencies(provide(), AdministrationDependencies)

    @app.get('/internal/guild/modules')
    async def internal_get_guild_modules(
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
    ):
        services = provide()
        guild = services._find_guild(guild_id)
        if not guild:
            raise HTTPException(status_code=404, detail='Server not found.')
        if not await services.check_user_guild_capability(user_id, guild_id, 'view_configuration'):
            raise HTTPException(status_code=403, detail='You do not have permission to view modules for this server.')

        saved = {item['module_id']: item for item in await services.guild_module_model.get_guild_modules(guild_id)}
        modules = []
        for definition in module_registry.all():
            persisted = saved.get(definition.module_id, {})
            enabled = bool(persisted.get('enabled', definition.default_enabled))
            health = services._module_runtime_health(definition, guild, enabled)
            modules.append(
                {
                    **definition.as_dict(),
                    'enabled': enabled,
                    'config': persisted.get('config', definition.default_config),
                    'health_status': health['status'],
                    'health': health,
                    'updated_at': _serialize_api_value(persisted.get('updated_at')),
                }
            )
        return {'guild_id': str(guild_id), 'modules': modules}

    @app.post('/internal/guild/modules')
    async def internal_set_guild_module(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        guild_id = _parse_integer(data.get('guild_id'), 'guild_id', minimum=1)
        module_id = data.get('module_id')
        if not isinstance(module_id, str) or module_registry.get(module_id) is None:
            raise HTTPException(status_code=400, detail='Unknown module.')
        if not isinstance(data.get('enabled'), bool):
            raise HTTPException(status_code=400, detail='enabled must be a boolean.')
        if not await services.check_user_guild_permission(user_id, guild_id):
            raise HTTPException(status_code=403, detail='You do not have permission to edit modules for this server.')

        config = data.get('config')
        if config is not None and not isinstance(config, dict):
            raise HTTPException(status_code=400, detail='config must be an object.')
        if module_id == 'moderation_casebook' and config is not None:
            config = normalize_casebook_config(config)
        previous = await services.guild_module_model.get_module(guild_id, module_id)
        await services.guild_module_model.set_module(
            guild_id,
            module_id,
            data['enabled'],
            config,
            updated_by=user_id,
        )
        try:
            await module_registry.run_hook(module_id, guild_id, data['enabled'])
        except Exception as exc:
            await services.guild_module_model.set_module(
                guild_id,
                module_id,
                bool(previous.get('enabled')) if previous else module_registry.get(module_id).default_enabled,
                previous.get('config') if previous else module_registry.get(module_id).default_config,
                updated_by=user_id,
            )
            logger.exception('Module lifecycle hook failed for %s in guild %s', module_id, guild_id)
            raise HTTPException(
                status_code=503,
                detail='The module lifecycle hook failed; the switch was rolled back.',
            ) from exc
        return {'success': True, 'module_id': module_id, 'enabled': data['enabled']}

    @app.get('/internal/guild/permissions')
    async def internal_get_guild_permissions(
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
    ):
        services = provide()
        if not await services.check_user_guild_capability(user_id, guild_id, 'view_configuration'):
            raise HTTPException(status_code=403, detail='You do not have permission to view role grants.')
        guild = services._find_guild(guild_id)
        if guild is None:
            raise HTTPException(status_code=404, detail='Server not found.')
        grants = await services.guild_permission_model.get_role_grants(guild_id)
        roles = [
            {'id': str(role.id), 'name': role.name, 'managed': role.managed, 'position': role.position}
            for role in sorted(guild.roles, reverse=True)
        ]
        return {
            'guild_id': str(guild_id),
            'capabilities': sorted(CAPABILITIES),
            'roles': roles,
            'grants': grants,
            'can_edit': await services.check_user_guild_capability(user_id, guild_id, 'edit_configuration'),
        }

    @app.post('/internal/guild/permissions')
    async def internal_set_guild_permissions(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        guild_id = _parse_integer(data.get('guild_id'), 'guild_id', minimum=1)
        role_id = _parse_integer(data.get('role_id'), 'role_id', minimum=1)
        capabilities = data.get('capabilities')
        if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
            raise HTTPException(status_code=400, detail='capabilities must be an array of strings.')
        unknown = set(capabilities) - CAPABILITIES
        if unknown:
            raise HTTPException(status_code=400, detail=f'Unknown capabilities: {", ".join(sorted(unknown))}.')
        if not await services.check_user_guild_capability(user_id, guild_id, 'edit_configuration'):
            raise HTTPException(status_code=403, detail='You do not have permission to edit role grants.')
        guild = services._find_guild(guild_id)
        if guild is None:
            raise HTTPException(status_code=404, detail='Server not found.')
        role = guild.get_role(role_id)
        if role is None:
            raise HTTPException(status_code=400, detail='The selected role does not belong to this server.')
        normalized = sorted(set(capabilities))

        # Delegation rule: edit_configuration must not become "grant yourself everything". Unless the actor
        # is a bot owner, the server owner or an administrator, every capability being added or removed must
        # be one they already hold, the role must sit below their top role, and @everyone is off limits.
        actor = guild.get_member(int(user_id))
        if actor is None:
            try:
                actor = await guild.fetch_member(int(user_id))
            except Exception:
                actor = None
        actor_permissions = getattr(actor, 'guild_permissions', None)
        is_superuser = (
            int(user_id) in Config.OWNER_IDS
            or (actor is not None and getattr(guild, 'owner_id', None) == actor.id)
            or bool(getattr(actor_permissions, 'administrator', False))
        )
        if not is_superuser:
            if actor is None:
                raise HTTPException(status_code=403, detail='You do not have permission to edit role grants.')
            if role.is_default():
                raise HTTPException(
                    status_code=403, detail='Only administrators can grant dashboard capabilities to @everyone.'
                )
            if role >= actor.top_role:
                raise HTTPException(
                    status_code=403, detail='You can only change grants for roles below your highest role.'
                )
            current = await services.guild_permission_model.get_role_grants(guild_id)
            changed = set(normalized) ^ set(current.get(str(role_id), []))
            for capability in sorted(changed):
                if not await services.check_user_guild_capability(user_id, guild_id, capability):
                    raise HTTPException(
                        status_code=403,
                        detail=f'You can only grant or revoke capabilities you hold yourself ({capability}).',
                    )
        await services.guild_permission_model.set_role_grants(
            guild_id,
            role_id,
            normalized,
            updated_by=user_id,
        )
        return {'success': True, 'role_id': str(role_id), 'capabilities': normalized}

    return {
        'internal_get_guild_modules': internal_get_guild_modules,
        'internal_set_guild_module': internal_set_guild_module,
        'internal_get_guild_permissions': internal_get_guild_permissions,
        'internal_set_guild_permissions': internal_set_guild_permissions,
    }
