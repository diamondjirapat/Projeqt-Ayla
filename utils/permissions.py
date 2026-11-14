"""Capability-based permission helpers shared by Discord and web features."""

from collections.abc import Mapping
from typing import Any


CAPABILITIES = frozenset(
    {
        'view_configuration',
        'edit_configuration',
        'view_private_logs',
        'manage_moderation',
        'review_appeals',
        'manage_tickets',
        'export_data',
    }
)

_NATIVE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    'view_configuration': ('manage_guild',),
    'edit_configuration': ('manage_guild',),
    'view_private_logs': ('view_audit_log',),
    'manage_moderation': ('moderate_members', 'kick_members', 'ban_members'),
    'review_appeals': ('manage_guild',),
    'manage_tickets': ('manage_channels',),
    'export_data': ('manage_guild',),
}


def _role_grants(member: Any, grants: Mapping[str, Any] | None) -> set[str]:
    if not grants:
        return set()
    role_ids = {str(role.id) for role in getattr(member, 'roles', ())}
    granted: set[str] = set()
    for role_id in role_ids:
        values = grants.get(role_id, ())
        if isinstance(values, (list, tuple, set, frozenset)):
            granted.update(str(value) for value in values)
    return granted


def has_capability(member: Any, capability: str, role_grants: Mapping[str, Any] | None = None) -> bool:
    """Return whether a guild member can perform a named capability.

    Server owners and administrators retain the native Discord override. Role
    grants are additive and only apply to known capabilities.
    """
    if capability not in CAPABILITIES or member is None:
        return False
    guild = getattr(member, 'guild', None)
    if guild is not None and getattr(guild, 'owner_id', None) == getattr(member, 'id', None):
        return True
    permissions = getattr(member, 'guild_permissions', None)
    if permissions is not None and getattr(permissions, 'administrator', False):
        return True
    if capability in _role_grants(member, role_grants):
        return True
    if permissions is None:
        return False
    return any(getattr(permissions, permission, False) for permission in _NATIVE_PERMISSIONS[capability])


async def member_has_capability(member: Any, capability: str) -> bool:
    """Guild-wide capability check for Discord commands, honouring dashboard role grants.

    Unlike ``commands.has_permissions`` (which resolves permissions in the invoking channel and so
    honours channel overwrites), this evaluates guild-level authority plus stored role grants.
    """
    if has_capability(member, capability):
        return True
    guild = getattr(member, 'guild', None)
    if guild is None:
        return False
    from database.administration import GuildPermissionModel

    try:
        grants = await GuildPermissionModel().get_role_grants(guild.id)
    except Exception:
        return False
    return has_capability(member, capability, grants)


def require_capability(capability: str):
    """``commands.check`` that fails closed unless the author holds ``capability`` guild-wide."""
    from discord.ext import commands

    async def predicate(ctx: Any) -> bool:
        if getattr(ctx, 'guild', None) is None:
            raise commands.NoPrivateMessage()
        if await member_has_capability(ctx.author, capability):
            return True
        raise commands.MissingPermissions(['manage_guild'])

    return commands.check(predicate)
