import discord
from discord.ext import commands
from typing import Callable, List, Literal, Optional, TypeAlias
import logging
import time

from utils.cache import CACHE_MISS, TTLCache
from database.prefix_models import UserPrefixModel, GuildPrefixModel
from config import Config

logger = logging.getLogger(__name__)

PrefixScope: TypeAlias = Literal['user', 'guild']
CacheKey: TypeAlias = tuple[PrefixScope, int]


class PrefixManager:
    def __init__(
        self,
        user_prefix_model: Optional[UserPrefixModel] = None,
        guild_prefix_model: Optional[GuildPrefixModel] = None,
        *,
        cache_ttl: float = 300,
        max_cache_size: int = 5000,
        clock: Callable[[], float] = time.monotonic,
    ):
        if cache_ttl <= 0:
            raise ValueError("cache_ttl must be positive")
        if max_cache_size <= 0:
            raise ValueError("max_cache_size must be positive")

        self.user_prefix_model = user_prefix_model or UserPrefixModel()
        self.guild_prefix_model = guild_prefix_model or GuildPrefixModel()
        self.default_prefix = Config.COMMAND_PREFIX
        self.max_prefix_length = 10
        self.forbidden_prefixes = ('@', '#', '/', '\\', '`')
        self._cache: TTLCache[CacheKey, Optional[str]] = TTLCache(
            ttl=cache_ttl,
            max_size=max_cache_size,
            clock=clock,
        )

    def _get_cache_key(self, type_: PrefixScope, id_: int) -> CacheKey:
        return type_, id_

    def _get_cached_value(self, type_: PrefixScope, id_: int):
        """Return the cached prefix, or ``_CACHE_MISS`` when absent/expired.

        A cached ``None`` (meaning "no custom prefix") is a real value and is
        returned as-is so we don't re-query the database every message.
        """
        return self._cache.get(self._get_cache_key(type_, id_))

    def _update_cache(self, type_: PrefixScope, id_: int, prefix: Optional[str]):
        self._cache.set(self._get_cache_key(type_, id_), prefix)

    def _invalidate_cache(self, type_: PrefixScope, id_: int):
        self._cache.pop(self._get_cache_key(type_, id_))

    async def get_prefix(self, bot: commands.Bot, message: discord.Message) -> List[str]:
        prefixes = []
        if bot and getattr(bot, 'user', None):
            prefixes = [f'<@!{bot.user.id}> ', f'<@{bot.user.id}> ', f'<@!{bot.user.id}>', f'<@{bot.user.id}>']

        author_id = getattr(message.author, 'id', None) if message.author else None
        guild_id = getattr(message.guild, 'id', None) if message.guild else None

        if not guild_id and not author_id:
            return prefixes + [self.default_prefix]

        # User Prefix (Cache -> DB)
        user_prefix = None
        if author_id:
            user_prefix = self._get_cached_value('user', author_id)
            if user_prefix is CACHE_MISS:
                user_prefix = await self.user_prefix_model.get_user_prefix(author_id)
                # Cache even when None so we don't re-query on every message.
                self._update_cache('user', author_id, user_prefix)

            if user_prefix:
                prefixes.append(user_prefix)

        # Guild Prefix (Cache -> DB)
        guild_prefix = None
        if guild_id:
            guild_prefix = self._get_cached_value('guild', guild_id)
            if guild_prefix is CACHE_MISS:
                guild_prefix = await self.guild_prefix_model.get_guild_prefix(guild_id)
                self._update_cache('guild', guild_id, guild_prefix)

            if guild_prefix and guild_prefix not in prefixes:
                prefixes.append(guild_prefix)

        # fallback
        if self.default_prefix not in prefixes:
            prefixes.append(self.default_prefix)

        # Sort non-mention prefixes longest first (Discord.py checks prefixes in list order)
        mention_prefixes = [p for p in prefixes if p.startswith('<@')]
        text_prefixes = [p for p in prefixes if not p.startswith('<@')]
        text_prefixes.sort(key=len, reverse=True)

        final = mention_prefixes + text_prefixes
        logger.debug(f"[PREFIX] Resolved for user={author_id}, guild={guild_id}: user={user_prefix!r}, guild={guild_prefix!r}, final={text_prefixes}")
        return final

    async def set_user_prefix(self, user_id: int, prefix: str) -> tuple[bool, str]:
        valid, reason = self.validate_prefix(prefix)
        if not valid:
            logger.warning(f"[PREFIX] Invalid prefix '{prefix}' for user {user_id}: {reason}")
            return False, reason

        prefix = prefix.strip()
        success = await self.user_prefix_model.set_user_prefix(user_id, prefix)
        if success:
            self._update_cache('user', user_id, prefix)
            logger.info(f"[PREFIX] User {user_id} set personal prefix to '{prefix}'")
            return True, "Prefix updated successfully"
        else:
            logger.error(f"[PREFIX] Failed to save personal prefix for user {user_id}")
            return False, "Failed to save prefix to database"

    async def set_guild_prefix(self, guild_id: int, prefix: str) -> tuple[bool, str]:
        valid, reason = self.validate_prefix(prefix)
        if not valid:
            logger.warning(f"[PREFIX] Invalid prefix '{prefix}' for guild {guild_id}: {reason}")
            return False, reason

        prefix = prefix.strip()
        success = await self.guild_prefix_model.set_guild_prefix(guild_id, prefix)
        if success:
            self._update_cache('guild', guild_id, prefix)
            logger.info(f"[PREFIX] Guild {guild_id} set server prefix to '{prefix}'")
            return True, "Prefix updated successfully"
        else:
            logger.error(f"[PREFIX] Failed to save server prefix for guild {guild_id}")
            return False, "Failed to save prefix to database"

    async def remove_user_prefix(self, user_id: int) -> bool:
        success = await self.user_prefix_model.remove_user_prefix(user_id)
        if success:
            self._update_cache('user', user_id, None)
            logger.info(f"[PREFIX] User {user_id} removed personal prefix")
        else:
            self._invalidate_cache('user', user_id)
            logger.debug(f"[PREFIX] No personal prefix to remove for user {user_id}")
        return success

    async def remove_guild_prefix(self, guild_id: int) -> bool:
        success = await self.guild_prefix_model.remove_guild_prefix(guild_id)
        if success:
            self._update_cache('guild', guild_id, None)
            logger.info(f"[PREFIX] Guild {guild_id} reset server prefix to default")
        else:
            self._invalidate_cache('guild', guild_id)
            logger.debug(f"[PREFIX] No custom server prefix to reset for guild {guild_id}")
        return success

    def validate_prefix(self, prefix: str) -> tuple[bool, str]:
        if prefix is None or not isinstance(prefix, str):
            return False, "Prefix must be a valid text string"
        if any(ord(character) < 32 or ord(character) == 127 for character in prefix):
            return False, "Prefix cannot contain control characters"
        prefix_clean = prefix.strip()
        if not prefix_clean:
            return False, "Prefix cannot be empty or only whitespace"
        if len(prefix_clean) > self.max_prefix_length:
            return False, f"Prefix cannot be longer than {self.max_prefix_length} characters"
        if any(prefix_clean.startswith(f) for f in self.forbidden_prefixes):
            return False, f"Prefix `{prefix_clean}` is not allowed"
        if prefix_clean.startswith('<@') and prefix_clean.endswith('>'):
            return False, "Prefix cannot be a mention"
        return True, "Valid prefix"

    async def get_user_prefix(self, user_id: int) -> Optional[str]:
        cached = self._get_cached_value('user', user_id)
        if cached is not CACHE_MISS:
            return cached
        prefix = await self.user_prefix_model.get_user_prefix(user_id)
        self._update_cache('user', user_id, prefix)
        return prefix

    async def get_guild_prefix(self, guild_id: int) -> Optional[str]:
        cached = self._get_cached_value('guild', guild_id)
        if cached is not CACHE_MISS:
            return cached
        prefix = await self.guild_prefix_model.get_guild_prefix(guild_id)
        self._update_cache('guild', guild_id, prefix)
        return prefix

    async def get_effective_prefix(self, user_id: int, guild_id: int = None) -> str:
        user_prefix = await self.get_user_prefix(user_id)
        if user_prefix: 
            return user_prefix
        
        if guild_id:
            guild_prefix = await self.get_guild_prefix(guild_id)
            if guild_prefix: 
                return guild_prefix
        return self.default_prefix

    async def get_prefix_info(self, user_id: int, guild_id: int = None) -> dict:
        user_prefix = await self.get_user_prefix(user_id)
        guild_prefix = await self.get_guild_prefix(guild_id) if guild_id else None
        effective_prefix = await self.get_effective_prefix(user_id, guild_id)

        return {
            'user_prefix': user_prefix,
            'guild_prefix': guild_prefix,
            'default_prefix': self.default_prefix,
            'effective_prefix': effective_prefix,
            'priority': 'user' if user_prefix else ('guild' if guild_prefix else 'default')
        }

prefix_manager = PrefixManager()
