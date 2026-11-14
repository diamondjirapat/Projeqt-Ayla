"""Required application services for administration routes."""

from dataclasses import dataclass

from database.models import (
    GuildModuleModel,
    GuildPermissionModel,
)
from api.dependencies.common import (
    PermissionCheck,
    CapabilityCheck,
    GuildLookup,
    ModuleHealth,
)


@dataclass(frozen=True, slots=True)
class AdministrationDependencies:
    _find_guild: GuildLookup
    _module_runtime_health: ModuleHealth
    check_user_guild_capability: CapabilityCheck
    check_user_guild_permission: PermissionCheck
    guild_module_model: GuildModuleModel
    guild_permission_model: GuildPermissionModel
