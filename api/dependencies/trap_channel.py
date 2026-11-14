"""Required application services for trap channel routes."""

from dataclasses import dataclass

from database.models import GuildModel
from api.dependencies.common import GuildLookup, PermissionCheck, PreviewContext


@dataclass(frozen=True, slots=True)
class TrapChannelDependencies:
    _find_guild: GuildLookup
    _guild_preview_context: PreviewContext
    check_user_guild_permission: PermissionCheck
    guild_model: GuildModel
