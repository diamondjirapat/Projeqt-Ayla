"""Required application services for guild routes."""

from collections.abc import Callable
from dataclasses import dataclass

import discord
from database.models import (
    GuildModel,
)
from api.dependencies.common import (
    CapabilityCheck,
    PermissionCheck,
    GuildLookup,
    PreviewContext,
)


@dataclass(frozen=True, slots=True)
class GuildDependencies:
    _find_guild: GuildLookup
    _guild_preview_context: PreviewContext
    check_user_guild_permission: PermissionCheck
    check_user_guild_capability: CapabilityCheck
    get_all_guilds: Callable[[], list[discord.Guild]]
    guild_model: GuildModel
