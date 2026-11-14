import json
import logging
from typing import Any, Dict, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from database.models import UserModel, GuildModel
from utils.cache import AsyncTTLCache

logger = logging.getLogger(__name__)


class I18nManager:
    def __init__(self):
        self.translations: Dict[str, Dict] = {}
        self.default_locale = 'en'
        self.supported_locales: list = []
        self.user_model = UserModel()
        self.guild_model = GuildModel()
        self._discord_translation_index: Dict[str, Dict[str, str]] = {}
        # Cache missing values too; most users do not set a custom locale.
        self._user_locale_cache: AsyncTTLCache[int, Optional[str]] = AsyncTTLCache(ttl=300, max_size=5000, name='locale.users')
        self._guild_locale_cache: AsyncTTLCache[int, Optional[str]] = AsyncTTLCache(ttl=300, max_size=5000, name='locale.guilds')
        self.load_translations()

    def _discover_locales(self) -> list:
        """Find available locales by scanning locales/*.json.

        Adding a new catalog file (e.g. locales/ja.json) automatically
        registers the locale. The default locale stays first; the rest are
        sorted for deterministic ordering.
        """
        locales_dir = Config.PROJECT_ROOT / 'locales'
        try:
            codes = sorted(p.stem for p in locales_dir.glob('*.json') if p.is_file() and p.stem)
        except OSError:
            logger.exception('Could not scan locales directory')
            codes = []
        codes = [code for code in codes if not code.startswith('.')]
        if self.default_locale in codes:
            codes.remove(self.default_locale)
        codes.insert(0, self.default_locale)
        return codes

    def load_translations(self):
        """Load all translation files discovered in locales/*.json"""
        self.supported_locales = self._discover_locales()
        self.translations = {}
        locales_dir = Config.PROJECT_ROOT / 'locales'

        for locale in self.supported_locales:
            file_path = locales_dir / f'{locale}.json'
            try:
                with file_path.open('r', encoding='utf-8') as f:
                    self.translations[locale] = json.load(f)
                logger.info(f'Loaded translations for locale: {locale}')
            except FileNotFoundError:
                logger.error(f'Translation file not found: {file_path}')
                continue
            except json.JSONDecodeError as e:
                logger.error(f'Invalid JSON in translation file {file_path}: {e}')
                continue

        self.supported_locales = [locale for locale in self.supported_locales if locale in self.translations]
        if self.translations and self.default_locale not in self.translations:
            self.default_locale = self.supported_locales[0]
            logger.warning(f'Default locale file missing; using {self.default_locale} instead')

        self._build_discord_translation_index()

    def get_language_name(self, code: str, locale: Optional[str] = None) -> str:
        """Display name for a locale code, falling back to the code itself.

        New catalogs should add a "languages.<code>" entry to every locale
        file; until then the raw code is shown instead of a lookup key.
        """
        key = f'languages.{code}'
        name = self.get_text(key, locale or self.default_locale)
        return code if name == key else name

    def get_language_choices(self) -> list:
        """Slash-command options built from discovered locales."""
        choices = []
        for code in self.supported_locales:
            native = self.get_text(f'languages.{code}', code)
            if native == f'languages.{code}':
                native = self.get_language_name(code)
            label = code if native == f'languages.{code}' else f'{native} ({code})'
            choices.append(app_commands.Choice(name=label, value=code))
        return choices

    @staticmethod
    def _flatten_strings(value: Dict[str, Any], prefix: str = '') -> Dict[str, str]:
        flattened: Dict[str, str] = {}
        for key, item in value.items():
            path = f'{prefix}.{key}' if prefix else key
            if isinstance(item, dict):
                flattened.update(I18nManager._flatten_strings(item, path))
            elif isinstance(item, str):
                flattened[path] = item
        return flattened

    @staticmethod
    def _normalize_discord_source(value: str) -> str:
        return value.strip().rstrip('.').casefold()

    def _build_discord_translation_index(self):
        """Build source-text indexes used by Discord's application-command translator."""
        default = self.translations.get(self.default_locale, {})
        default_flat = self._flatten_strings(default)
        for locale in self.supported_locales:
            target_flat = self._flatten_strings(self.translations.get(locale, {}))
            index: Dict[str, str] = {}
            for key, source in default_flat.items():
                target = target_flat.get(key)
                if not target or target == source:
                    continue
                index.setdefault(source, target)
                index.setdefault(self._normalize_discord_source(source), target)
            self._discord_translation_index[locale] = index

    def translate_discord_string(self, source: str, locale: str) -> Optional[str]:
        index = self._discord_translation_index.get(locale, {})
        return index.get(source) or index.get(self._normalize_discord_source(source))

    async def get_user_locale(self, user_id: int) -> Optional[str]:
        """Get the user's preferred locale from a database (with caching)"""

        async def load():
            user_data = await self.user_model.get_user(user_id)
            return user_data.get('locale') if user_data else None

        try:
            return await self._user_locale_cache.get_or_load(user_id, load)
        except Exception:
            logger.exception('Error getting locale for user %s', user_id)
            return None

    async def get_guild_locale(self, guild_id: int) -> Optional[str]:
        """Get guild's preferred locale from a database (with caching)."""

        async def load():
            guild_data = await self.guild_model.get_guild(guild_id)
            return guild_data.get('locale') if guild_data else None

        try:
            return await self._guild_locale_cache.get_or_load(guild_id, load)
        except Exception:
            logger.exception('Error getting locale for guild %s', guild_id)
            return None

    async def set_user_locale(self, user_id: int, locale: str) -> bool:
        """Set user's preferred locale"""
        if locale not in self.supported_locales:
            return False

        try:
            user_data = await self.user_model.get_user(user_id)
            if not user_data:
                await self.user_model.create_user(user_id, 'Unknown', locale=locale)
            else:
                await self.user_model.update_user(user_id, {'locale': locale})
            self._user_locale_cache.set(user_id, locale)
            return True
        except Exception as e:
            logger.error(f'Error setting user locale: {e}')
            return False

    async def set_guild_locale(self, guild_id: int, locale: str) -> bool:
        """Set guild's preferred locale"""
        if locale not in self.supported_locales:
            return False

        try:
            guild_data = await self.guild_model.get_guild(guild_id)
            if not guild_data:
                await self.guild_model.create_guild(guild_id, 'Unknown', locale=locale)
            else:
                await self.guild_model.update_guild(guild_id, {'locale': locale})
            self._guild_locale_cache.set(guild_id, locale)
            return True
        except Exception as e:
            logger.error(f'Error setting guild locale: {e}')
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

            logger.warning(f'Translation key not found: {key}')
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
        logger.info('Locale cache cleared')

    def clear_user_cache(self, user_id: int):
        """Clear cache for a specific user"""
        self._user_locale_cache.pop(user_id)

    def clear_guild_cache(self, guild_id: int):
        """Clear cache for a specific guild"""
        self._guild_locale_cache.pop(guild_id)


i18n = I18nManager()


class I18nTranslator(app_commands.Translator):
    """Translate Discord slash-command metadata from the shared locale catalog."""

    async def translate(
        self,
        string: app_commands.locale_str,
        locale: discord.Locale,
        context: app_commands.TranslationContext,
    ) -> Optional[str]:
        locale_code = str(locale.value).lower().replace('_', '-')
        target_locale = None
        for candidate in (locale_code, locale_code.split('-')[0]):
            if candidate in i18n.supported_locales:
                target_locale = candidate
                break
        if target_locale is None or target_locale == i18n.default_locale:
            return None
        return i18n.translate_discord_string(string.message, target_locale)
