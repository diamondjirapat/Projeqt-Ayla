"""Users persistence and domain rules."""

from datetime import UTC, datetime
from typing import Optional, Dict, Any
import logging
from pymongo.errors import DuplicateKeyError
from utils.artwork import normalize_saved_track

from database.base import BaseModel
from database.playlists import _normalize_playlist
from database.playlists import _playlist_key
from database.user_policy import user_mutation, validate_user_settings

logger = logging.getLogger(__name__)


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
        values = {key: value for key, value in update_data.items() if key not in ('user_id', '_id')}
        validate_user_settings(values)
        return await self._upsert({'user_id': user_id}, values)

    async def update_lastfm(self, user_id: int, username: str, session_key: str):
        """Update Last.fm data"""
        logger.info(f'[DB] Linking Last.fm account for user {user_id}: {username}')
        await self.collection.update_one(
            {'user_id': user_id},
            {'$set': {'lastfm': {'username': username, 'session_key': session_key, 'scrobbling': True}}},
            upsert=True,
        )

    async def remove_lastfm(self, user_id: int):
        """Remove Last.fm data"""
        logger.info(f'[DB] Unlinking Last.fm account for user {user_id}')
        await self.collection.update_one({'user_id': user_id}, {'$unset': {'lastfm': ''}})

    async def toggle_lastfm_scrobbling(self, user_id: int, enabled: bool):
        """Toggle scrobbling status"""
        await self.collection.update_one({'user_id': user_id}, {'$set': {'lastfm.scrobbling': enabled}})

    async def create_playlist(self, user_id: int, name: str) -> bool:
        """Create a new empty playlist. Returns False if it already exists."""
        return await self._create_playlist_document(user_id, name, {'tracks': []})

    async def _create_playlist_document(self, user_id: int, name: str, fields: dict) -> bool:
        key = _playlist_key(name)
        now = datetime.now(UTC)
        mutation = user_mutation(
            'createPlaylist',
            user_id=int(user_id),
            key=key,
            now=now,
            playlist={**fields, 'name': name, 'created_at': now},
        )
        try:
            result = await self.collection.update_one(mutation['filter'], mutation['update'], upsert=True)
        except DuplicateKeyError:
            # Another writer may have created this user with a different playlist.
            # Retry without upsert; an existing playlist never matches this filter.
            result = await self.collection.update_one(mutation['filter'], mutation['update'], upsert=False)
        return result.modified_count > 0 or result.upserted_id is not None

    async def create_playlist_with_tracks(self, user_id: int, name: str, tracks: list[Dict[str, Any]]) -> bool:
        """Create a new playlist pre-filled with tracks. Returns False if it already exists."""
        name = name.strip() if isinstance(name, str) else name
        normalized = [normalize_saved_track(t) for t in (tracks or []) if isinstance(t, dict)]
        for track in normalized:
            track['added_at'] = datetime.now(UTC)
        return await self._create_playlist_document(user_id, name, {'tracks': normalized})

    async def delete_playlist(self, user_id: int, name: str) -> bool:
        """Delete an entire playlist"""
        key = _playlist_key(name)
        result = await self.collection.update_one({'user_id': int(user_id)}, {'$unset': {f'playlists.{key}': ''}})
        return result.modified_count > 0

    async def set_playlist_cover(self, user_id: int, name: str, cover_url: str) -> bool:
        """Set or reset custom thumbnail/cover URL for a playlist"""
        key = _playlist_key(name)
        uid = int(user_id)
        if not await self.get_playlist(uid, name):
            return False
        if cover_url:
            result = await self.collection.update_one(
                {'user_id': uid, f'playlists.{key}': {'$exists': True}}, {'$set': {f'playlists.{key}.cover': cover_url}}
            )
        else:
            result = await self.collection.update_one(
                {'user_id': uid, f'playlists.{key}': {'$exists': True}}, {'$unset': {f'playlists.{key}.cover': ''}}
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
            {'user_id': uid, f'playlists.{key}': {'$exists': True}, f'playlists.{key}.type': {'$ne': 'imported'}},
            {'$push': {f'playlists.{key}.tracks': track_data}},
        )
        return result.modified_count > 0

    async def remove_track_from_playlist(
        self,
        user_id: int,
        playlist_name: str,
        index: int,
    ) -> tuple[bool, Optional[Dict[str, Any]]]:
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

        imported = playlist.get('type') == 'imported'
        tracks = playlist.get('modifications', {}).get('additions', []) if imported else playlist.get('tracks', [])
        if not tracks:
            return False, {'error': 'no_additions' if imported else 'empty_playlist'}
        if type(index) is not int or index < 0 or index >= len(tracks):
            return False, {'error': 'invalid_index', 'max_index': len(tracks)}
        path = f'playlists.{key}.' + ('modifications.additions' if imported else 'tracks')
        mutation = user_mutation(
            'removePlaylistTrack',
            user_id=int(user_id),
            path=path,
            before=tracks,
            after=tracks[:index] + tracks[index + 1 :],
            now=datetime.now(UTC),
        )
        result = await self.collection.update_one(mutation['filter'], mutation['update'])
        if result.modified_count > 0:
            return True, tracks[index]
        return False, {'error': 'conflict'}

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
            return {key: _normalize_playlist(playlist) for key, playlist in user_data['playlists'].items()}
        return {}

    async def import_playlist(self, user_id: int, name: str, source_url: str, track_count: int) -> bool:
        """Create a new imported playlist. Returns False if it already exists."""
        return await self._create_playlist_document(
            user_id,
            name,
            {
                'type': 'imported',
                'source_url': source_url,
                'source_track_count': track_count,
                'modifications': {'reorder': [], 'additions': [], 'removals': []},
            },
        )

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
