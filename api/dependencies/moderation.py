"""Required application services for moderation routes."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

import discord
from database.models import (
    GuildModuleModel,
    ModerationAppealModel,
    ModerationCaseModel,
    ModerationEvidenceModel,
)
from api.dependencies.common import (
    CapabilityCheck,
    GuildLookup,
    ModerationTargetValidator,
)


@dataclass(frozen=True, slots=True)
class ModerationDependencies:
    EVIDENCE_UPLOAD_MAX_BYTES: int
    _actor_has_action_permission: Callable[[discord.Member | None, str], bool]
    _discord_user_snapshot: Callable[[discord.abc.User], dict[str, str]]
    _execute_web_moderation_action: Callable[[str, discord.Member, str, int | None], Awaitable[datetime | None]]
    _find_guild: GuildLookup
    _notify_web_case: Callable[[discord.Member, dict, str], Awaitable[None]]
    _validate_web_moderation_target: ModerationTargetValidator
    check_user_guild_capability: CapabilityCheck
    guild_module_model: GuildModuleModel
    moderation_appeal_model: ModerationAppealModel
    moderation_case_model: ModerationCaseModel
    moderation_evidence_model: ModerationEvidenceModel
