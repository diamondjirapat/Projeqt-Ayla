import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import discord

from utils.cache import CACHE_MISS, TTLCache

logger = logging.getLogger(__name__)

# Cache profiles for 5 minutes with a maximum of 2,000 entries
_profile_cache: TTLCache[Tuple[int, int], Tuple[str, str]] = TTLCache(ttl=300.0, max_size=2000, name='discord.profiles')


# Concurrent lookups of the same profile share one Discord REST round trip.
_inflight: Dict[Tuple[int, int], 'asyncio.Future[Tuple[str, str]]'] = {}

# Discord REST lookups one leaderboard request may trigger; remaining cache misses use stored names.
LEADERBOARD_REST_BUDGET = 10


def clear_profile_cache() -> None:
    """Clear in-memory profile cache (useful for testing or cache invalidation)."""
    _profile_cache.clear()
    _inflight.clear()


def _get_all_bots(bot: Any) -> List[Any]:
    if not bot:
        return []
    return getattr(bot, 'all_bots', [bot])


def _find_guild(bot: Any, guild_id: int) -> Optional[Any]:
    for b in _get_all_bots(bot):
        if not b or not hasattr(b, 'get_guild'):
            continue
        g = b.get_guild(guild_id)
        if g is not None:
            return g
    return None


async def fetch_discord_profile(
    bot: Any,
    user_id: int,
    guild_id: Optional[int] = None,
    fallback_username: Optional[str] = None,
    fallback_avatar: Optional[str] = None,
    *,
    allow_rest: Union[bool, Callable[[], bool]] = True,
) -> Tuple[str, str]:
    """
    Fetch a user's display name and avatar URL live from Discord.

    With ``allow_rest=False`` (or a callable budget that returns False) only gateway caches are consulted;
    an unresolved profile then returns the fallbacks without caching them, so a later request with REST
    budget can still resolve it. A callable is only consulted when the gateway caches miss.

    - If guild_id is provided, attempts to fetch guild member (server nickname & server avatar).
    - Falls back to global user profile (global display name & avatar).
    - If user cannot be found, falls back to provided fallbacks or 'User <id>'.
    """
    cache_key = (int(guild_id) if guild_id else 0, int(user_id))
    cached = _profile_cache.get(cache_key)
    if cached is not CACHE_MISS:
        return cached
    pending = _inflight.get(cache_key)
    if pending is not None:
        return await asyncio.shield(pending)
    gateway = _resolve_from_cache(bot, int(user_id), guild_id)
    if gateway is not None:
        resolved = (gateway[0], gateway[1] if gateway[1] is not None else (fallback_avatar or ''))
        _profile_cache.set(cache_key, resolved)
        return resolved
    rest_ok = allow_rest() if callable(allow_rest) else allow_rest
    if not rest_ok:
        return fallback_username or f'User {user_id}', fallback_avatar or ''

    future: asyncio.Future[Tuple[str, str]] = asyncio.get_running_loop().create_future()
    _inflight[cache_key] = future
    try:
        resolved = await _fetch_profile(bot, int(user_id), guild_id, fallback_username, fallback_avatar)
    except BaseException as exc:
        if not future.done():
            future.set_exception(exc)
            future.exception()  # mark retrieved for waiters-less failures
        raise
    else:
        _profile_cache.set(cache_key, resolved)
        if not future.done():
            future.set_result(resolved)
        return resolved
    finally:
        _inflight.pop(cache_key, None)


def _profile_of(obj: Any) -> Tuple[Optional[str], Optional[str]]:
    name = getattr(obj, 'display_name', None) or getattr(obj, 'name', None)
    avatar = str(obj.display_avatar.url) if getattr(obj, 'display_avatar', None) else None
    return name, avatar


def _resolve_from_cache(bot: Any, user_id: int, guild_id: Optional[int]) -> Optional[Tuple[str, Optional[str]]]:
    """Profile from gateway caches only (no REST), or None when a REST lookup would be needed."""
    if not bot:
        return None
    if guild_id:
        guild = _find_guild(bot, int(guild_id))
        if guild is not None:
            member = guild.get_member(user_id) if hasattr(guild, 'get_member') else None
            if member is not None:
                name, avatar = _profile_of(member)
                return (name, avatar) if name else None
            # The guild path REST-fetches the member before falling back to the global user.
            return None
    for b in _get_all_bots(bot):
        user = b.get_user(user_id) if b and hasattr(b, 'get_user') else None
        if user is not None:
            name, avatar = _profile_of(user)
            return (name, avatar) if name else None
    return None


async def _fetch_profile(
    bot: Any,
    user_id: int,
    guild_id: Optional[int],
    fallback_username: Optional[str],
    fallback_avatar: Optional[str],
) -> Tuple[str, str]:
    name: Optional[str] = None
    avatar: Optional[str] = None

    # 1. Check guild member first if guild_id provided
    if guild_id and bot:
        guild = _find_guild(bot, int(guild_id))
        if guild:
            member = None
            if hasattr(guild, 'get_member'):
                member = guild.get_member(int(user_id))
            if member is None and hasattr(guild, 'fetch_member'):
                try:
                    member = await guild.fetch_member(int(user_id))
                except (discord.NotFound, discord.HTTPException, Exception):
                    member = None

            if member is not None:
                name = getattr(member, 'display_name', None) or getattr(member, 'name', None)
                if hasattr(member, 'display_avatar') and member.display_avatar:
                    avatar = str(member.display_avatar.url)

    # 2. Check bot global user cache / fetch_user if member not found
    if not name and bot:
        user = None
        for b in _get_all_bots(bot):
            if not b:
                continue
            if hasattr(b, 'get_user'):
                user = b.get_user(int(user_id))
                if user is not None:
                    break

        if user is None:
            # User objects are global, so one REST lookup (through the first capable bot) is enough.
            fetcher = next((b for b in _get_all_bots(bot) if b and hasattr(b, 'fetch_user')), None)
            if fetcher is not None:
                try:
                    user = await fetcher.fetch_user(int(user_id))
                except (discord.NotFound, discord.HTTPException, Exception):
                    user = None

        if user is not None:
            name = getattr(user, 'display_name', None) or getattr(user, 'name', None)
            if hasattr(user, 'display_avatar') and user.display_avatar:
                avatar = str(user.display_avatar.url)

    # 3. Apply fallback if unresolved
    final_name = name or fallback_username or f'User {user_id}'
    final_avatar = avatar if avatar is not None else (fallback_avatar or '')

    return final_name, final_avatar


async def enrich_leaderboard_profiles(
    bot: Any,
    entries: List[Dict[str, Any]],
    guild_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Enrich a list of leaderboard entry dictionaries in-place with up-to-date Discord profile data.
    """
    if not entries or not bot:
        return entries

    rest_budget = LEADERBOARD_REST_BUDGET

    def take_rest_budget() -> bool:
        nonlocal rest_budget
        if rest_budget <= 0:
            return False
        rest_budget -= 1
        return True

    async def _resolve_entry(entry: Dict[str, Any]) -> None:
        user_id_raw = entry.get('user_id')
        if user_id_raw is None:
            return
        try:
            uid = int(user_id_raw)
        except (ValueError, TypeError):
            return

        name, avatar = await fetch_discord_profile(
            bot=bot,
            user_id=uid,
            guild_id=guild_id,
            fallback_username=entry.get('username'),
            fallback_avatar=entry.get('avatar_url'),
            allow_rest=take_rest_budget,
        )
        entry['username'] = name
        entry['avatar_url'] = avatar

    await asyncio.gather(*(_resolve_entry(entry) for entry in entries))
    return entries
