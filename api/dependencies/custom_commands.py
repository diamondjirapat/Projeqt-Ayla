"""Required application services for custom_commands routes."""

from dataclasses import dataclass

from discord.ext import commands
from database.models import (
    CustomCommandModel,
    GuildModel,
)
from api.dependencies.common import (
    PermissionCheck,
    GuildLookup,
    PreviewContext,
)


@dataclass(frozen=True, slots=True)
class CustomCommandsDependencies:
    _find_guild: GuildLookup
    _guild_preview_context: PreviewContext
    bot_instance: commands.Bot | None
    check_user_guild_permission: PermissionCheck
    custom_command_model: CustomCommandModel
    guild_model: GuildModel
