from datetime import datetime, UTC
from typing import Optional, Dict, Any

from database.connection import db_manager


class BaseModel:
    def __init__(self, collection_name: str):
        self.collection_name = collection_name

    @property
    def collection(self):
        return db_manager.get_collection(self.collection_name)


class UserModel(BaseModel):
    def __init__(self):
        super().__init__('users')
    
    async def create_user(self, user_id: int, username: str, **kwargs) -> Dict[str, Any]:
        """Create a new user"""
        user_data = {
            'user_id': user_id,
            'username': username,
            'locale': kwargs.get('locale', None),
            'created_at': datetime.now(UTC),
            'updated_at': datetime.now(UTC),
            **kwargs
        }
        
        result = await self.collection.insert_one(user_data)
        user_data['_id'] = result.inserted_id
        return user_data
    
    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        return await self.collection.find_one({'user_id': user_id})

    async def update_user(self, user_id: int, update_data: Dict[str, Any]) -> bool:
        """Update user data"""
        update_data['updated_at'] = datetime.now(UTC)
        result = await self.collection.update_one(
            {'user_id': user_id},
            {'$set': update_data}
        )
        return result.modified_count > 0

    async def update_lastfm(self, user_id: int, username: str, session_key: str):
        """Update Last.fm data"""
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
        """Create a new empty playlist"""
        key = name.lower().replace(" ", "_")
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
        key = name.lower().replace(" ", "_")
        result = await self.collection.update_one(
            {'user_id': user_id},
            {'$unset': {f'playlists.{key}': ""}}
        )
        return result.modified_count > 0
    
    async def add_track_to_playlist(self, user_id: int, playlist_name: str, track_info: Dict[str, Any]) -> bool:
        """Add a track to a playlist"""
        key = playlist_name.lower().replace(" ", "_")
        track_data = {
            'title': track_info.get('title', 'Unknown'),
            'url': track_info.get('url', ''),
            'author': track_info.get('author', 'Unknown'),
            'added_at': datetime.now(UTC)
        }
        result = await self.collection.update_one(
            {'user_id': user_id, f'playlists.{key}': {'$exists': True}},
            {'$push': {f'playlists.{key}.tracks': track_data}}
        )
        return result.modified_count > 0
    
    async def remove_track_from_playlist(self, user_id: int, playlist_name: str, index: int) -> bool:
        """Remove a track from a playlist by index"""
        key = playlist_name.lower().replace(" ", "_")
        user_data = await self.get_user(user_id)
        if not user_data or 'playlists' not in user_data:
            return False
        playlist = user_data['playlists'].get(key)
        if not playlist or 'tracks' not in playlist:
            return False
        if index < 0 or index >= len(playlist['tracks']):
            return False
        
        track_to_remove = playlist['tracks'][index]
        result = await self.collection.update_one(
            {'user_id': user_id},
            {'$pull': {f'playlists.{key}.tracks': track_to_remove}}
        )
        return result.modified_count > 0
    
    async def get_playlist(self, user_id: int, name: str) -> Optional[Dict[str, Any]]:
        """Get a specific playlist with all tracks"""
        key = name.lower().replace(" ", "_")
        user_data = await self.get_user(user_id)
        if user_data and 'playlists' in user_data:
            return user_data['playlists'].get(key)
        return None
    
    async def get_all_playlists(self, user_id: int) -> Dict[str, Dict[str, Any]]:
        """Get all playlists with their metadata (name and track count)"""
        user_data = await self.get_user(user_id)
        if user_data and 'playlists' in user_data:
            return user_data['playlists']
        return {}

    async def import_playlist(self, user_id: int, name: str, source_url: str, track_count: int) -> bool:
        """Create a new imported playlist"""
        key = name.lower().replace(" ", "_")
        result = await self.collection.update_one(
            {'user_id': user_id},
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
        key = playlist_name.lower().replace(" ", "_")
        
        playlist = await self.get_playlist(user_id, playlist_name)
        if not playlist or playlist.get('type') != 'imported':
            return False
            
        update_op = {}
        if mod_type == 'additions':
            data['added_at'] = datetime.now(UTC)
            update_op = {'$push': {f'playlists.{key}.modifications.additions': data}}
        elif mod_type == 'removals':
            update_op = {'$push': {f'playlists.{key}.modifications.removals': data}}
        else:
            return False
            
        result = await self.collection.update_one(
            {'user_id': user_id},
            update_op
        )
        return result.modified_count > 0

    async def update_playlist_reorder(self, user_id: int, playlist_name: str, track_ids: list) -> bool:
        """Update the reorder list for an imported playlist"""
        key = playlist_name.lower().replace(" ", "_")
        
        playlist = await self.get_playlist(user_id, playlist_name)
        if not playlist or playlist.get('type') != 'imported':
            return False
            
        result = await self.collection.update_one(
            {'user_id': user_id},
            {'$set': {f'playlists.{key}.modifications.reorder': track_ids}}
        )
        return result.modified_count > 0


class GuildModel(BaseModel):
    def __init__(self):
        super().__init__('guilds')
    
    async def create_guild(self, guild_id: int, name: str, **kwargs) -> Dict[str, Any]:
        """Create a new guild"""
        guild_data = {
            'guild_id': guild_id,
            'name': name,
            'locale': kwargs.get('locale', 'en'),
            'created_at': datetime.now(UTC),
            'updated_at': datetime.now(UTC),
            **kwargs
        }
        
        result = await self.collection.insert_one(guild_data)
        guild_data['_id'] = result.inserted_id
        return guild_data
    
    async def get_guild(self, guild_id: int) -> Optional[Dict[str, Any]]:
        return await self.collection.find_one({'guild_id': guild_id})

    async def update_guild(self, guild_id: int, update_data: Dict[str, Any]) -> bool:
        update_data['updated_at'] = datetime.now(UTC)
        result = await self.collection.update_one(
            {'guild_id': guild_id},
            {'$set': update_data}
        )
        return result.modified_count > 0
    
    async def create_playlist(self, guild_id: int, name: str) -> bool:
        """Create a new empty server playlist"""
        key = name.lower().replace(" ", "_")
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
        key = name.lower().replace(" ", "_")
        result = await self.collection.update_one(
            {'guild_id': guild_id},
            {'$unset': {f'playlists.{key}': ""}}
        )
        return result.modified_count > 0
    
    async def add_track_to_playlist(self, guild_id: int, playlist_name: str, track_info: Dict[str, Any]) -> bool:
        """Add a track to a server playlist"""
        key = playlist_name.lower().replace(" ", "_")
        track_data = {
            'title': track_info.get('title', 'Unknown'),
            'url': track_info.get('url', ''),
            'author': track_info.get('author', 'Unknown'),
            'added_at': datetime.now(UTC)
        }
        result = await self.collection.update_one(
            {'guild_id': guild_id, f'playlists.{key}': {'$exists': True}},
            {'$push': {f'playlists.{key}.tracks': track_data}}
        )
        return result.modified_count > 0
    
    async def remove_track_from_playlist(self, guild_id: int, playlist_name: str, index: int) -> bool:
        """Remove a track from a server playlist by index"""
        key = playlist_name.lower().replace(" ", "_")
        guild_data = await self.get_guild(guild_id)
        if not guild_data or 'playlists' not in guild_data:
            return False
        playlist = guild_data['playlists'].get(key)
        if not playlist or 'tracks' not in playlist:
            return False
        if index < 0 or index >= len(playlist['tracks']):
            return False
        
        track_to_remove = playlist['tracks'][index]
        result = await self.collection.update_one(
            {'guild_id': guild_id},
            {'$pull': {f'playlists.{key}.tracks': track_to_remove}}
        )
        return result.modified_count > 0
    
    async def get_playlist(self, guild_id: int, name: str) -> Optional[Dict[str, Any]]:
        """Get a specific server playlist with all tracks"""
        key = name.lower().replace(" ", "_")
        guild_data = await self.get_guild(guild_id)
        if guild_data and 'playlists' in guild_data:
            return guild_data['playlists'].get(key)
        return None
    
    async def get_all_playlists(self, guild_id: int) -> Dict[str, Dict[str, Any]]:
        """Get all server playlists with their metadata"""
        guild_data = await self.get_guild(guild_id)
        if guild_data and 'playlists' in guild_data:
            return guild_data['playlists']
        return {}

    async def import_playlist(self, guild_id: int, name: str, source_url: str, track_count: int) -> bool:
        """Create a new imported server playlist"""
        key = name.lower().replace(" ", "_")
        result = await self.collection.update_one(
            {'guild_id': guild_id},
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
        """Add a modification to an imported server playlist"""
        key = playlist_name.lower().replace(" ", "_")
        
        # Verify playlist is imported type
        playlist = await self.get_playlist(guild_id, playlist_name)
        if not playlist or playlist.get('type') != 'imported':
            return False
            
        update_op = {}
        if mod_type == 'additions':
            data['added_at'] = datetime.now(UTC)
            update_op = {'$push': {f'playlists.{key}.modifications.additions': data}}
        elif mod_type == 'removals':
            update_op = {'$push': {f'playlists.{key}.modifications.removals': data}}
        else:
            return False
            
        result = await self.collection.update_one(
            {'guild_id': guild_id},
            update_op
        )
        return result.modified_count > 0

    async def update_playlist_reorder(self, guild_id: int, playlist_name: str, track_ids: list) -> bool:
        """Update the reorder list for an imported server playlist"""
        key = playlist_name.lower().replace(" ", "_")
        
        playlist = await self.get_playlist(guild_id, playlist_name)
        if not playlist or playlist.get('type') != 'imported':
            return False
            
        result = await self.collection.update_one(
            {'guild_id': guild_id},
            {'$set': {f'playlists.{key}.modifications.reorder': track_ids}}
        )
        return result.modified_count > 0

    async def set_music_channel(self, guild_id: int, channel_id: int):
        """Set the static music channel ID"""
        await self.collection.update_one(
            {'guild_id': guild_id},
            {'$set': {'music.channel_id': channel_id}},
            upsert=True
        )

    async def get_music_channel(self, guild_id: int) -> Optional[int]:
        """Get the static music channel ID"""
        data = await self.collection.find_one({'guild_id': guild_id})
        if data and 'music' in data:
            return data['music'].get('channel_id')
        return None

    async def remove_music_channel(self, guild_id: int):
        """Remove the static music channel"""
        await self.collection.update_one(
            {'guild_id': guild_id},
            {'$unset': {'music': ""}}
        )

    async def set_music_message(self, guild_id: int, message_id: int):
        """Set the static music embed message ID"""
        await self.collection.update_one(
            {'guild_id': guild_id},
            {'$set': {'music.message_id': message_id}},
            upsert=True
        )

    async def get_music_message(self, guild_id: int) -> Optional[int]:
        """Get the static music embed message ID"""
        data = await self.collection.find_one({'guild_id': guild_id})
        if data and 'music' in data:
            return data['music'].get('message_id')
        return None

    async def set_default_volume(self, guild_id: int, volume: int):
        """Set the default music volume for the guild"""
        await self.collection.update_one(
            {'guild_id': guild_id},
            {'$set': {'music.default_volume': volume}},
            upsert=True
        )

    async def get_default_volume(self, guild_id: int) -> int:
        """Get the default music volume for the guild (defaults to 20)"""
        data = await self.collection.find_one({'guild_id': guild_id})
        if data and 'music' in data:
            return data['music'].get('default_volume', 20)
        return 20
