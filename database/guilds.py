"""Guilds persistence and domain rules."""

from datetime import UTC, datetime
from typing import Callable, Optional, Dict, Any
import logging
import re
import time
import os
from pymongo.errors import DuplicateKeyError
from database.guild_cache import (
    DEFAULT_GUILD_CACHE_MAX_SIZE,
    DEFAULT_GUILD_CACHE_TTL,
    GuildDocCache,
    normalize_guild_key,
    invalidate_guild_caches,
    scoped_guild_read,
)
from utils.artwork import normalize_saved_track
from utils.greetings import normalize_greetings
from utils.level_alert_images import normalize_level_alert_visual
from utils.trap_channel import normalize_trap_channel_config
from utils.music_embed import (
    get_default_music_embed_config,
    normalize_music_embed_config,
    get_default_music_channel_embed_config,
    normalize_music_channel_embed_config,
)

from database.base import BaseModel
from database.playlists import _normalize_playlist
from database.playlists import _playlist_key

logger = logging.getLogger(__name__)


class GuildModel(BaseModel):
    def __init__(
        self,
        *,
        cache_ttl: float = DEFAULT_GUILD_CACHE_TTL,
        max_cache_size: int = DEFAULT_GUILD_CACHE_MAX_SIZE,
        clock: Callable[[], float] = time.monotonic,
        doc_cache: Optional[GuildDocCache] = None,
        cache_enabled: Optional[bool] = None,
    ):
        super().__init__('guilds')
        if doc_cache is None:
            doc_cache = GuildDocCache(ttl=cache_ttl, max_size=max_cache_size, clock=clock)
        self._doc_cache = doc_cache
        self._cache_enabled = (
            cache_enabled if cache_enabled is not None else os.getenv('GUILD_CACHE_MODE', 'fresh') == 'ttl'
        )

    async def create_guild(self, guild_id: int, name: str, prefix: str = '!', **kwargs) -> Dict[str, Any]:
        """Create a new guild"""
        locale = kwargs.pop('locale', 'en')
        guild_data = {
            **kwargs,
            'guild_id': guild_id,
            'name': name,
            'prefix': prefix,
            'locale': locale,
            'created_at': datetime.now(UTC),
            'updated_at': datetime.now(UTC),
        }

        result = await self.collection.insert_one(guild_data)
        guild_data['_id'] = result.inserted_id
        self.invalidate_guild(guild_id)
        self._doc_cache.set(guild_data['guild_id'], guild_data)
        return guild_data

    async def _upsert(self, identity: Dict[str, int], update_data: Dict[str, Any]) -> bool:
        """Upsert into the guilds collection and drop the cached document."""
        result = await super()._upsert(identity, update_data)
        guild_id = identity.get('guild_id') if isinstance(identity, dict) else None
        if guild_id is not None:
            self.invalidate_guild(guild_id)
        return result

    def invalidate_guild(self, guild_id: int) -> None:
        """Drop the cached guild document so the next read re-fetches it."""
        invalidate_guild_caches(guild_id)

    async def _get_fresh_guild_doc(self, guild_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a guild document directly, bypassing the cache.

        Used by playlist and reaction-role methods, where index-sensitive
        read-modify-write cycles must observe the latest stored state.
        """
        return await self.collection.find_one({'guild_id': normalize_guild_key(guild_id)})

    async def get_guild(self, guild_id: int) -> Optional[Dict[str, Any]]:
        if not self._cache_enabled:
            return await scoped_guild_read(self.collection, guild_id, lambda: self._get_fresh_guild_doc(guild_id))
        return await self._doc_cache.get_or_load(guild_id, lambda: self._get_fresh_guild_doc(guild_id))

    async def update_guild(self, guild_id: int, update_data: Dict[str, Any]) -> bool:
        return await self._upsert({'guild_id': int(guild_id)}, update_data)

    async def get_greetings_config(self, guild_id: int) -> Dict[str, Dict[str, Any]]:
        """Get normalized welcome and goodbye message configuration."""
        guild = await self.get_guild(int(guild_id))
        if not guild:
            return normalize_greetings(None)

        raw_greetings = guild.get('greetings')
        if raw_greetings is None and isinstance(guild.get('settings'), dict):
            raw_greetings = guild['settings'].get('greetings')
        return normalize_greetings(raw_greetings)

    async def set_greetings_config(self, guild_id: int, greetings: Dict[str, Any]) -> bool:
        """Persist normalized welcome and goodbye message configuration."""
        normalized = normalize_greetings(greetings)
        return await self._upsert(
            {'guild_id': int(guild_id)},
            {'greetings': normalized, 'settings.greetings': normalized},
        )

    async def get_trap_channel_config(self, guild_id: int) -> Dict[str, Any]:
        """Get the normalized trap channel configuration."""
        guild = await self.get_guild(int(guild_id))
        return normalize_trap_channel_config((guild or {}).get('trap_channel'))

    async def set_trap_channel_config(self, guild_id: int, config: Dict[str, Any]) -> Dict[str, Any]:
        """Persist the trap channel configuration and return the stored, normalized copy."""
        normalized = normalize_trap_channel_config(config)
        await self._upsert({'guild_id': int(guild_id)}, {'trap_channel': normalized})
        return normalized

    async def get_default_volume(self, guild_id: int) -> int:
        """Get the default volume for a guild (1-100, default 100)."""
        guild = await self.get_guild(int(guild_id))
        if guild:
            vol = guild.get('default_volume')
            if vol is None and 'settings' in guild:
                vol = guild['settings'].get('default_volume')
            if vol is not None:
                try:
                    return max(1, min(100, int(vol)))
                except (ValueError, TypeError):
                    pass
        return 25

    async def set_default_volume(self, guild_id: int, volume: int) -> bool:
        """Set the default volume for a guild."""
        vol = max(1, min(100, int(volume)))
        return await self._upsert({'guild_id': int(guild_id)}, {'default_volume': vol, 'settings.default_volume': vol})

    async def get_music_channel(self, guild_id: int) -> Optional[int]:
        """Get the bound music text channel ID for a guild."""
        guild = await self.get_guild(int(guild_id))
        if guild:
            chan_id = guild.get('music_channel_id')
            if chan_id is None and 'settings' in guild:
                chan_id = guild['settings'].get('music_channel_id')
            if chan_id is not None:
                try:
                    return int(chan_id)
                except (ValueError, TypeError):
                    pass
        return None

    async def set_music_channel(self, guild_id: int, channel_id: int) -> bool:
        """Set the bound music text channel ID for a guild."""
        cid = int(channel_id)
        current_cid = await self.get_music_channel(guild_id)
        updates: Dict[str, Any] = {'music_channel_id': cid, 'settings.music_channel_id': cid}
        if current_cid is not None and current_cid != cid:
            updates['music_message_id'] = 0
            updates['settings.music_message_id'] = 0
        return await self._upsert({'guild_id': int(guild_id)}, updates)

    async def remove_music_channel(self, guild_id: int) -> bool:
        """Remove the bound music text channel ID for a guild."""
        result = await self.collection.update_one(
            {'guild_id': int(guild_id)},
            {
                '$unset': {
                    'music_channel_id': '',
                    'settings.music_channel_id': '',
                    'music_message_id': '',
                    'settings.music_message_id': '',
                }
            },
        )
        self.invalidate_guild(guild_id)
        return result.modified_count > 0

    async def _get_bool_setting(self, guild_id: int, field: str, aliases: tuple = ()) -> bool:
        """Read a boolean toggle from the top-level field or ``settings`` sub-dict."""
        guild = await self.get_guild(int(guild_id))
        if guild:
            val = guild.get(field)
            for alias in aliases:
                if val is None:
                    val = guild.get(alias)
            if val is None and isinstance(guild.get('settings'), dict):
                val = guild['settings'].get(field)
                for alias in aliases:
                    if val is None:
                        val = guild['settings'].get(alias)
            if val is not None:
                return bool(val)
        return False

    async def _set_bool_setting(self, guild_id: int, field: str, enabled: bool, aliases: tuple = ()) -> bool:
        """Write a boolean toggle to the top-level field and ``settings`` sub-dict."""
        is_on = bool(enabled)
        update = {field: is_on, f'settings.{field}': is_on}
        for alias in aliases:
            update[alias] = is_on
            update[f'settings.{alias}'] = is_on
        return await self._upsert({'guild_id': int(guild_id)}, update)

    async def get_autoplay_enabled(self, guild_id: int) -> bool:
        """Check if autoplay is enabled for a guild."""
        return await self._get_bool_setting(guild_id, 'autoplay')

    async def set_autoplay_enabled(self, guild_id: int, enabled: bool) -> bool:
        """Set whether autoplay is enabled for a guild."""
        return await self._set_bool_setting(guild_id, 'autoplay', enabled)

    def _read_autoplay_preset(self, guild: Optional[Dict[str, Any]]) -> str:
        """Return the stored autoplay preset id, defaulting to ``'default'``."""
        from utils.autoplay import AUTOPLAY_PRESET_CUSTOM, AUTOPLAY_PRESET_DEFAULT

        if isinstance(guild, dict):
            if guild.get('autoplay_preset') == AUTOPLAY_PRESET_CUSTOM:
                return AUTOPLAY_PRESET_CUSTOM
            settings = guild.get('settings')
            if isinstance(settings, dict) and settings.get('autoplay_preset') == AUTOPLAY_PRESET_CUSTOM:
                return AUTOPLAY_PRESET_CUSTOM
        return AUTOPLAY_PRESET_DEFAULT

    async def get_autoplay_preset(self, guild_id: int) -> str:
        """Return the guild's autoplay preset: ``'default'`` or ``'custom'``."""
        guild = await self.get_guild(int(guild_id))
        return self._read_autoplay_preset(guild)

    async def set_autoplay_preset(self, guild_id: int, preset: str) -> bool:
        """Select the guild's autoplay preset without touching the default recipe."""
        from utils.autoplay import AUTOPLAY_PRESET_CUSTOM, AUTOPLAY_PRESET_VERSION, AUTOPLAY_PRESETS

        normalized = str(preset or '').strip().lower()
        if normalized not in AUTOPLAY_PRESETS:
            raise ValueError(f'autoplay preset must be one of {list(AUTOPLAY_PRESETS)}')
        update: Dict[str, Any] = {
            'autoplay_preset': normalized,
            'settings.autoplay_preset': normalized,
        }
        if normalized == AUTOPLAY_PRESET_CUSTOM:
            custom = await self.get_autoplay_custom_config(int(guild_id)) or {}
            update['autoplay_config'] = custom
            update['settings.autoplay_config'] = custom
            update['autoplay_config_source_version'] = AUTOPLAY_PRESET_VERSION
            update['settings.autoplay_config_source_version'] = AUTOPLAY_PRESET_VERSION
        return await self._upsert({'guild_id': int(guild_id)}, update)

    def _read_autoplay_custom(self, guild: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Return the stored custom autoplay config, or None when absent."""
        if isinstance(guild, dict):
            raw = guild.get('autoplay_config')
            if raw is None and isinstance(guild.get('settings'), dict):
                raw = guild['settings'].get('autoplay_config')
            if isinstance(raw, dict) and raw:
                from utils.autoplay import normalize_autoplay_config

                return normalize_autoplay_config(raw)
        return None

    async def get_autoplay_custom_config(self, guild_id: int) -> Optional[Dict[str, Any]]:
        """Return the guild's normalized custom autoplay config, if one is stored."""
        guild = await self.get_guild(int(guild_id))
        return self._read_autoplay_custom(guild)

    async def get_autoplay_settings(self, guild_id: int) -> Dict[str, Any]:
        """Return the full autoplay preference state for a guild."""
        from utils.autoplay import AUTOPLAY_PRESET_CUSTOM, AUTOPLAY_PRESET_VERSION, default_autoplay_config

        guild = await self.get_guild(int(guild_id))
        preset = self._read_autoplay_preset(guild)
        custom = self._read_autoplay_custom(guild)
        source_version = None
        if isinstance(guild, dict):
            source_version = guild.get('autoplay_config_source_version')
            if source_version is None and isinstance(guild.get('settings'), dict):
                source_version = guild['settings'].get('autoplay_config_source_version')
            if source_version is not None:
                try:
                    source_version = int(source_version)
                except (TypeError, ValueError):
                    source_version = None
        default = default_autoplay_config()
        if preset == AUTOPLAY_PRESET_CUSTOM and custom is not None:
            from utils.autoplay import effective_autoplay_config

            config = effective_autoplay_config(custom)
        else:
            config = default_autoplay_config()
        return {
            'preset': preset,
            'config': config,
            'custom': custom,
            'default': default,
            'default_version': AUTOPLAY_PRESET_VERSION,
            'custom_source_version': source_version,
        }

    async def set_autoplay_custom_config(self, guild_id: int, config: Dict[str, Any]) -> Dict[str, Any]:
        """Store a custom autoplay recipe and select the ``custom`` preset."""
        from utils.autoplay import AUTOPLAY_PRESET_CUSTOM, AUTOPLAY_PRESET_VERSION, normalize_autoplay_config

        normalized = normalize_autoplay_config(config)
        await self._upsert(
            {'guild_id': int(guild_id)},
            {
                'autoplay_preset': AUTOPLAY_PRESET_CUSTOM,
                'settings.autoplay_preset': AUTOPLAY_PRESET_CUSTOM,
                'autoplay_config': normalized,
                'settings.autoplay_config': normalized,
                'autoplay_config_source_version': AUTOPLAY_PRESET_VERSION,
                'settings.autoplay_config_source_version': AUTOPLAY_PRESET_VERSION,
            },
        )
        return normalized

    async def get_247_enabled(self, guild_id: int) -> bool:
        """Check if 24/7 playback is enabled for a guild."""
        return await self._get_bool_setting(guild_id, 'twenty_four_seven', aliases=('247',))

    async def set_247_enabled(self, guild_id: int, enabled: bool) -> bool:
        """Set whether 24/7 playback is enabled for a guild."""
        return await self._set_bool_setting(guild_id, 'twenty_four_seven', enabled, aliases=('247',))

    async def get_level_channel(self, guild_id: int) -> Optional[int]:
        """Get the bound leveling alert text channel ID for a guild."""
        guild = await self.get_guild(int(guild_id))
        if guild:
            chan_id = guild.get('level_channel_id')
            if chan_id is None and 'settings' in guild:
                chan_id = guild['settings'].get('level_channel_id')
            if chan_id is not None:
                try:
                    return int(chan_id)
                except (ValueError, TypeError):
                    pass
        return None

    async def set_level_channel(self, guild_id: int, channel_id: int) -> bool:
        """Set the bound leveling alert text channel ID for a guild."""
        cid = int(channel_id)
        return await self._upsert(
            {'guild_id': int(guild_id)}, {'level_channel_id': cid, 'settings.level_channel_id': cid}
        )

    async def remove_level_channel(self, guild_id: int) -> bool:
        """Remove the bound leveling alert text channel ID for a guild."""
        result = await self.collection.update_one(
            {'guild_id': int(guild_id)},
            {
                '$unset': {
                    'level_channel_id': '',
                    'settings.level_channel_id': '',
                    'level_alert_config.channel_id': '',
                    'settings.level_alert_config.channel_id': '',
                }
            },
        )
        self.invalidate_guild(guild_id)
        return result.modified_count > 0

    async def is_leveling_enabled(self, guild_id: int) -> bool:
        """Check if the leveling system is enabled for a guild (default: False)."""
        guild = await self.get_guild(int(guild_id))
        if not guild:
            return False
        enabled = guild.get('leveling_enabled')
        if enabled is None and 'settings' in guild:
            enabled = guild['settings'].get('leveling_enabled')
        return bool(enabled) if enabled is not None else False

    async def set_leveling_enabled(self, guild_id: int, enabled: bool) -> bool:
        """Enable or disable the leveling system for a guild."""
        is_on = bool(enabled)
        return await self._upsert(
            {'guild_id': int(guild_id)}, {'leveling_enabled': is_on, 'settings.leveling_enabled': is_on}
        )

    async def get_level_alert_config(self, guild_id: int) -> Dict[str, Any]:
        """Get the customizable level-up alert configuration for a guild."""
        defaults: Dict[str, Any] = {
            'leveling_enabled': False,
            'enabled': False,
            'channel_id': None,
            'title': '',
            'description': '',
            'color': '#f1c40f',
            'image_url': '',
            'thumbnail_url': '{avatar}',
            'footer_text': '{server}',
            'footer_url': '{server_icon}',
            'show_xp_field': True,
            'mode': 'embed',
            'image_card': {},
        }
        default_visual = normalize_level_alert_visual(defaults)
        defaults['mode'] = default_visual['mode']
        defaults['image_card'] = default_visual['image_card']
        guild = await self.get_guild(int(guild_id))
        if not guild:
            return defaults

        raw_cfg = guild.get('level_alert_config')
        if raw_cfg is None and 'settings' in guild:
            raw_cfg = guild['settings'].get('level_alert_config')

        cfg = dict(defaults)
        if isinstance(raw_cfg, dict):
            cfg.update(raw_cfg)

        cfg['leveling_enabled'] = await self.is_leveling_enabled(guild_id)
        visual = normalize_level_alert_visual(cfg)
        cfg['mode'] = visual['mode']
        cfg['image_card'] = visual['image_card']

        if cfg.get('channel_id') is None:
            top_chan = guild.get('level_channel_id')
            if top_chan is None and 'settings' in guild:
                top_chan = guild['settings'].get('level_channel_id')
            if top_chan is not None:
                try:
                    cfg['channel_id'] = int(top_chan)
                except (ValueError, TypeError):
                    pass

        return cfg

    async def set_level_alert_config(self, guild_id: int, config: Dict[str, Any]) -> bool:
        """Set the customizable level-up alert configuration for a guild."""
        gid = int(guild_id)
        current = await self.get_level_alert_config(gid)
        current.update(config)

        if 'leveling_enabled' in config:
            await self.set_leveling_enabled(gid, bool(config['leveling_enabled']))
            current['leveling_enabled'] = bool(config['leveling_enabled'])

        chan_id = current.get('channel_id')
        if chan_id is not None and chan_id != '' and str(chan_id).lower() not in ('null', 'none'):
            try:
                chan_id = int(chan_id)
            except (ValueError, TypeError):
                chan_id = None
        else:
            chan_id = None
        current['channel_id'] = chan_id

        current['enabled'] = bool(current.get('enabled', False))
        current['show_xp_field'] = bool(current.get('show_xp_field', True))

        # These fields are rendered as text (Discord embeds / web preview);
        # never allow raw HTML to be stored, so the dashboard cannot become
        # a stored-XSS vector.
        html_tag_pattern = re.compile(r'<[^>]*>')
        for str_key in ('title', 'description', 'color', 'image_url', 'thumbnail_url', 'footer_text', 'footer_url'):
            current[str_key] = html_tag_pattern.sub('', str(current.get(str_key) or '')).strip()

        if not current['color']:
            current['color'] = '#f1c40f'

        visual = normalize_level_alert_visual(current)
        current['mode'] = visual['mode']
        current['image_card'] = visual['image_card']

        updates: Dict[str, Any] = {
            'level_alert_config': current,
            'settings.level_alert_config': current,
        }
        if chan_id is not None:
            updates['level_channel_id'] = chan_id
            updates['settings.level_channel_id'] = chan_id

        res = await self._upsert({'guild_id': gid}, updates)
        if chan_id is None:
            await self.collection.update_one(
                {'guild_id': gid}, {'$unset': {'level_channel_id': '', 'settings.level_channel_id': ''}}
            )
            self.invalidate_guild(gid)
        return res

    async def get_music_message(self, guild_id: int) -> Optional[int]:
        """Get the active now-playing / control message ID for a guild."""
        guild = await self.get_guild(int(guild_id))
        if guild:
            mid = guild.get('music_message_id')
            if mid is None and isinstance(guild.get('settings'), dict):
                mid = guild['settings'].get('music_message_id')
            if mid is not None:
                try:
                    val = int(mid)
                    return val if val > 0 else None
                except (ValueError, TypeError):
                    pass
        return None

    async def set_music_message(self, guild_id: int, message_id: int) -> bool:
        """Set the active now-playing / control message ID for a guild."""
        mid = int(message_id)
        return await self._upsert(
            {'guild_id': int(guild_id)},
            {'music_message_id': mid, 'settings.music_message_id': mid},
        )

    async def get_voice_music_message(self, guild_id: int, bot_id: int) -> Optional[Dict[str, int]]:
        """Read this bot's voice-chat player without sharing another bot's message ID."""
        guild = await self.get_guild(int(guild_id))
        bindings = (guild or {}).get('voice_music_messages')
        entry = bindings.get(str(int(bot_id))) if isinstance(bindings, dict) else None
        if isinstance(entry, dict):
            try:
                channel_id, message_id = int(entry['channel_id']), int(entry['message_id'])
                if channel_id > 0 and message_id > 0:
                    return {'channel_id': channel_id, 'message_id': message_id}
            except (KeyError, TypeError, ValueError):
                pass
        return None

    async def set_voice_music_message(self, guild_id: int, bot_id: int, channel_id: int, message_id: int) -> bool:
        return await self._upsert(
            {'guild_id': int(guild_id)},
            {f'voice_music_messages.{int(bot_id)}': {'channel_id': int(channel_id), 'message_id': int(message_id)}},
        )

    async def clear_voice_music_message(self, guild_id: int, bot_id: int, message_id: int) -> bool:
        """Only clear the binding that was deleted, preserving a newer session's message."""
        field = f'voice_music_messages.{int(bot_id)}'
        result = await self.collection.update_one(
            {'guild_id': int(guild_id), f'{field}.message_id': int(message_id)},
            {'$unset': {field: ''}},
        )
        self.invalidate_guild(guild_id)
        return result.modified_count > 0

    async def get_music_embed_config(self, guild_id: int) -> Dict[str, Any]:
        """Get custom music embed appearance configuration for a guild."""
        guild = await self.get_guild(int(guild_id))
        if guild:
            cfg = guild.get('music_embed')
            if cfg is None and isinstance(guild.get('settings'), dict):
                cfg = guild['settings'].get('music_embed')
            if isinstance(cfg, dict):
                return normalize_music_embed_config(cfg)
        return get_default_music_embed_config()

    async def set_music_embed_config(self, guild_id: int, config: Dict[str, Any]) -> bool:
        """Set custom music embed appearance configuration for a guild."""
        normalized = normalize_music_embed_config(config)
        return await self._upsert(
            {'guild_id': int(guild_id)},
            {'music_embed': normalized, 'settings.music_embed': normalized},
        )

    async def reset_music_embed_config(self, guild_id: int) -> bool:
        """Reset custom music embed appearance configuration to defaults."""
        result = await self.collection.update_one(
            {'guild_id': int(guild_id)},
            {'$unset': {'music_embed': '', 'settings.music_embed': ''}},
        )
        self.invalidate_guild(guild_id)
        return result.modified_count > 0

    async def get_music_channel_embed_config(self, guild_id: int) -> Dict[str, Any]:
        guild = await self.get_guild(int(guild_id))
        if guild:
            return normalize_music_channel_embed_config(guild.get('music_channel_embed'))
        return get_default_music_channel_embed_config()

    async def set_music_channel_embed_config(self, guild_id: int, config: Dict[str, Any]) -> bool:
        return await self._upsert(
            {'guild_id': int(guild_id)},
            {'music_channel_embed': normalize_music_channel_embed_config(config)},
        )

    async def reset_music_channel_embed_config(self, guild_id: int) -> bool:
        result = await self.collection.update_one({'guild_id': int(guild_id)}, {'$unset': {'music_channel_embed': ''}})
        self.invalidate_guild(guild_id)
        return result.modified_count > 0

    async def create_playlist(self, guild_id: int, name: str) -> bool:
        """Create a new empty server playlist. Returns False if it already exists."""
        return await self._create_playlist_document(guild_id, name, {'tracks': []})

    async def _create_playlist_document(self, guild_id: int, name: str, fields: dict) -> bool:
        key = _playlist_key(name)
        gid = int(guild_id)
        query = {'guild_id': gid, f'playlists.{key}': {'$exists': False}}
        update = {'$set': {f'playlists.{key}': {**fields, 'name': name, 'created_at': datetime.now(UTC)}}}
        try:
            result = await self.collection.update_one(query, update, upsert=True)
        except DuplicateKeyError:
            result = await self.collection.update_one(query, update, upsert=False)
        self.invalidate_guild(guild_id)
        return result.modified_count > 0 or result.upserted_id is not None

    async def delete_playlist(self, guild_id: int, name: str) -> bool:
        """Delete an entire server playlist"""
        key = _playlist_key(name)
        result = await self.collection.update_one({'guild_id': int(guild_id)}, {'$unset': {f'playlists.{key}': ''}})
        self.invalidate_guild(guild_id)
        return result.modified_count > 0

    async def set_playlist_cover(self, guild_id: int, name: str, cover_url: str) -> bool:
        """Set or reset custom thumbnail/cover URL for a server playlist"""
        key = _playlist_key(name)
        gid = int(guild_id)
        if not await self.get_playlist(gid, name):
            return False
        if cover_url:
            result = await self.collection.update_one(
                {'guild_id': gid, f'playlists.{key}': {'$exists': True}},
                {'$set': {f'playlists.{key}.cover': cover_url}},
            )
        else:
            result = await self.collection.update_one(
                {'guild_id': gid, f'playlists.{key}': {'$exists': True}}, {'$unset': {f'playlists.{key}.cover': ''}}
            )
        self.invalidate_guild(gid)
        return result.modified_count > 0 or result.matched_count > 0

    async def add_track_to_playlist(self, guild_id: int, playlist_name: str, track_info: Dict[str, Any]) -> bool:
        """Add a track to a server playlist (handles both regular and imported playlists)"""
        key = _playlist_key(playlist_name)
        gid = int(guild_id)

        playlist = await self.get_playlist(gid, playlist_name)
        if not playlist:
            return False

        track_data = normalize_saved_track(track_info)

        if playlist.get('type') == 'imported':
            return await self.add_playlist_modification(gid, playlist_name, 'additions', track_data)

        track_data['added_at'] = datetime.now(UTC)
        result = await self.collection.update_one(
            {'guild_id': gid, f'playlists.{key}': {'$exists': True}}, {'$push': {f'playlists.{key}.tracks': track_data}}
        )
        self.invalidate_guild(gid)
        return result.modified_count > 0

    async def remove_track_from_playlist(
        self,
        guild_id: int,
        playlist_name: str,
        index: int,
    ) -> tuple[bool, Optional[Dict[str, Any]]]:
        """
        Remove a track from a server playlist by index.
        Returns (success, track_info) where track_info is the removed track on success, or error info on failure.
        Handles both regular playlists (tracks array) and imported playlists (modifications.additions).
        """
        key = _playlist_key(playlist_name)
        guild_data = await self._get_fresh_guild_doc(int(guild_id))
        if not guild_data or 'playlists' not in guild_data:
            return (False, {'error': 'guild_not_found'})

        playlist = guild_data['playlists'].get(key)
        if not playlist:
            return (False, {'error': 'playlist_not_found'})

        imported = playlist.get('type') == 'imported'
        tracks = playlist.get('modifications', {}).get('additions', []) if imported else playlist.get('tracks', [])
        if not tracks:
            return False, {'error': 'no_additions' if imported else 'empty_playlist'}
        if type(index) is not int or index < 0 or index >= len(tracks):
            return False, {'error': 'invalid_index', 'max_index': len(tracks)}
        path = f'playlists.{key}.' + ('modifications.additions' if imported else 'tracks')
        result = await self.collection.update_one(
            {'guild_id': int(guild_id), path: tracks},
            {'$set': {path: tracks[:index] + tracks[index + 1 :]}},
        )
        self.invalidate_guild(guild_id)
        if result.modified_count > 0:
            return True, tracks[index]
        return False, {'error': 'conflict'}

    async def get_playlist(self, guild_id: int, name: str) -> Optional[Dict[str, Any]]:
        """Get a specific server playlist with all tracks"""
        key = _playlist_key(name)
        guild_data = await self._get_fresh_guild_doc(int(guild_id))
        if guild_data and 'playlists' in guild_data:
            return _normalize_playlist(guild_data['playlists'].get(key))
        return None

    async def get_all_playlists(self, guild_id: int) -> Dict[str, Dict[str, Any]]:
        """Get all server playlists with their metadata (name and track count)"""
        guild_data = await self._get_fresh_guild_doc(int(guild_id))
        if guild_data and 'playlists' in guild_data:
            return {key: _normalize_playlist(playlist) for key, playlist in guild_data['playlists'].items()}
        return {}

    async def import_playlist(self, guild_id: int, name: str, source_url: str, track_count: int) -> bool:
        """Create a new imported server playlist. Returns False if it already exists."""
        return await self._create_playlist_document(
            guild_id,
            name,
            {
                'type': 'imported',
                'source_url': source_url,
                'source_track_count': track_count,
                'modifications': {'reorder': [], 'additions': [], 'removals': []},
            },
        )

    async def add_playlist_modification(self, guild_id: int, playlist_name: str, mod_type: str, data: Any) -> bool:
        """Add a modification to an imported server playlist (additions or removals)"""
        key = _playlist_key(playlist_name)

        playlist = await self.get_playlist(int(guild_id), playlist_name)
        if not playlist or playlist.get('type') != 'imported':
            return False

        update_op = {}
        if mod_type == 'additions':
            data['added_at'] = datetime.now(UTC)
            update_op = {'$push': {f'playlists.{key}.modifications.additions': data}}
        elif mod_type == 'removals':
            update_op = {'$addToSet': {f'playlists.{key}.modifications.removals': data}}
        elif mod_type == 'reorder':
            update_op = {'$set': {f'playlists.{key}.modifications.reorder': data}}

        if update_op:
            result = await self.collection.update_one({'guild_id': int(guild_id)}, update_op)
            self.invalidate_guild(guild_id)
            return result.modified_count > 0
        return False

    async def add_reaction_role(
        self,
        guild_id: int,
        message_id: int,
        emoji: str,
        role_id: int,
        channel_id: int,
    ) -> bool:
        """Add a reaction role binding."""
        binding = {
            'message_id': message_id,
            'emoji': emoji,
            'role_id': role_id,
            'channel_id': channel_id,
            'created_at': datetime.now(UTC),
        }
        result = await self.collection.update_one(
            {'guild_id': guild_id},
            {'$push': {'reaction_roles': binding}},
            upsert=True,
        )
        self.invalidate_guild(guild_id)
        return result.modified_count > 0 or result.upserted_id is not None

    async def remove_reaction_role(self, guild_id: int, message_id: int, emoji: str) -> bool:
        """Remove a reaction role binding."""
        result = await self.collection.update_one(
            {'guild_id': guild_id},
            {'$pull': {'reaction_roles': {'message_id': message_id, 'emoji': emoji}}},
        )
        self.invalidate_guild(guild_id)
        return result.modified_count > 0

    async def get_reaction_role(self, guild_id: int, message_id: int, emoji: str) -> Optional[int]:
        """Get the role ID for a reaction role binding."""
        guild = await self._get_fresh_guild_doc(guild_id)
        if not guild or 'reaction_roles' not in guild:
            return None
        for binding in guild['reaction_roles']:
            if binding['message_id'] == message_id and binding['emoji'] == emoji:
                return binding['role_id']
        return None

    async def get_all_reaction_roles(self, guild_id: int) -> list[Dict[str, Any]]:
        """Get all reaction role bindings for a guild."""
        guild = await self._get_fresh_guild_doc(guild_id)
        if not guild or 'reaction_roles' not in guild:
            return []
        return guild.get('reaction_roles', [])

    async def get_max_custom_commands(self, guild_id: int) -> int:
        """Get maximum custom commands limit for a guild."""
        guild = await self.get_guild(guild_id)
        if guild and 'settings' in guild:
            return guild['settings'].get('max_custom_commands', 25)
        return 25
