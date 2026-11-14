"""Bound repeated REST lookups for users absent from active player guilds."""

import discord
from utils.cache import AsyncTTLCache

_missing_members = AsyncTTLCache(ttl=15, max_size=5000, name='discord.missing_members')


async def fetch_missing_member(guild, user_id):
    key = (guild.id, user_id)

    async def load():
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None

    member = await _missing_members.get_or_load(key, load)
    # Do not cache permission-bearing member objects: authorization stays fresh.
    if member is not None:
        _missing_members.pop(key)
    return member
