"""Required application services for player routes."""

from collections.abc import Callable
from dataclasses import dataclass

import discord
from discord.ext import commands
from database.models import (
    GuildModel,
    UserModel,
)


@dataclass(frozen=True, slots=True)
class PlayerDependencies:
    _get_guild_voice_state: Callable[[discord.Guild, int], discord.VoiceState | None]
    bot_instance: commands.Bot | None
    get_all_guilds: Callable[[], list[discord.Guild]]
    get_all_voice_clients: Callable[[], list[discord.VoiceClient]]
    guild_model: GuildModel
    user_model: UserModel
