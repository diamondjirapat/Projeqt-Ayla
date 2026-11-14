"""Required application services for scheduler routes."""

from dataclasses import dataclass

from discord.ext import commands
from database.scheduler_models import ScheduledItemModel
from api.dependencies.common import (
    PermissionCheck,
    GuildLookup,
    MemberLookup,
)


@dataclass(frozen=True, slots=True)
class SchedulerDependencies:
    _find_guild: GuildLookup
    _get_guild_member: MemberLookup
    bot_instance: commands.Bot | None
    check_user_guild_permission: PermissionCheck
    scheduler_model: ScheduledItemModel
