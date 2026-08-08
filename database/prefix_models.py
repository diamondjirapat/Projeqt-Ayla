import logging
from datetime import UTC, datetime
from typing import Any, Optional

from database.connection import db_manager

logger = logging.getLogger(__name__)


class PrefixModel:
    """Shared persistence operations for user and guild prefixes."""

    def __init__(self, collection_name: str, identity_field: str):
        self.collection_name = collection_name
        self.identity_field = identity_field

    @property
    def collection(self):
        return db_manager.get_collection(self.collection_name)

    async def _set_prefix(self, identity: int, prefix: str) -> bool:
        try:
            await self.collection.update_one(
                {self.identity_field: identity},
                {
                    '$set': {
                        self.identity_field: identity,
                        'prefix': prefix,
                        'updated_at': datetime.now(UTC),
                    }
                },
                upsert=True,
            )
            return True
        except Exception:
            logger.exception(
                "Failed to set prefix in %s for %s=%s",
                self.collection_name,
                self.identity_field,
                identity,
            )
            return False

    async def _get_prefix(self, identity: int) -> Optional[str]:
        try:
            result = await self.collection.find_one(
                {self.identity_field: identity},
                {'prefix': 1},
            )
            return result.get('prefix') if result else None
        except Exception:
            logger.exception(
                "Failed to get prefix from %s for %s=%s",
                self.collection_name,
                self.identity_field,
                identity,
            )
            return None

    async def _remove_prefix(self, identity: int) -> bool:
        try:
            result = await self.collection.delete_one({self.identity_field: identity})
            return result.deleted_count > 0
        except Exception:
            logger.exception(
                "Failed to remove prefix from %s for %s=%s",
                self.collection_name,
                self.identity_field,
                identity,
            )
            return False

    async def _find_by_prefix(self, prefix: str) -> list[dict[str, Any]]:
        try:
            cursor = self.collection.find({'prefix': prefix})
            return await cursor.to_list(length=None)
        except Exception:
            logger.exception(
                "Failed to list documents from %s for prefix=%r",
                self.collection_name,
                prefix,
            )
            return []


class UserPrefixModel(PrefixModel):
    def __init__(self):
        super().__init__('user_prefixes', 'user_id')

    async def set_user_prefix(self, user_id: int, prefix: str) -> bool:
        return await self._set_prefix(user_id, prefix)

    async def get_user_prefix(self, user_id: int) -> Optional[str]:
        return await self._get_prefix(user_id)

    async def remove_user_prefix(self, user_id: int) -> bool:
        return await self._remove_prefix(user_id)

    async def get_users_with_prefix(self, prefix: str) -> list[dict[str, Any]]:
        return await self._find_by_prefix(prefix)


class GuildPrefixModel(PrefixModel):
    def __init__(self):
        super().__init__('guild_prefixes', 'guild_id')

    async def set_guild_prefix(self, guild_id: int, prefix: str) -> bool:
        return await self._set_prefix(guild_id, prefix)

    async def get_guild_prefix(self, guild_id: int) -> Optional[str]:
        return await self._get_prefix(guild_id)

    async def remove_guild_prefix(self, guild_id: int) -> bool:
        return await self._remove_prefix(guild_id)

    async def get_guilds_with_prefix(self, prefix: str) -> list[dict[str, Any]]:
        return await self._find_by_prefix(prefix)
