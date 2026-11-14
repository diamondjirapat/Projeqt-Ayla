"""Custom Commands persistence and domain rules."""

from datetime import UTC, datetime
from typing import Optional, Dict, Any
import logging

from database.base import BaseModel

logger = logging.getLogger(__name__)


class CustomCommandModel(BaseModel):
    def __init__(self):
        super().__init__('custom_commands')

    async def create_command(
        self,
        guild_id: int,
        name: str,
        response: str,
        description: str = '',
        is_embed: bool = False,
        embed_config: Optional[Dict[str, Any]] = None,
        created_by: Optional[int] = None,
        created_by_name: str = '',
        enabled: bool = True,
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

        await self.collection.update_one(
            {'guild_id': guild_id, 'name': name_clean},
            {
                '$set': doc_data,
                '$setOnInsert': {
                    'created_by': created_by,
                    'created_by_name': created_by_name,
                    'use_count': 0,
                    'created_at': now,
                },
            },
            upsert=True,
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

    async def update_command(self, guild_id: int, name: str, update_data: Dict[str, Any]) -> bool:
        """Update an existing custom command's fields."""
        name_clean = name.strip().lower()
        now = datetime.now(UTC)
        cleaned_data = {**update_data, 'updated_at': now}
        cleaned_data.pop('_id', None)
        cleaned_data.pop('guild_id', None)
        cleaned_data.pop('name', None)
        cleaned_data.pop('created_at', None)

        result = await self.collection.update_one({'guild_id': guild_id, 'name': name_clean}, {'$set': cleaned_data})
        return result.modified_count > 0

    async def delete_command(self, guild_id: int, name: str) -> bool:
        """Delete a custom command by guild ID and command name."""
        name_clean = name.strip().lower()
        result = await self.collection.delete_one({'guild_id': guild_id, 'name': name_clean})
        return result.deleted_count > 0

    async def increment_use_count(self, guild_id: int, name: str) -> bool:
        """Increment the usage counter of a custom command."""
        name_clean = name.strip().lower()
        result = await self.collection.update_one({'guild_id': guild_id, 'name': name_clean}, {'$inc': {'use_count': 1}})
        return result.modified_count > 0
