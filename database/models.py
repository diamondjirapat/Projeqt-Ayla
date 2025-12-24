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
            'updated_at': datetime.now(UTC)
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

    async def add_playlist(self, user_id: int, name: str, url: str):
        """Add a personal playlist bookmark"""
        await self.collection.update_one(
            {'user_id': user_id},
            {'$set': {f'playlists.{name}': url}},
            upsert=True
        )

    async def remove_playlist(self, user_id: int, name: str):
        await self.collection.update_one(
            {'user_id': user_id},
            {'$unset': {f'playlists.{name}': ""}}
        )


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
            'updated_at': datetime.now(UTC)
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

    async def add_playlist(self, guild_id: int, name: str, url: str):
        """Add a server playlist bookmark"""
        await self.collection.update_one(
            {'guild_id': guild_id},
            {'$set': {f'playlists.{name}': url}},
            upsert=True
        )

    async def remove_playlist(self, guild_id: int, name: str):
        await self.collection.update_one(
            {'guild_id': guild_id},
            {'$unset': {f'playlists.{name}': ""}}
        )