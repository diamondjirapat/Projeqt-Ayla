import json
import logging
from typing import Any, Dict, Optional, Union

import discord
from discord.ext import commands

from config import Config
from database.models import UserModel, GuildModel
from utils.cache import CACHE_MISS, TTLCache

logger = logging.getLogger(__name__)

class I18nManager:
    def __init__(self):
        self.translations: Dict[str, Dict] = {}
        self.default_locale = 'en'
        self.supported_locales = ['en', 'th']
        self.user_model = UserModel()
        self.guild_model = GuildModel()
        # Cache missing values too; most users do not set a custom locale.
        self._user_locale_cache: TTLCache[int, Optional[str]] = TTLCache(ttl=300, max_size=5000)
        self._guild_locale_cache: TTLCache[int, Optional[str]] = TTLCache(ttl=300, max_size=5000)
        self.load_translations()
    
    def load_translations(self):
        """Load all translation files"""
        locales_dir = Config.PROJECT_ROOT / 'locales'
        
        for locale in self.supported_locales:
            file_path = locales_dir / f'{locale}.json'
            try:
                with file_path.open('r', encoding='utf-8') as f:
                    self.translations[locale] = json.load(f)
                logger.info(f"Loaded translations for locale: {locale}")
            except FileNotFoundError:
                logger.error(f"Translation file not found: {file_path}")
                continue
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON in translation file {file_path}: {e}")
                continue

    async def get_user_locale(self, user_id: int) -> Optional[str]:
        """Get the user's preferred locale from a database (with caching)"""
        cached = self._user_locale_cache.get(user_id)
        if cached is not CACHE_MISS:
            return cached
        
        try:
            user_data = await self.user_model.get_user(user_id)
            locale = user_data.get('locale') if user_data else None
            self._user_locale_cache.set(user_id, locale)
            return locale
        except Exception:
            logger.exception("Error getting locale for user %s", user_id)
            return None
    
    async def get_guild_locale(self, guild_id: int) -> Optional[str]:
        """Get guild's preferred locale from a database (with caching)"""
        cached = self._guild_locale_cache.get(guild_id)
        if cached is not CACHE_MISS:
            return cached
        
        try:
            guild_data = await self.guild_model.get_guild(guild_id)
            locale = guild_data.get('locale') if guild_data else None
            self._guild_locale_cache.set(guild_id, locale)
            return locale
        except Exception:
            logger.exception("Error getting locale for guild %s", guild_id)
            return None
    
    async def set_user_locale(self, user_id: int, locale: str) -> bool:
        """Set user's preferred locale"""
        if locale not in self.supported_locales:
            return False
        
        try:
            user_data = await self.user_model.get_user(user_id)
            if not user_data:
                await self.user_model.create_user(user_id, "Unknown", locale=locale)
            else:
                await self.user_model.update_user(user_id, {'locale': locale})
            self._user_locale_cache.set(user_id, locale)
            return True
        except Exception as e:
            logger.error(f"Error setting user locale: {e}")
            return False
    
    async def set_guild_locale(self, guild_id: int, locale: str) -> bool:
        """Set guild's preferred locale"""
        if locale not in self.supported_locales:
            return False
        
        try:
            guild_data = await self.guild_model.get_guild(guild_id)
            if not guild_data:
                await self.guild_model.create_guild(guild_id, "Unknown", locale=locale)
            else:
                await self.guild_model.update_guild(guild_id, {'locale': locale})
            self._guild_locale_cache.set(guild_id, locale)
            return True
        except Exception as e:
            logger.error(f"Error setting guild locale: {e}")
            return False
    
    async def get_locale(self, ctx: Union[commands.Context, discord.Interaction, Any], static_embed: bool = False) -> str:
        """
        Get the appropriate locale based on priority:
        - For static embeds: Guild locale > Default locale
        - For regular messages: User locale > Guild locale > Default locale
        
        Supports Context (prefix commands), Interaction (slash commands), and mock contexts
        """
        if isinstance(ctx, discord.Interaction):
            user = ctx.user
            guild = ctx.guild
        elif isinstance(ctx, commands.Context):
            user = ctx.author
            guild = ctx.guild
        else:
            user = getattr(ctx, 'author', None)
            guild = getattr(ctx, 'guild', None)
        
        if static_embed:
            if guild:
                guild_locale = await self.get_guild_locale(guild.id)
                if guild_locale:
                    return guild_locale
            return self.default_locale
        else:
            if user:
                user_locale = await self.get_user_locale(user.id)
                if user_locale:
                    return user_locale

            if guild:
                guild_locale = await self.get_guild_locale(guild.id)
                if guild_locale:
                    return guild_locale

            return self.default_locale
    
    def get_text(self, key: str, locale: str = None, **kwargs) -> str:
        """
        Get translated text by the key with optional formatting
        
        Args:
            key: Translation key (e.g., 'commands.ping.response_title')
            locale: Locale to use (defaults to default_locale)
            **kwargs: Format arguments for the translation string
        """
        if locale is None:
            locale = self.default_locale

        if locale not in self.translations:
            locale = self.default_locale

        keys = key.split('.')
        value = self.translations[locale]

        try:
            for k in keys:
                value = value[k]

            if kwargs:
                try:
                    return value.format(**kwargs)
                except KeyError as e:
                    logger.warning(f"Missing key '{e}' for translation '{key}' in '{locale}'")
                    return value
                except ValueError as e:
                    logger.error(f"Formatting error for '{key}': {e}")
                    return value
            return value

        except (KeyError, TypeError):
            if locale != self.default_locale:
                return self.get_text(key, self.default_locale, **kwargs)

            logger.warning(f"Translation key not found: {key}")
            return key
    
    async def t(self, ctx: Union[commands.Context, discord.Interaction, Any], key: str, static_embed: bool = False, **kwargs) -> str:
        """
        Convenience method to get translated text with context-aware locale
        
        Supports Context (prefix commands), Interaction (slash commands), and mock contexts
        
        Args:
            ctx: Discord command context, interaction, or mock context
            key: Translation key
            static_embed: Whether this is for a static embed
            **kwargs: Format arguments
        """
        try:
            locale = await self.get_locale(ctx, static_embed)
            return self.get_text(key, locale, **kwargs)
        except Exception as e:
            logger.error(f"Error getting translation for '{key}': {e}")
            return self.get_text(key, self.default_locale, **kwargs)
    
    def clear_cache(self):
        """Clear the locale cache (useful for testing or manual refresh)"""
        self._user_locale_cache.clear()
        self._guild_locale_cache.clear()
        logger.info("Locale cache cleared")
    
    def clear_user_cache(self, user_id: int):
        """Clear cache for a specific user"""
        self._user_locale_cache.pop(user_id)
    
    def clear_guild_cache(self, guild_id: int):
        """Clear cache for a specific guild"""
        self._guild_locale_cache.pop(guild_id)

i18n = I18nManager()
