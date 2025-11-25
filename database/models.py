from datetime import datetime
from typing import Optional, Dict, Any
from database.connection import db_manager

class BaseModel:
    """Base model for database operations"""
    
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
            'locale': kwargs.get('locale', 'en'),
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow(),
            **kwargs
        }
        
        result = await self.collection.insert_one(user_data)
        user_data['_id'] = result.inserted_id
        return user_data
    
    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get user by ID"""
        return await self.collection.find_one({'user_id': user_id})
    
    async def update_user(self, user_id: int, update_data: Dict[str, Any]) -> bool:
        """Update user data"""
        update_data['updated_at'] = datetime.utcnow()
        result = await self.collection.update_one(
            {'user_id': user_id},
            {'$set': update_data}
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
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow(),
            **kwargs
        }
        
        result = await self.collection.insert_one(guild_data)
        guild_data['_id'] = result.inserted_id
        return guild_data
    
    async def get_guild(self, guild_id: int) -> Optional[Dict[str, Any]]:
        """Get guild by ID"""
        return await self.collection.find_one({'guild_id': guild_id})
    
    async def update_guild(self, guild_id: int, update_data: Dict[str, Any]) -> bool:
        """Update guild data"""
        update_data['updated_at'] = datetime.utcnow()
        result = await self.collection.update_one(
            {'guild_id': guild_id},
            {'$set': update_data}
        )
        return result.modified_count > 0