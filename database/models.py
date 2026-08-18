from datetime import datetime, UTC
from typing import Optional, Dict, Any
import logging
import re
import math

from database.connection import db_manager
from utils.artwork import normalize_saved_track

logger = logging.getLogger(__name__)


def _normalize_playlist(playlist: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not playlist:
        return playlist
    normalized = dict(playlist)
    normalized['tracks'] = [
        normalize_saved_track(track)
        for track in playlist.get('tracks', [])
        if isinstance(track, dict)
    ]
    modifications = playlist.get('modifications')
    if isinstance(modifications, dict):
        normalized_modifications = dict(modifications)
        normalized_modifications['additions'] = [
            normalize_saved_track(track)
            for track in modifications.get('additions', [])
            if isinstance(track, dict)
        ]
        normalized['modifications'] = normalized_modifications
    return normalized


def _playlist_key(name: str) -> str:
    """Sanitize a playlist name into a MongoDB-safe field key.

    MongoDB interprets dots in field names as nested-path separators and a
    leading ``$`` as an operator, so a playlist named ``"My.Mix"`` would
    otherwise be stored as ``playlists.my.mix`` and corrupt the document.
    Whitespace is collapsed to underscores and any ``.``/``$`` is replaced.
    """
    if not isinstance(name, str):
        raise TypeError("Playlist name must be a string")

    key = re.sub(r"\s+", "_", name.strip().lower())
    key = re.sub(r"[.$]", "_", key)
    if not key:
        raise ValueError("Playlist name cannot be empty")
    return key


class BaseModel:
    def __init__(self, collection_name: str):
        self.collection_name = collection_name

    @property
    def collection(self):
        return db_manager.get_collection(self.collection_name)

    async def _upsert(self, identity: Dict[str, int], update_data: Dict[str, Any]) -> bool:
        """Update a document without mutating the caller's mapping."""
        now = datetime.now(UTC)
        values = {**update_data, 'updated_at': now}
        values.pop('created_at', None)
        result = await self.collection.update_one(
            identity,
            {
                '$set': values,
                '$setOnInsert': {'created_at': now},
            },
            upsert=True,
        )
        return result.modified_count > 0 or result.upserted_id is not None


class UserModel(BaseModel):
    def __init__(self):
        super().__init__('users')

    async def create_user(self, user_id: int, username: str, **kwargs) -> Dict[str, Any]:
        """Create a new user"""
        locale = kwargs.pop('locale', None)
        user_data = {
            **kwargs,
            'user_id': user_id,
            'username': username,
            'locale': locale,
            'created_at': datetime.now(UTC),
            'updated_at': datetime.now(UTC),
        }

        result = await self.collection.insert_one(user_data)
        user_data['_id'] = result.inserted_id
        return user_data

    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        return await self.collection.find_one({'user_id': user_id})

    async def update_user(self, user_id: int, update_data: Dict[str, Any]) -> bool:
        """Update user data"""
        return await self._upsert({'user_id': user_id}, update_data)

    async def update_lastfm(self, user_id: int, username: str, session_key: str):
        """Update Last.fm data"""
        logger.info(f"[DB] Linking Last.fm account for user {user_id}: {username}")
        await self.collection.update_one(
            {'user_id': user_id},
            {'$set': {
                'lastfm': {
                    'username': username,
                    'session_key': session_key,
                    'scrobbling': True
                }
            }},
            upsert=True
        )

    async def remove_lastfm(self, user_id: int):
        """Remove Last.fm data"""
        logger.info(f"[DB] Unlinking Last.fm account for user {user_id}")
        await self.collection.update_one(
            {'user_id': user_id},
            {'$unset': {'lastfm': ""}}
        )

    async def toggle_lastfm_scrobbling(self, user_id: int, enabled: bool):
        """Toggle scrobbling status"""
        await self.collection.update_one(
            {'user_id': user_id},
            {'$set': {'lastfm.scrobbling': enabled}}
        )

    async def create_playlist(self, user_id: int, name: str) -> bool:
        """Create a new empty playlist. Returns False if it already exists."""
        key = _playlist_key(name)
        if await self.get_playlist(user_id, name):
            return False
        result = await self.collection.update_one(
            {'user_id': user_id},
            {'$set': {f'playlists.{key}': {
                'name': name,
                'created_at': datetime.now(UTC),
                'tracks': []
            }}},
            upsert=True
        )
        return result.modified_count > 0 or result.upserted_id is not None

    async def delete_playlist(self, user_id: int, name: str) -> bool:
        """Delete an entire playlist"""
        key = _playlist_key(name)
        result = await self.collection.update_one(
            {'user_id': int(user_id)},
            {'$unset': {f'playlists.{key}': ""}}
        )
        return result.modified_count > 0

    async def set_playlist_cover(self, user_id: int, name: str, cover_url: str) -> bool:
        """Set or reset custom thumbnail/cover URL for a playlist"""
        key = _playlist_key(name)
        uid = int(user_id)
        if not await self.get_playlist(uid, name):
            return False
        if cover_url:
            result = await self.collection.update_one(
                {'user_id': uid, f'playlists.{key}': {'$exists': True}},
                {'$set': {f'playlists.{key}.cover': cover_url}}
            )
        else:
            result = await self.collection.update_one(
                {'user_id': uid, f'playlists.{key}': {'$exists': True}},
                {'$unset': {f'playlists.{key}.cover': ""}}
            )
        return result.modified_count > 0 or result.matched_count > 0

    async def add_track_to_playlist(self, user_id: int, playlist_name: str, track_info: Dict[str, Any]) -> bool:
        """Add a track to a playlist (handles both regular and imported playlists)"""
        key = _playlist_key(playlist_name)
        uid = int(user_id)

        playlist = await self.get_playlist(uid, playlist_name)
        if not playlist:
            return False

        track_data = normalize_saved_track(track_info)

        if playlist.get('type') == 'imported':
            return await self.add_playlist_modification(uid, playlist_name, 'additions', track_data)

        track_data['added_at'] = datetime.now(UTC)
        result = await self.collection.update_one(
            {'user_id': uid, f'playlists.{key}': {'$exists': True}},
            {'$push': {f'playlists.{key}.tracks': track_data}}
        )
        return result.modified_count > 0

    async def remove_track_from_playlist(self, user_id: int, playlist_name: str, index: int) -> tuple[bool, Optional[Dict[str, Any]]]:
        """
        Remove a track from a playlist by index.
        Returns (success, track_info) where track_info is the removed track on success, or error info on failure.
        Handles both regular playlists (tracks array) and imported playlists (modifications.additions).
        """
        key = _playlist_key(playlist_name)
        user_data = await self.get_user(int(user_id))
        if not user_data or 'playlists' not in user_data:
            return (False, {'error': 'user_not_found'})

        playlist = user_data['playlists'].get(key)
        if not playlist:
            return (False, {'error': 'playlist_not_found'})

        is_imported = playlist.get('type') == 'imported'

        if is_imported:
            additions = playlist.get('modifications', {}).get('additions', [])
            if not additions:
                return (False, {'error': 'no_additions'})
            if index < 0 or index >= len(additions):
                return (False, {'error': 'invalid_index', 'max_index': len(additions)})

            track_to_remove = additions[index]
            await self.collection.update_one(
                {'user_id': int(user_id)},
                {'$unset': {f'playlists.{key}.modifications.additions.{index}': 1}}
            )
            result = await self.collection.update_one(
                {'user_id': int(user_id)},
                {'$pull': {f'playlists.{key}.modifications.additions': None}}
            )
            if result.modified_count > 0:
                return (True, track_to_remove)
            return (False, {'error': 'remove_failed'})
        else:
            tracks = playlist.get('tracks', [])
            if not tracks:
                return (False, {'error': 'empty_playlist'})
            if index < 0 or index >= len(tracks):
                return (False, {'error': 'invalid_index', 'max_index': len(tracks)})

            track_to_remove = tracks[index]
            await self.collection.update_one(
                {'user_id': int(user_id)},
                {'$unset': {f'playlists.{key}.tracks.{index}': 1}}
            )
            result = await self.collection.update_one(
                {'user_id': int(user_id)},
                {'$pull': {f'playlists.{key}.tracks': None}}
            )
            if result.modified_count > 0:
                return (True, track_to_remove)
            return (False, {'error': 'remove_failed'})

    async def get_playlist(self, user_id: int, name: str) -> Optional[Dict[str, Any]]:
        """Get a specific playlist with all tracks"""
        key = _playlist_key(name)
        user_data = await self.get_user(int(user_id))
        if user_data and 'playlists' in user_data:
            return _normalize_playlist(user_data['playlists'].get(key))
        return None

    async def get_all_playlists(self, user_id: int) -> Dict[str, Dict[str, Any]]:
        """Get all playlists with their metadata (name and track count)"""
        user_data = await self.get_user(int(user_id))
        if user_data and 'playlists' in user_data:
            return {
                key: _normalize_playlist(playlist)
                for key, playlist in user_data['playlists'].items()
            }
        return {}

    async def import_playlist(self, user_id: int, name: str, source_url: str, track_count: int) -> bool:
        """Create a new imported playlist. Returns False if it already exists."""
        key = _playlist_key(name)
        if await self.get_playlist(int(user_id), name):
            return False
        result = await self.collection.update_one(
            {'user_id': int(user_id)},
            {'$set': {f'playlists.{key}': {
                'name': name,
                'type': 'imported',
                'source_url': source_url,
                'source_track_count': track_count,
                'created_at': datetime.now(UTC),
                'modifications': {
                    'reorder': [],
                    'additions': [],
                    'removals': []
                }
            }}},
            upsert=True
        )
        return result.modified_count > 0 or result.upserted_id is not None

    async def add_playlist_modification(self, user_id: int, playlist_name: str, mod_type: str, data: Any) -> bool:
        """Add a modification to an imported playlist (additions or removals)"""
        key = _playlist_key(playlist_name)

        playlist = await self.get_playlist(int(user_id), playlist_name)
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
            result = await self.collection.update_one({'user_id': int(user_id)}, update_op)
            return result.modified_count > 0
        return False


class GuildModel(BaseModel):
    def __init__(self):
        super().__init__('guilds')

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
        return guild_data

    async def get_guild(self, guild_id: int) -> Optional[Dict[str, Any]]:
        return await self.collection.find_one({'guild_id': guild_id})

    async def update_guild(self, guild_id: int, update_data: Dict[str, Any]) -> bool:
        return await self._upsert({'guild_id': int(guild_id)}, update_data)

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
        return await self._upsert(
            {'guild_id': int(guild_id)},
            {'default_volume': vol, 'settings.default_volume': vol}
        )

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
        return await self._upsert(
            {'guild_id': int(guild_id)},
            {'music_channel_id': cid, 'settings.music_channel_id': cid}
        )

    async def remove_music_channel(self, guild_id: int) -> bool:
        """Remove the bound music text channel ID for a guild."""
        result = await self.collection.update_one(
            {'guild_id': int(guild_id)},
            {'$unset': {'music_channel_id': '', 'settings.music_channel_id': '', 'music_message_id': ''}}
        )
        return result.modified_count > 0

    async def get_music_message(self, guild_id: int) -> Optional[int]:
        """Get the active now-playing / control message ID for a guild."""
        guild = await self.get_guild(int(guild_id))
        if guild:
            mid = guild.get('music_message_id')
            if mid is not None:
                try:
                    return int(mid)
                except (ValueError, TypeError):
                    pass
        return None

    async def set_music_message(self, guild_id: int, message_id: int) -> bool:
        """Set the active now-playing / control message ID for a guild."""
        mid = int(message_id)
        return await self._upsert(
            {'guild_id': int(guild_id)},
            {'music_message_id': mid}
        )

    async def create_playlist(self, guild_id: int, name: str) -> bool:
        """Create a new empty server playlist. Returns False if it already exists."""
        key = _playlist_key(name)
        if await self.get_playlist(guild_id, name):
            return False
        result = await self.collection.update_one(
            {'guild_id': guild_id},
            {'$set': {f'playlists.{key}': {
                'name': name,
                'created_at': datetime.now(UTC),
                'tracks': []
            }}},
            upsert=True
        )
        return result.modified_count > 0 or result.upserted_id is not None

    async def delete_playlist(self, guild_id: int, name: str) -> bool:
        """Delete an entire server playlist"""
        key = _playlist_key(name)
        result = await self.collection.update_one(
            {'guild_id': int(guild_id)},
            {'$unset': {f'playlists.{key}': ""}}
        )
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
                {'$set': {f'playlists.{key}.cover': cover_url}}
            )
        else:
            result = await self.collection.update_one(
                {'guild_id': gid, f'playlists.{key}': {'$exists': True}},
                {'$unset': {f'playlists.{key}.cover': ""}}
            )
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
            {'guild_id': gid, f'playlists.{key}': {'$exists': True}},
            {'$push': {f'playlists.{key}.tracks': track_data}}
        )
        return result.modified_count > 0

    async def remove_track_from_playlist(self, guild_id: int, playlist_name: str, index: int) -> tuple[bool, Optional[Dict[str, Any]]]:
        """
        Remove a track from a server playlist by index.
        Returns (success, track_info) where track_info is the removed track on success, or error info on failure.
        Handles both regular playlists (tracks array) and imported playlists (modifications.additions).
        """
        key = _playlist_key(playlist_name)
        guild_data = await self.get_guild(int(guild_id))
        if not guild_data or 'playlists' not in guild_data:
            return (False, {'error': 'guild_not_found'})

        playlist = guild_data['playlists'].get(key)
        if not playlist:
            return (False, {'error': 'playlist_not_found'})

        is_imported = playlist.get('type') == 'imported'

        if is_imported:
            additions = playlist.get('modifications', {}).get('additions', [])
            if not additions:
                return (False, {'error': 'no_additions'})
            if index < 0 or index >= len(additions):
                return (False, {'error': 'invalid_index', 'max_index': len(additions)})

            track_to_remove = additions[index]
            await self.collection.update_one(
                {'guild_id': int(guild_id)},
                {'$unset': {f'playlists.{key}.modifications.additions.{index}': 1}}
            )
            result = await self.collection.update_one(
                {'guild_id': int(guild_id)},
                {'$pull': {f'playlists.{key}.modifications.additions': None}}
            )
            if result.modified_count > 0:
                return (True, track_to_remove)
            return (False, {'error': 'remove_failed'})
        else:
            tracks = playlist.get('tracks', [])
            if not tracks:
                return (False, {'error': 'empty_playlist'})
            if index < 0 or index >= len(tracks):
                return (False, {'error': 'invalid_index', 'max_index': len(tracks)})

            track_to_remove = tracks[index]
            await self.collection.update_one(
                {'guild_id': int(guild_id)},
                {'$unset': {f'playlists.{key}.tracks.{index}': 1}}
            )
            result = await self.collection.update_one(
                {'guild_id': int(guild_id)},
                {'$pull': {f'playlists.{key}.tracks': None}}
            )
            if result.modified_count > 0:
                return (True, track_to_remove)
            return (False, {'error': 'remove_failed'})

    async def get_playlist(self, guild_id: int, name: str) -> Optional[Dict[str, Any]]:
        """Get a specific server playlist with all tracks"""
        key = _playlist_key(name)
        guild_data = await self.get_guild(int(guild_id))
        if guild_data and 'playlists' in guild_data:
            return _normalize_playlist(guild_data['playlists'].get(key))
        return None

    async def get_all_playlists(self, guild_id: int) -> Dict[str, Dict[str, Any]]:
        """Get all server playlists with their metadata (name and track count)"""
        guild_data = await self.get_guild(int(guild_id))
        if guild_data and 'playlists' in guild_data:
            return {
                key: _normalize_playlist(playlist)
                for key, playlist in guild_data['playlists'].items()
            }
        return {}

    async def import_playlist(self, guild_id: int, name: str, source_url: str, track_count: int) -> bool:
        """Create a new imported server playlist. Returns False if it already exists."""
        key = _playlist_key(name)
        if await self.get_playlist(int(guild_id), name):
            return False
        result = await self.collection.update_one(
            {'guild_id': int(guild_id)},
            {'$set': {f'playlists.{key}': {
                'name': name,
                'type': 'imported',
                'source_url': source_url,
                'source_track_count': track_count,
                'created_at': datetime.now(UTC),
                'modifications': {
                    'reorder': [],
                    'additions': [],
                    'removals': []
                }
            }}},
            upsert=True
        )
        return result.modified_count > 0 or result.upserted_id is not None

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
            return result.modified_count > 0
        return False

    async def add_reaction_role(self, guild_id: int, message_id: int, emoji: str, role_id: int, channel_id: int) -> bool:
        """Add a reaction role binding"""
        binding = {
            'message_id': message_id,
            'emoji': emoji,
            'role_id': role_id,
            'channel_id': channel_id,
            'created_at': datetime.now(UTC)
        }
        result = await self.collection.update_one(
            {'guild_id': guild_id},
            {'$push': {'reaction_roles': binding}},
            upsert=True
        )
        return result.modified_count > 0 or result.upserted_id is not None

    async def remove_reaction_role(self, guild_id: int, message_id: int, emoji: str) -> bool:
        """Remove a reaction role binding"""
        result = await self.collection.update_one(
            {'guild_id': guild_id},
            {'$pull': {'reaction_roles': {'message_id': message_id, 'emoji': emoji}}}
        )
        return result.modified_count > 0

    async def get_reaction_role(self, guild_id: int, message_id: int, emoji: str) -> Optional[int]:
        """Get role_id for a reaction role binding"""
        guild = await self.get_guild(guild_id)
        if not guild or 'reaction_roles' not in guild:
            return None
        for binding in guild['reaction_roles']:
            if binding['message_id'] == message_id and binding['emoji'] == emoji:
                return binding['role_id']
        return None

    async def get_all_reaction_roles(self, guild_id: int) -> list[Dict[str, Any]]:
        """Get all reaction role bindings for a guild"""
        guild = await self.get_guild(guild_id)
        if not guild or 'reaction_roles' not in guild:
            return []
        return guild.get('reaction_roles', [])

    async def get_max_custom_commands(self, guild_id: int) -> int:
        """Get maximum custom commands limit for a guild."""
        guild = await self.get_guild(guild_id)
        if guild and 'settings' in guild:
            return guild['settings'].get('max_custom_commands', 25)
        return 25


