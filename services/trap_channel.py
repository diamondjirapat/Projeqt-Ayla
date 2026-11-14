"""Discord side of the trap channel: building, posting, and checking the warning embed.

Shared by the ``TrapChannel`` cog and the internal API so the dashboard and the slash commands post
exactly the same warning.
"""

import logging
from typing import Any, Dict, Mapping

import discord

from utils.i18n import i18n
from utils.trap_channel import render_trap_embed_parts

logger = logging.getLogger(__name__)

_ACTION_PERMISSIONS = {
    'timeout': 'moderate_members',
    'kick': 'kick_members',
    'ban': 'ban_members',
    'delete': None,
}


def localized_duration(minutes: int, locale: str) -> str:
    minutes = max(1, int(minutes))
    parts = []
    for size, unit in ((1440, 'day'), (60, 'hour'), (1, 'minute')):
        amount, minutes = divmod(minutes, size)
        if amount:
            key = f'trap_channel.units.{unit}' + ('' if amount == 1 else 's')
            parts.append(i18n.get_text(key, locale, count=amount))
    return ' '.join(parts)


def penalty_text(config: Mapping[str, Any], locale: str) -> str:
    duration = localized_duration(config['timeout_minutes'], locale)
    return i18n.get_text(f'trap_channel.penalty.{config["action"]}', locale, duration=duration)


def placeholder_values(guild: discord.Guild, channel: Any, config: Mapping[str, Any], locale: str) -> Dict[str, str]:
    return {
        'server': guild.name,
        'channel': getattr(channel, 'mention', '') or f'#{getattr(channel, "name", "")}',
        'penalty': penalty_text(config, locale),
        'duration': localized_duration(config['timeout_minutes'], locale),
    }


async def build_warning(guild: discord.Guild, channel: Any, config: Mapping[str, Any]) -> tuple[str, discord.Embed]:
    locale = await i18n.get_guild_locale(guild.id) or i18n.default_locale
    parts = render_trap_embed_parts(config, placeholder_values(guild, channel, config, locale))
    embed = discord.Embed(
        title=parts['title'] or None,
        description=parts['description'] or None,
        color=discord.Color(parts['color']),
    )
    if parts['image_url']:
        embed.set_image(url=parts['image_url'])
    if parts['thumbnail_url']:
        embed.set_thumbnail(url=parts['thumbnail_url'])
    if parts['footer_text']:
        embed.set_footer(text=parts['footer_text'], icon_url=parts['footer_icon_url'])
    if parts['author']['name']:
        embed.set_author(name=parts['author']['name'], url=parts['author']['url'], icon_url=parts['author']['icon_url'])
    for field in parts['fields']:
        embed.add_field(name=field['name'], value=field['value'], inline=field['inline'])
    if not (embed.title or embed.description or embed.fields or parts['content']):
        embed.description = penalty_text(config, locale)
    return parts['content'], embed


def missing_permissions(guild: discord.Guild, channel: Any, action: str) -> list[str]:
    """Bot permissions the trap needs in ``channel`` but does not have."""
    me = guild.me
    if me is None or channel is None:
        return []
    perms = channel.permissions_for(me)
    needed = ['view_channel', 'send_messages', 'embed_links', 'read_message_history', 'manage_messages']
    guild_needed = _ACTION_PERMISSIONS.get(action)
    missing = [name for name in needed if not getattr(perms, name, False)]
    if guild_needed and not getattr(me.guild_permissions, guild_needed, False):
        missing.append(guild_needed)
    return missing


async def publish_warning(
    guild: discord.Guild,
    config: Dict[str, Any],
    *,
    previous_channel_id: int | None = None,
    previous_message_id: int | None = None,
) -> int | None:
    """Edit the existing warning in place, or post a new one. Returns the warning message id.

    A warning left behind in a previous trap channel is removed so it does not mislead members.
    """
    channel = guild.get_channel(config['channel_id']) if config.get('channel_id') else None
    if previous_message_id and previous_channel_id and previous_channel_id != config.get('channel_id'):
        old_channel = guild.get_channel(previous_channel_id)
        if old_channel is not None:
            try:
                await old_channel.get_partial_message(previous_message_id).delete()
            except discord.HTTPException:
                pass
        config['message_id'] = None
    if not isinstance(channel, discord.TextChannel):
        return None

    content, embed = await build_warning(guild, channel, config)
    allowed = discord.AllowedMentions.none()
    message_id = config.get('message_id')
    if message_id:
        try:
            await channel.get_partial_message(message_id).edit(content=content or None, embed=embed)
            return message_id
        except discord.NotFound:
            pass
    message = await channel.send(content=content or None, embed=embed, allowed_mentions=allowed)
    return message.id
