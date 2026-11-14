"""Required application services for tickets routes."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import discord
from discord.ext import commands
from database.models import (
    GuildPermissionModel,
    TicketModel,
    TicketPanelModel,
)
from database.connection import DatabaseManager
from api.dependencies.common import (
    CapabilityCheck,
)


@dataclass(frozen=True, slots=True)
class TicketsDependencies:
    NO_CACHE_HEADERS: dict[str, str]
    _cog_for_guild: Callable[[int, str], commands.Cog | None]
    _require_ticket_access: Callable[[int, int], Awaitable[tuple[dict, discord.Guild]]]
    _validate_ticket_panel_for_guild: Callable[[discord.Guild, dict], dict]
    check_user_guild_capability: CapabilityCheck
    db_manager: DatabaseManager
    guild_permission_model: GuildPermissionModel
    ticket_model: TicketModel
    ticket_panel_model: TicketPanelModel
    create_ticket_panel_view: Callable[[dict], discord.ui.View]