class CustomCommandModel(BaseModel):
    def __init__(self):
        super().__init__('custom_commands')

    async def create_command(
        self,
        guild_id: int,
        name: str,
        response: str,
        description: str = "",
        is_embed: bool = False,
        embed_config: Optional[Dict[str, Any]] = None,
        created_by: Optional[int] = None,
        created_by_name: str = "",
        enabled: bool = True
    ) -> Optional[Dict[str, Any]]:
        """Create or replace a custom command for a guild."""
        name_clean = name.strip().lower()
        if not name_clean:
            return None

        now = datetime.now(UTC)
        doc_data = {
            'guild_id': guild_id,
            'name': name_clean,
            'response': response,
            'description': description,
            'is_embed': is_embed,
            'embed_config': embed_config or {},
            'enabled': enabled,
            'updated_at': now,
        }

        result = await self.collection.update_one(
            {'guild_id': guild_id, 'name': name_clean},
            {
                '$set': doc_data,
                '$setOnInsert': {
                    'created_by': created_by,
                    'created_by_name': created_by_name,
                    'use_count': 0,
                    'created_at': now,
                }
            },
            upsert=True
        )
        return await self.get_command(guild_id, name_clean)

    async def get_command(self, guild_id: int, name: str) -> Optional[Dict[str, Any]]:
        """Get a custom command by guild ID and command name."""
        name_clean = name.strip().lower()
        return await self.collection.find_one({'guild_id': guild_id, 'name': name_clean})

    async def get_guild_commands(self, guild_id: int) -> list[Dict[str, Any]]:
        """Get all custom commands for a specific guild."""
        cursor = self.collection.find({'guild_id': guild_id}).sort('name', 1)
        return await cursor.to_list(length=None)

    async def update_command(
        self,
        guild_id: int,
        name: str,
        update_data: Dict[str, Any]
    ) -> bool:
        """Update an existing custom command's fields."""
        name_clean = name.strip().lower()
        now = datetime.now(UTC)
        cleaned_data = {**update_data, 'updated_at': now}
        cleaned_data.pop('_id', None)
        cleaned_data.pop('guild_id', None)
        cleaned_data.pop('name', None)
        cleaned_data.pop('created_at', None)

        result = await self.collection.update_one(
            {'guild_id': guild_id, 'name': name_clean},
            {'$set': cleaned_data}
        )
        return result.modified_count > 0

    async def delete_command(self, guild_id: int, name: str) -> bool:
        """Delete a custom command by guild ID and command name."""
        name_clean = name.strip().lower()
        result = await self.collection.delete_one({'guild_id': guild_id, 'name': name_clean})
        return result.deleted_count > 0

    async def increment_use_count(self, guild_id: int, name: str) -> bool:
        """Increment the usage counter of a custom command."""
        name_clean = name.strip().lower()
        result = await self.collection.update_one(
            {'guild_id': guild_id, 'name': name_clean},
            {'$inc': {'use_count': 1}}
        )
        return result.modified_count > 0


