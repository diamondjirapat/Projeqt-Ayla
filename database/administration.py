"""Administration persistence and domain rules."""

from typing import Optional, Dict, Any
import logging
from utils.permissions import CAPABILITIES

from database.base import BaseModel

logger = logging.getLogger(__name__)


class GuildModuleModel(BaseModel):
    """Persist per-guild module switches and module-specific configuration."""

    def __init__(self):
        super().__init__('guild_modules')

    async def get_module(self, guild_id: int, module_id: str) -> Optional[Dict[str, Any]]:
        return await self.collection.find_one({'guild_id': int(guild_id), 'module_id': module_id})

    async def get_guild_modules(self, guild_id: int) -> list[Dict[str, Any]]:
        cursor = self.collection.find({'guild_id': int(guild_id)}).sort('module_id', 1)
        return [module async for module in cursor]

    async def set_module(
        self,
        guild_id: int,
        module_id: str,
        enabled: bool,
        config: Optional[Dict[str, Any]] = None,
        *,
        updated_by: Optional[int] = None,
    ) -> bool:
        values: Dict[str, Any] = {'enabled': bool(enabled)}
        if config is not None:
            values['config'] = dict(config)
        if updated_by is not None:
            values['updated_by'] = int(updated_by)
        return await self._upsert({'guild_id': int(guild_id), 'module_id': module_id}, values)

    async def is_enabled(self, guild_id: int, module_id: str, default: bool = False) -> bool:
        module = await self.get_module(guild_id, module_id)
        return bool(module['enabled']) if module and 'enabled' in module else default


class GuildPermissionModel(BaseModel):
    """Persist additive capability grants for Discord roles."""

    def __init__(self):
        super().__init__('guild_permission_grants')

    async def get_guild_grants(self, guild_id: int) -> list[Dict[str, Any]]:
        cursor = self.collection.find({'guild_id': int(guild_id)}).sort('role_id', 1)
        return [grant async for grant in cursor]

    async def get_role_grants(self, guild_id: int) -> Dict[str, list[str]]:
        grants = await self.get_guild_grants(guild_id)
        return {str(item['role_id']): [capability for capability in item.get('capabilities', []) if capability in CAPABILITIES] for item in grants}

    async def set_role_grants(
        self,
        guild_id: int,
        role_id: int,
        capabilities: list[str],
        *,
        updated_by: Optional[int] = None,
    ) -> bool:
        normalized = sorted(set(capabilities))
        unknown = set(normalized) - CAPABILITIES
        if unknown:
            raise ValueError(f'Unknown capabilities: {", ".join(sorted(unknown))}')
        if not normalized:
            result = await self.collection.delete_one({'guild_id': int(guild_id), 'role_id': int(role_id)})
            return result.deleted_count > 0
        values: Dict[str, Any] = {'capabilities': normalized}
        if updated_by is not None:
            values['updated_by'] = int(updated_by)
        return await self._upsert({'guild_id': int(guild_id), 'role_id': int(role_id)}, values)
