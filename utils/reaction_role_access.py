"""Guild and channel boundaries for reaction-role message access."""

import re
import time

import discord

# Guild documents remember where each reaction-role message lives so lookups need one
# API call instead of probing every text channel.
CHANNEL_HINTS_FIELD = 'reaction_role_channels'

# A fallback scan costs one REST call per readable text channel, so it is bounded and its misses are
# remembered: deleted or unknown ids would otherwise re-trigger a full scan on every settings request.
MAX_SCAN_CHANNELS = 50
MISS_TTL_SECONDS = 600.0
_MISS_CACHE_MAX = 5000
_missing: dict[tuple[int, int], float] = {}


def _known_missing(guild_id: int, message_id: int) -> bool:
    expiry = _missing.get((guild_id, message_id))
    if expiry is None:
        return False
    if expiry > time.monotonic():
        return True
    _missing.pop((guild_id, message_id), None)
    return False


def _remember_missing(guild_id: int, message_id: int) -> None:
    if len(_missing) >= _MISS_CACHE_MAX:
        _missing.pop(next(iter(_missing)))
    _missing[(guild_id, message_id)] = time.monotonic() + MISS_TTL_SECONDS


def forget_missing(guild_id: int, message_id: int) -> None:
    """Drop a remembered miss, e.g. after the message id was saved again with a link."""
    _missing.pop((int(guild_id), int(message_id)), None)


_MESSAGE_LINK = re.compile(r'^https?://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)/?$')


def parse_message_reference(raw) -> tuple[int, int | None]:
    """Parse a message id or a message link into ``(message_id, channel_id or None)``.

    Raises ``ValueError`` when the value is neither.
    """
    text = str(raw).strip()
    match = _MESSAGE_LINK.match(text)
    if match:
        return int(match.group(3)), int(match.group(2))
    return int(text), None


def channel_hint(guild_data, message_id) -> int | None:
    hints = (guild_data or {}).get(CHANNEL_HINTS_FIELD) or {}
    try:
        return int(hints.get(str(message_id)))
    except (TypeError, ValueError):
        return None


def can_read_message(message, guild, member):
    if member is None or getattr(message.guild, 'id', None) != guild.id:
        return False
    permissions = message.channel.permissions_for(member)
    return permissions.view_channel and permissions.read_message_history


def _readable_by(channel, actor) -> bool:
    if actor is None:
        return True
    permissions = channel.permissions_for(actor)
    return permissions.view_channel and permissions.read_message_history


async def locate_message(bot, guild, message_id: int, *, hint: int | None = None, member=None):
    """Find a guild message by id: client caches, then the hinted channel, then a channel scan.

    The scan skips channels the bot (and ``member``, when given) cannot read, so it never
    spends API calls on requests that would be rejected.
    """
    for client in getattr(bot, 'all_bots', None) or [bot]:
        message = discord.utils.get(getattr(client, 'cached_messages', ()), id=message_id)
        if message is not None and getattr(message.guild, 'id', None) == guild.id:
            return message
    me = getattr(guild, 'me', None)
    hinted = guild.get_channel(hint) if hint else None
    if hinted is not None and hasattr(hinted, 'fetch_message'):
        try:
            return await hinted.fetch_message(message_id)
        except discord.HTTPException:
            pass
    guild_id = int(getattr(guild, 'id', 0) or 0)
    if _known_missing(guild_id, message_id):
        return None
    scanned = 0
    complete = True  # whether every channel the bot can read was searched
    for channel in guild.text_channels:
        if channel is hinted or not _readable_by(channel, me):
            continue
        if not _readable_by(channel, member):
            complete = False
            continue
        if scanned >= MAX_SCAN_CHANNELS:
            complete = False
            break
        scanned += 1
        try:
            return await channel.fetch_message(message_id)
        except discord.HTTPException:
            continue
    if complete:
        # Only an exhaustive miss proves the message is gone for every requester.
        _remember_missing(guild_id, message_id)
    return None


async def resolve_reaction_role_message(bot, guild, message_id, member, *, hint: int | None = None):
    """Resolve only messages the requesting member can read in this guild."""
    if member is None:
        return None
    try:
        message_id = int(message_id)
    except (TypeError, ValueError):
        return None
    message = await locate_message(bot, guild, message_id, hint=hint, member=member)
    if message is not None and can_read_message(message, guild, member):
        return message
    return None
