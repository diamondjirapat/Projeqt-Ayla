"""Discord actions shared by web and command-based case reviews."""

from datetime import UTC, datetime, timedelta

import discord


async def undo_case_action(guild, actor, case: dict, reason: str) -> None:
    """Undo a reversible sanction before persisting its revocation.

    Already-removed sanctions are successful retries. A newer timeout must not
    be removed while reviewing an older case.
    """
    action = case.get('action')
    if action not in {'ban', 'timeout'}:
        return
    permission = 'ban_members' if action == 'ban' else 'moderate_members'
    if actor is None or not (
        actor.guild_permissions.administrator or getattr(actor.guild_permissions, permission, False)
    ):
        raise PermissionError(f'The {permission} permission is required to reverse this sanction.')
    target_id = int(case['target_user_id'])
    if action == 'ban':
        try:
            await guild.unban(discord.Object(id=target_id), reason=reason)
        except discord.NotFound:
            pass
        return
    target = guild.get_member(target_id)
    if target is None:
        try:
            target = await guild.fetch_member(target_id)
        except discord.NotFound:
            return
    if actor.id != guild.owner_id and not actor.guild_permissions.administrator and target.top_role >= actor.top_role:
        raise PermissionError('The member is at or above your highest role.')
    if guild.me is None or target.top_role >= guild.me.top_role:
        raise PermissionError('Ayla cannot remove a timeout at or above its highest role.')
    current = target.timed_out_until
    if current is None or current <= datetime.now(UTC):
        return
    expiry = case.get('expires_at')
    if isinstance(expiry, str):
        expiry = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
    if expiry is None:
        raise ValueError('The original timeout expiry is missing; remove the timeout manually before retrying.')
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if current > expiry + timedelta(seconds=1):
        raise ValueError('A newer timeout is active; review that case before removing this sanction.')
    await target.timeout(None, reason=reason)
