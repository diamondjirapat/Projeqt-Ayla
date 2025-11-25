from datetime import datetime
from typing import Optional, Dict, Any, List
from database.connection import db_manager

class PrefixModel:
    """Base model for prefix-related database operations"""
    
    def __init__(self, collection_name: str):
        self.collection_name = collection_name
    
    @property
    def collection(self):
        return db_manager.get_collection(self.collection_name)

class UserPrefixModel(PrefixModel):
    def __init__(self):
        super().__init__('user_prefixes')
    
    async def set_user_prefix(self, user_id: int, prefix: str) -> bool:
        """Set a user's personal prefix"""
        try:
            await self.collection.update_one(
                {'user_id': user_id},
                {
                    '$set': {
                        'user_id': user_id,
                        'prefix': prefix,
                        'updated_at': datetime.utcnow()
                    }
                },
                upsert=True
            )
            return True
        except Exception:
            return False
    
    async def get_user_prefix(self, user_id: int) -> Optional[str]:
        """Get user's personal prefix"""
        result = await self.collection.find_one({'user_id': user_id})
        return result['prefix'] if result else None
    
    async def remove_user_prefix(self, user_id: int) -> bool:
        """Remove user's personal prefix"""
        try:
            result = await self.collection.delete_one({'user_id': user_id})
            return result.deleted_count > 0
        except Exception:
            return False
    
    async def get_users_with_prefix(self, prefix: str) -> List[Dict[str, Any]]:
        """Get all users using a specific prefix"""
        cursor = self.collection.find({'prefix': prefix})
        return await cursor.to_list(length=None)

class GuildPrefixModel(PrefixModel):
    def __init__(self):
        super().__init__('guild_prefixes')
    
    async def set_guild_prefix(self, guild_id: int, prefix: str) -> bool:
        """Set a guild's default prefix"""
        try:
            await self.collection.update_one(
                {'guild_id': guild_id},
                {
                    '$set': {
                        'guild_id': guild_id,
                        'prefix': prefix,
                        'updated_at': datetime.utcnow()
                    }
                },
                upsert=True
            )
            return True
        except Exception:
            return False
    
    async def get_guild_prefix(self, guild_id: int) -> Optional[str]:
        """Get guild's default prefix"""
        result = await self.collection.find_one({'guild_id': guild_id})
        return result['prefix'] if result else None
    
    async def remove_guild_prefix(self, guild_id: int) -> bool:
        """Remove guild's custom prefix (revert to default)"""
        try:
            result = await self.collection.delete_one({'guild_id': guild_id})
            return result.deleted_count > 0
        except Exception:
            return False
    
    async def get_guilds_with_prefix(self, prefix: str) -> List[Dict[str, Any]]:
        """Get all guilds using a specific prefix"""
        cursor = self.collection.find({'prefix': prefix})
        return await cursor.to_list(length=None)