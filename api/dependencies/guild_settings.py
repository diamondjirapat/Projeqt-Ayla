"""Required application services for guild_settings routes."""

from collections.abc import Callable
from dataclasses import dataclass

import discord
from discord.ext import commands
from database.models import (
    GuildModel,
)
from utils.i18n import I18nManager
from api.dependencies.common import (
    CapabilityCheck,
    PermissionCheck,
    GuildLookup,
    PreviewContext,
)


@dataclass(frozen=True, slots=True)
class GuildSettingsDependencies:
    _find_guild: GuildLookup
    _guild_preview_context: PreviewContext
    _validate_assignable_role: Callable[[discord.Role, discord.Member | None], str | None]
    bot_instance: commands.Bot | None
    check_user_guild_permission: PermissionCheck
    check_user_guild_capability: CapabilityCheck
    get_all_voice_clients: Callable[[], list[discord.VoiceClient]]
    guild_model: GuildModel
    i18n: I18nManager
