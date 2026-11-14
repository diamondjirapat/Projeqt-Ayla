"""Required application services for music_appearance routes."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from discord.ext import commands
from database.models import (
    GuildModel,
)
from api.dependencies.common import (
    GuildLookup,
    GuildPermission,
)


@dataclass(frozen=True, slots=True)
class MusicAppearanceDependencies:
    _find_guild: GuildLookup
    _resolve_music_banner_url: Callable[[], Awaitable[str]]
    _resolve_music_bar_url: Callable[[], Awaitable[str]]
    bot: commands.Bot | None
    bot_instance: commands.Bot | None
    guild_model: GuildModel
    has_guild_permission: GuildPermission
