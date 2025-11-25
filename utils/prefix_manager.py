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
    
    async def get_prefix(self, bot: commands.Bot, message: discord.Message) -> List[str]:
        """
        Get the appropriate prefix for a message.
        Priority: User prefix > Guild prefix > Default prefix
        Always includes mentions as valid prefixes.
        """
        prefixes = []

        prefixes.extend([f'<@!{bot.user.id}> ', f'<@{bot.user.id}> ', f'<@!{bot.user.id}>', f'<@{bot.user.id}>'])

        if message.author:
            user_prefix = await self.user_prefix_model.get_user_prefix(message.author.id)
            if user_prefix:
                prefixes.append(user_prefix)
                logger.debug(f"Using user prefix for {message.author}: {prefixes}")
                return prefixes
        
        # guild's custom prefix
        if message.guild:
            guild_prefix = await self.guild_prefix_model.get_guild_prefix(message.guild.id)
            if guild_prefix:
                prefixes.append(guild_prefix)
                logger.debug(f"Using guild prefix for {message.guild}: {prefixes}")
                return prefixes
        
        # default prefix
        prefixes.append(self.default_prefix)
        logger.debug(f"Using default prefixes: {prefixes}")
        return prefixes
    
    async def set_user_prefix(self, user_id: int, prefix: str) -> tuple[bool, str]:
        """Set a user's personal prefix with validation"""
        validation_result = self.validate_prefix(prefix)
        if not validation_result[0]:
            return validation_result
        
        success = await self.user_prefix_model.set_user_prefix(user_id, prefix)
        if success:
            return True, f"Personal prefix set to `{prefix}`"
        else:
            return False, "Failed to save prefix to database"
    
    async def set_guild_prefix(self, guild_id: int, prefix: str) -> tuple[bool, str]:
        """Set a guild's default prefix with validation"""
        validation_result = self.validate_prefix(prefix)
        if not validation_result[0]:
            return validation_result
        
        success = await self.guild_prefix_model.set_guild_prefix(guild_id, prefix)
        if success:
            return True, f"Server prefix set to `{prefix}`"
        else:
            return False, "Failed to save prefix to database"
    
    async def remove_user_prefix(self, user_id: int) -> tuple[bool, str]:
        """Remove a user's personal prefix"""
        success = await self.user_prefix_model.remove_user_prefix(user_id)
        if success:
            return True, "Personal prefix removed"
        else:
            return False, "No personal prefix was set"
    
    async def remove_guild_prefix(self, guild_id: int) -> tuple[bool, str]:
        """Remove a guild's custom prefix"""
        success = await self.guild_prefix_model.remove_guild_prefix(guild_id)
        if success:
            return True, f"Server prefix reset to default (`{self.default_prefix}`)"
        else:
            return False, "No custom server prefix was set"
    
    def validate_prefix(self, prefix: str) -> tuple[bool, str]:
        """Validate a prefix"""
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
        """Get a user's personal prefix"""
        return await self.user_prefix_model.get_user_prefix(user_id)
    
    async def get_guild_prefix(self, guild_id: int) -> Optional[str]:
        """Get a guild's custom prefix"""
        return await self.guild_prefix_model.get_guild_prefix(guild_id)
    
    async def get_effective_prefix(self, user_id: int, guild_id: int = None) -> str:
        """Get the effective prefix for a user in a guild"""
        user_prefix = await self.get_user_prefix(user_id)
        if user_prefix:
            return user_prefix

        if guild_id:
            guild_prefix = await self.get_guild_prefix(guild_id)
            if guild_prefix:
                return guild_prefix

        return self.default_prefix
    
    async def get_prefix_info(self, user_id: int, guild_id: int = None) -> dict:
        """Get comprehensive prefix information"""
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
