"""Required application services for giveaways routes."""

from dataclasses import dataclass
from collections.abc import Callable
from datetime import timedelta
import discord

from discord.ext import commands
from database.models import (
    GiveawayModel,
)
from api.dependencies.common import (
    PermissionCheck,
    MemberLookup,
    GiveawayEmbedNormalizer,
)
from database.giveaway_creations import GiveawayCreationModel


@dataclass(frozen=True, slots=True)
class GiveawaysDependencies:
    _get_guild_member: MemberLookup
    bot_instance: commands.Bot | None
    check_user_guild_permission: PermissionCheck
    giveaway_model: GiveawayModel
    giveaway_creation_model: GiveawayCreationModel
    create_giveaway_view: Callable[[], discord.ui.View]
    normalize_giveaway_embed_config: GiveawayEmbedNormalizer
    parse_duration: Callable[[str], timedelta | None]
