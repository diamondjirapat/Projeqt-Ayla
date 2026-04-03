import discord
from discord.ext import commands
from typing import Union, List, Optional
import logging
from database.prefix_models import UserPrefixModel, GuildPrefixModel
from config import Config

logger = logging.getLogger(__name__)


class PrefixManager:
    def __init__(self):
        self.user_prefix_model = UserPrefixModel()
        self.guild_prefix_model = GuildPrefixModel()
        self.default_prefix = Config.COMMAND_PREFIX
        self.max_prefix_length = 10
        self.forbidden_prefixes = ['@', '#', '/', '\\', '`', '```']
        self._cache = {}

    def _get_cache_key(self, type_: str, id_: int) -> str:
        return f"{type_}_{id_}"

    def _update_cache(self, type_: str, id_: int, prefix: str):
        self._cache[self._get_cache_key(type_, id_)] = prefix

    def _invalidate_cache(self, type_: str, id_: int):
        key = self._get_cache_key(type_, id_)
        if key in self._cache:
            del self._cache[key]

    async def get_prefix(self, bot: commands.Bot, message: discord.Message) -> List[str]:
        prefixes = [f'<@!{bot.user.id}> ', f'<@{bot.user.id}> ', f'<@!{bot.user.id}>', f'<@{bot.user.id}>']

        if not message.guild and not message.author:
            return prefixes + [self.default_prefix]

        # User Prefix (Cache -> DB)
        user_prefix = None
        if message.author:
            cache_key = self._get_cache_key('user', message.author.id)
            if cache_key in self._cache:
                user_prefix = self._cache[cache_key]
            else:
                user_prefix = await self.user_prefix_model.get_user_prefix(message.author.id)
                if user_prefix:
                    self._update_cache('user', message.author.id, user_prefix)

            if user_prefix:
                prefixes.append(user_prefix)

        # Guild Prefix (Cache -> DB)
        guild_prefix = None
        if message.guild:
            cache_key = self._get_cache_key('guild', message.guild.id)
            if cache_key in self._cache:
                guild_prefix = self._cache[cache_key]
            else:
                guild_prefix = await self.guild_prefix_model.get_guild_prefix(message.guild.id)
                if guild_prefix:
                    self._update_cache('guild', message.guild.id, guild_prefix)
            
            if guild_prefix and guild_prefix not in prefixes:
                prefixes.append(guild_prefix)

        # fallback
        if self.default_prefix not in prefixes:
            prefixes.append(self.default_prefix)

        # (Edge case) Sort non-mention prefixes longest first (Discord.py checks prefixes in list order)
        mention_prefixes = [p for p in prefixes if p.startswith('<@')]
        text_prefixes = [p for p in prefixes if not p.startswith('<@')]
        text_prefixes.sort(key=len, reverse=True)

        final = mention_prefixes + text_prefixes
        logger.debug(f"[PREFIX] Resolved for user={message.author.id}, guild={getattr(message.guild, 'id', None)}: user={user_prefix!r}, guild={guild_prefix!r}, final={text_prefixes}")
        return final

    async def set_user_prefix(self, user_id: int, prefix: str) -> bool:
        valid, reason = self.validate_prefix(prefix)
        if not valid:
            logger.warning(f"[PREFIX] Invalid prefix '{prefix}' for user {user_id}: {reason}")
            return False

        success = await self.user_prefix_model.set_user_prefix(user_id, prefix)
        if success:
            self._update_cache('user', user_id, prefix)
            logger.info(f"[PREFIX] User {user_id} set personal prefix to '{prefix}'")
        else:
            logger.error(f"[PREFIX] Failed to save personal prefix for user {user_id}")
        return success

    async def set_guild_prefix(self, guild_id: int, prefix: str) -> bool:
        valid, reason = self.validate_prefix(prefix)
        if not valid:
            logger.warning(f"[PREFIX] Invalid prefix '{prefix}' for guild {guild_id}: {reason}")
            return False

        success = await self.guild_prefix_model.set_guild_prefix(guild_id, prefix)
        if success:
            self._update_cache('guild', guild_id, prefix)
            logger.info(f"[PREFIX] Guild {guild_id} set server prefix to '{prefix}'")
        else:
            logger.error(f"[PREFIX] Failed to save server prefix for guild {guild_id}")
        return success

    async def remove_user_prefix(self, user_id: int) -> bool:
        success = await self.user_prefix_model.remove_user_prefix(user_id)
        if success:
            self._invalidate_cache('user', user_id)
            logger.info(f"[PREFIX] User {user_id} removed personal prefix")
        else:
            logger.debug(f"[PREFIX] No personal prefix to remove for user {user_id}")
        return success

    async def remove_guild_prefix(self, guild_id: int) -> bool:
        success = await self.guild_prefix_model.remove_guild_prefix(guild_id)
        if success:
            self._invalidate_cache('guild', guild_id)
            logger.info(f"[PREFIX] Guild {guild_id} reset server prefix to default")
        else:
            logger.debug(f"[PREFIX] No custom server prefix to reset for guild {guild_id}")
        return success

    def validate_prefix(self, prefix: str) -> tuple[bool, str]:
        if not prefix:
            return False, "Prefix cannot be empty"
        if len(prefix) > self.max_prefix_length:
            return False, f"Prefix cannot be longer than {self.max_prefix_length} characters"
        if prefix in self.forbidden_prefixes:
            return False, f"Prefix `{prefix}` is not allowed"
        if prefix.startswith('<@') and prefix.endswith('>'):
            return False, "Prefix cannot be a mention"
        if prefix.isspace():
            return False, "Prefix cannot be only whitespace"
        return True, "Valid prefix"

    async def get_user_prefix(self, user_id: int) -> Optional[str]:
        return await self.user_prefix_model.get_user_prefix(user_id)

    async def get_guild_prefix(self, guild_id: int) -> Optional[str]:
        return await self.guild_prefix_model.get_guild_prefix(guild_id)

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