def calculate_level(xp: int) -> int:
    """Calculate level from total XP using 50 * L^2 + 100 * L = XP."""
    if xp <= 0:
        return 0
    val = 1.0 + (xp / 50.0)
    return int(-1.0 + math.sqrt(val))


def xp_for_level(level: int) -> int:
    """Calculate total cumulative XP required to reach level."""
    if level <= 0:
        return 0
    return int(50 * (level ** 2) + 100 * level)


def get_level_progress(xp: int) -> Dict[str, Any]:
    """Calculate detailed level progress stats for given XP."""
    level = calculate_level(xp)
    current_level_xp = xp_for_level(level)
    next_level_xp = xp_for_level(level + 1)

    xp_in_level = xp - current_level_xp
    xp_needed = next_level_xp - current_level_xp
    progress_pct = (xp_in_level / xp_needed * 100.0) if xp_needed > 0 else 0.0
    progress_pct = min(100.0, max(0.0, progress_pct))

    return {
        "level": level,
        "xp": xp,
        "current_level_xp": current_level_xp,
        "next_level_xp": next_level_xp,
        "xp_in_level": xp_in_level,
        "xp_needed_for_next": xp_needed,
        "progress_pct": round(progress_pct, 1)
    }


class LevelingModel(BaseModel):
    def __init__(self):
        super().__init__('user_levels')

    async def add_xp(self, guild_id: int, user_id: int, username: str, avatar_url: str, xp_amount: int) -> Dict[str, Any]:
        """Add XP to user in a specific guild and check for level up."""
        existing = await self.collection.find_one({'guild_id': guild_id, 'user_id': user_id})

        now = datetime.now(UTC)
        old_xp = existing.get('xp', 0) if existing else 0
        old_level = calculate_level(old_xp)

        new_xp = old_xp + xp_amount
        new_level = calculate_level(new_xp)

        update_data = {
            'guild_id': guild_id,
            'user_id': user_id,
            'username': username,
            'avatar_url': avatar_url,
            'xp': new_xp,
            'level': new_level,
            'updated_at': now,
            'last_xp_at': now
        }

        await self.collection.update_one(
            {'guild_id': guild_id, 'user_id': user_id},
            {
                '$set': update_data,
                '$inc': {'messages_count': 1},
                '$setOnInsert': {'created_at': now}
            },
            upsert=True
        )

        leveled_up = new_level > old_level
        return {
            'leveled_up': leveled_up,
            'old_level': old_level,
            'new_level': new_level,
            'xp': new_xp,
            'guild_id': guild_id,
            'user_id': user_id,
            'username': username,
            'avatar_url': avatar_url
        }

    async def set_xp(self, guild_id: int, user_id: int, username: str, avatar_url: str, total_xp: int) -> Dict[str, Any]:
        """Set total XP for user in a guild."""
        total_xp = max(0, total_xp)
        new_level = calculate_level(total_xp)
        now = datetime.now(UTC)

        update_data = {
            'guild_id': guild_id,
            'user_id': user_id,
            'username': username,
            'avatar_url': avatar_url,
            'xp': total_xp,
            'level': new_level,
            'updated_at': now
        }

        await self.collection.update_one(
            {'guild_id': guild_id, 'user_id': user_id},
            {
                '$set': update_data,
                '$setOnInsert': {'messages_count': 0, 'created_at': now}
            },
            upsert=True
        )

        return {
            'guild_id': guild_id,
            'user_id': user_id,
            'xp': total_xp,
            'level': new_level
        }

    async def get_user_stats(self, guild_id: int, user_id: int) -> Dict[str, Any]:
        """Get comprehensive leveling stats for a user in a guild and globally."""
        guild_doc = await self.collection.find_one({'guild_id': guild_id, 'user_id': user_id})
        guild_xp = guild_doc.get('xp', 0) if guild_doc else 0
        guild_level = guild_doc.get('level', 0) if guild_doc else 0
        guild_messages = guild_doc.get('messages_count', 0) if guild_doc else 0
        username = guild_doc.get('username', 'Unknown') if guild_doc else 'Unknown'
        avatar_url = guild_doc.get('avatar_url', '') if guild_doc else ''

        guild_rank = 1
        if guild_doc and guild_xp > 0:
            higher_guild = await self.collection.count_documents({
                'guild_id': guild_id,
                'xp': {'$gt': guild_xp}
            })
            guild_rank = higher_guild + 1

        pipeline = [
            {'$match': {'user_id': user_id}},
            {
                '$group': {
                    '_id': '$user_id',
                    'global_xp': {'$sum': '$xp'},
                    'global_messages': {'$sum': '$messages_count'}
                }
            }
        ]
        agg_result = await self.collection.aggregate(pipeline).to_list(length=1)
        global_xp = agg_result[0]['global_xp'] if agg_result else 0
        global_messages = agg_result[0]['global_messages'] if agg_result else 0
        global_level = calculate_level(global_xp)

        global_rank = 1
        if global_xp > 0:
            rank_pipeline = [
                {
                    '$group': {
                        '_id': '$user_id',
                        'total_xp': {'$sum': '$xp'}
                    }
                },
                {'$match': {'total_xp': {'$gt': global_xp}}},
                {'$count': 'higher_count'}
            ]
            rank_res = await self.collection.aggregate(rank_pipeline).to_list(length=1)
            if rank_res:
                global_rank = rank_res[0]['higher_count'] + 1

        guild_progress = get_level_progress(guild_xp)
        global_progress = get_level_progress(global_xp)

        return {
            'user_id': user_id,
            'guild_id': guild_id,
            'username': username,
            'avatar_url': avatar_url,
            'guild_rank': guild_rank,
            'guild_xp': guild_xp,
            'guild_level': guild_level,
            'guild_messages': guild_messages,
            'guild_progress': guild_progress,
            'global_rank': global_rank,
            'global_xp': global_xp,
            'global_level': global_level,
            'global_messages': global_messages,
            'global_progress': global_progress
        }

    async def get_guild_leaderboard(self, guild_id: int, page: int = 1, limit: int = 10) -> Dict[str, Any]:
        """Get paginated server leaderboard for a specific guild."""
        skip = (max(1, page) - 1) * limit
        total_count = await self.collection.count_documents({'guild_id': guild_id})

        cursor = self.collection.find({'guild_id': guild_id}).sort('xp', -1).skip(skip).limit(limit)
        docs = await cursor.to_list(length=limit)

        entries = []
        for idx, doc in enumerate(docs):
            rank = skip + idx + 1
            xp = doc.get('xp', 0)
            level = doc.get('level', calculate_level(xp))
            progress = get_level_progress(xp)
            entries.append({
                'rank': rank,
                'user_id': doc.get('user_id'),
                'username': doc.get('username', 'Unknown'),
                'avatar_url': doc.get('avatar_url', ''),
                'xp': xp,
                'level': level,
                'messages_count': doc.get('messages_count', 0),
                'progress': progress
            })

        total_pages = max(1, math.ceil(total_count / limit))
        return {
            'entries': entries,
            'total': total_count,
            'page': page,
            'limit': limit,
            'total_pages': total_pages
        }

    async def get_global_leaderboard(self, page: int = 1, limit: int = 10) -> Dict[str, Any]:
        """Get paginated global leaderboard across all servers."""
        skip = (max(1, page) - 1) * limit

        pipeline = [
            {
                '$group': {
                    '_id': '$user_id',
                    'user_id': {'$first': '$user_id'},
                    'username': {'$first': '$username'},
                    'avatar_url': {'$first': '$avatar_url'},
                    'xp': {'$sum': '$xp'},
                    'messages_count': {'$sum': '$messages_count'},
                    'guilds_count': {'$sum': 1}
                }
            },
            {'$sort': {'xp': -1}},
            {
                '$facet': {
                    'metadata': [{'$count': 'total'}],
                    'data': [{'$skip': skip}, {'$limit': limit}]
                }
            }
        ]

        result = await self.collection.aggregate(pipeline).to_list(length=1)

        total_count = 0
        raw_data = []
        if result:
            meta = result[0].get('metadata', [])
            if meta:
                total_count = meta[0].get('total', 0)
            raw_data = result[0].get('data', [])

        entries = []
        for idx, doc in enumerate(raw_data):
            rank = skip + idx + 1
            xp = doc.get('xp', 0)
            level = calculate_level(xp)
            progress = get_level_progress(xp)
            entries.append({
                'rank': rank,
                'user_id': doc.get('user_id'),
                'username': doc.get('username', 'Unknown'),
                'avatar_url': doc.get('avatar_url', ''),
                'xp': xp,
                'level': level,
                'messages_count': doc.get('messages_count', 0),
                'guilds_count': doc.get('guilds_count', 1),
                'progress': progress
            })

        total_pages = max(1, math.ceil(total_count / limit))
        return {
            'entries': entries,
            'total': total_count,
            'page': page,
            'limit': limit,
            'total_pages': total_pages
        }
