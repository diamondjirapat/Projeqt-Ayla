"""Required application services for leveling routes."""

from dataclasses import dataclass

from discord.ext import commands
from database.models import (
    LevelingModel,
)
from api.dependencies.common import (
    GuildLookup,
    MemberLookup,
)


@dataclass(frozen=True, slots=True)
class LevelingDependencies:
    _find_guild: GuildLookup
    _get_guild_member: MemberLookup
    bot: commands.Bot | None
    bot_instance: commands.Bot | None
    leveling_model: LevelingModel
