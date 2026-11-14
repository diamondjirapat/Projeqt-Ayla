"""Internal API for ElysiaJS web server. Runs on 127.0.0.1:9000 inside the bot process."""

import asyncio
import time
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Optional

import discord
from discord.ext import commands
from fastapi import FastAPI, HTTPException
import uvicorn

from config import Config
from utils.helpers import silence_uvicorn_200_after_startup
from utils.music_embed import (
    resolve_music_banner_url_async,
    resolve_music_bar_url_async,
)
from database.models import (
    CustomCommandModel,
    GiveawayModel,
    GuildModel,
    GuildModuleModel,
    GuildPermissionModel,
    LevelingModel,
    ModerationAppealModel,
    ModerationCaseModel,
    ModerationEvidenceModel,
    TicketModel,
    TicketPanelModel,
    UserModel,
)
from database.scheduler_models import (
    ScheduledItemModel,
)
from database.giveaway_creations import GiveawayCreationModel
from utils.i18n import i18n
from utils.permissions import has_capability
from utils.tickets import normalize_ticket_panel

# Compatibility exports for older callers; routes import their utilities directly.
import csv as csv
import io as io
import json as json
import re as re
import lava_lyra as lava_lyra
from typing import cast as cast
from fastapi import Body as Body, Header as Header, Query as Query, Response as Response
from database.connection import db_manager as db_manager
from database.scheduler_models import (
    serialize_scheduler_item as serialize_scheduler_item,
    validate_scheduler_payload as validate_scheduler_payload,
)
from utils.music_embed import (
    build_custom_music_embed as build_custom_music_embed,
    get_default_music_embed_config as get_default_music_embed_config,
    normalize_http_url as normalize_http_url,
    normalize_music_embed_config as normalize_music_embed_config,
    refresh_discord_attachment_url as refresh_discord_attachment_url,
    refresh_music_embed_config_attachments as refresh_music_embed_config_attachments,
)
from utils.queue import (
    CustomPlayer as CustomPlayer,
    LoopMode as LoopMode,
    apply_session_volume as apply_session_volume,
    move_current_to_history as move_current_to_history,
    move_current_to_queue as move_current_to_queue,
    remember_session_volume as remember_session_volume,
)
from utils.modules import module_registry as module_registry
from utils.moderation import (
    normalize_casebook_config as normalize_casebook_config,
    select_crossed_escalation_rule as select_crossed_escalation_rule,
)
from utils.permissions import CAPABILITIES as CAPABILITIES
from utils.tickets import (
    TICKET_PRIORITIES as TICKET_PRIORITIES,
    render_transcript as render_transcript,
    safe_channel_name as safe_channel_name,
)
from utils.greetings import normalize_greetings as normalize_greetings
from utils.discord_profile import enrich_leaderboard_profiles as enrich_leaderboard_profiles
from utils.web_validation import (
    assert_public_url_host as assert_public_url_host,
    detect_evidence_extension as _detect_evidence_extension,
    parse_integer as _parse_integer,
    parse_iso_datetime as _parse_iso_datetime,
    split_url as _split_url,
)

__all__ = ['_detect_evidence_extension', '_parse_integer', '_parse_iso_datetime', '_split_url']

from services.social_alerts import (
    SOCIAL_ALERT_EVENTS as SOCIAL_ALERT_EVENTS,
    SOCIAL_ALERT_MAX_ENTRIES as SOCIAL_ALERT_MAX_ENTRIES,
    SOCIAL_ALERT_PLATFORMS as SOCIAL_ALERT_PLATFORMS,
    _legacy_stream_alerts_from_social as _legacy_stream_alerts_from_social,
    _normalize_social_alerts as _normalize_social_alerts,
    _normalize_stream_alerts as _normalize_stream_alerts,
    _social_alerts_from_legacy as _social_alerts_from_legacy,
)
from services.serialization import PreciseJSONResponse
from api.contracts.routing import ContractRoute
from api.operations import configure_operations
from services.serialization import serialize_track as serialize_track
from services.serialization import _serialize_api_value as _serialize_api_value
from services.serialization import _serialize_ticket as _serialize_ticket
from services.serialization import _serialize_custom_command as _serialize_custom_command
from services.serialization import _serialize_giveaway as _serialize_giveaway

from api.dependencies.administration import AdministrationDependencies
from api.dependencies.custom_commands import CustomCommandsDependencies
from api.dependencies.giveaways import GiveawaysDependencies
from api.dependencies.guild import GuildDependencies
from api.dependencies.guild_settings import GuildSettingsDependencies
from api.dependencies.user_cache import UserCacheDependencies
from api.dependencies.leveling import LevelingDependencies
from api.dependencies.moderation import ModerationDependencies
from api.dependencies.music_appearance import MusicAppearanceDependencies
from api.dependencies.player import PlayerDependencies
from api.dependencies.scheduler import SchedulerDependencies
from api.dependencies.tickets import TicketsDependencies
from api.dependencies.trap_channel import TrapChannelDependencies

logger = logging.getLogger(__name__)

guild_model = GuildModel()
guild_module_model = GuildModuleModel()
guild_permission_model = GuildPermissionModel()
moderation_case_model = ModerationCaseModel()
moderation_appeal_model = ModerationAppealModel()
moderation_evidence_model = ModerationEvidenceModel()
giveaway_model = GiveawayModel()
giveaway_creation_model = GiveawayCreationModel()
scheduler_model = ScheduledItemModel()
user_model = UserModel()
ticket_model = TicketModel()
ticket_panel_model = TicketPanelModel()
leveling_model = LevelingModel()
custom_command_model = CustomCommandModel()

EVIDENCE_UPLOAD_MAX_BYTES = 8 * 1024 * 1024
STATIC_DIR = (Config.PROJECT_ROOT / 'static').resolve()

NO_CACHE_HEADERS = {
    'Cache-Control': 'no-cache, no-store, must-revalidate',
    'Pragma': 'no-cache',
    'Expires': '0',
}

internal_app = FastAPI(title='Ayla Internal API', default_response_class=PreciseJSONResponse)
internal_app.router.route_class = ContractRoute
_INTERNAL_KEY: Optional[str] = None
bot_instance: Optional[commands.Bot] = None
bot: Optional[commands.Bot] = None


def set_internal_key(key: str) -> None:
    global _INTERNAL_KEY
    _INTERNAL_KEY = key


def set_bot_instance(bot_inst: commands.Bot) -> None:
    global bot_instance, bot
    bot_instance = bot_inst
    bot = bot_inst


def get_all_guilds():
    if not bot_instance:
        return []
    guilds = []
    seen = set()
    all_bots = getattr(bot_instance, 'all_bots', [bot_instance])
    for b in all_bots:
        for g in b.guilds:
            if g.id not in seen:
                guilds.append(g)
                seen.add(g.id)
    return guilds


def get_all_voice_clients():
    if not bot_instance:
        return []
    clients = []
    all_bots = getattr(bot_instance, 'all_bots', [bot_instance])
    for b in all_bots:
        clients.extend(b.voice_clients)
    return clients


def _find_guild(guild_id: int):
    return next((guild for guild in get_all_guilds() if guild.id == guild_id), None)


def _get_guild_voice_state(guild, user_id: int) -> Optional[discord.VoiceState]:
    """Lookup voice state using the exact authenticated Discord user ID."""
    if not guild or not hasattr(guild, '_voice_states'):
        return None
    try:
        uid = int(user_id)
    except (ValueError, TypeError):
        return None
    return guild._voice_states.get(uid)


def _get_guild_member(guild_id: int, user_id: int) -> Optional[discord.Member]:
    """Cached membership lookup for a user across all bot instances."""
    if not bot_instance:
        return None
    try:
        uid = int(user_id)
    except (ValueError, TypeError):
        return None
    for b in getattr(bot_instance, 'all_bots', [bot_instance]):
        guild = b.get_guild(guild_id)
        if guild:
            member = guild.get_member(uid)
            if member:
                return member
    return None


# (guild_id, user_id) -> monotonic expiry for users Discord reported as non-members.
_NON_MEMBER_TTL_SECONDS = 60.0
_NON_MEMBER_CACHE_MAX = 10_000
_non_member_cache: dict[tuple[int, int], float] = {}


def _remember_non_member(guild_id: int, uid: int) -> None:
    if len(_non_member_cache) >= _NON_MEMBER_CACHE_MAX:
        now = time.monotonic()
        for key in [key for key, expiry in _non_member_cache.items() if expiry <= now]:
            _non_member_cache.pop(key, None)
        if len(_non_member_cache) >= _NON_MEMBER_CACHE_MAX:
            _non_member_cache.pop(next(iter(_non_member_cache)))
    _non_member_cache[(guild_id, uid)] = time.monotonic() + _NON_MEMBER_TTL_SECONDS


async def _resolve_member(guild, guild_id: int, uid: int) -> Optional[discord.Member]:
    """Cached member, else one REST fetch unless the cache is authoritative or a recent miss is known."""
    member = guild.get_member(uid)
    if member:
        return member
    # With the members intent a fully chunked guild's cache is complete: a miss means "not a member",
    # so a REST call would only spend the shared bot token's rate limit.
    if getattr(guild, 'chunked', False) is True:
        return None
    key = (int(guild_id), uid)
    expiry = _non_member_cache.get(key)
    if expiry is not None:
        if expiry > time.monotonic():
            return None
        _non_member_cache.pop(key, None)
    try:
        return await guild.fetch_member(uid)
    except discord.NotFound:
        _remember_non_member(int(guild_id), uid)
        return None
    except Exception:
        return None


async def check_user_guild_capability(user_id: int, guild_id: int, capability: str) -> bool:
    try:
        uid = int(user_id)
    except (ValueError, TypeError):
        return False
    if uid in Config.OWNER_IDS:
        return True
    if not bot_instance:
        return False
    all_bots = getattr(bot_instance, 'all_bots', [bot_instance])
    for b in all_bots:
        guild = b.get_guild(guild_id)
        if guild:
            member = await _resolve_member(guild, guild_id, uid)
            if not member:
                continue
            if member:
                if has_capability(member, capability):
                    return True
                grants = await guild_permission_model.get_role_grants(guild_id)
                return has_capability(member, capability, grants)
    return False


async def check_user_guild_permission(user_id: int, guild_id: int) -> bool:
    return await check_user_guild_capability(user_id, guild_id, 'edit_configuration')


async def has_guild_permission(guild_id: int, user_id: int, permission: str = 'manage_guild') -> bool:
    """Check if a user has a specific permission in a guild."""
    if permission in ('manage_guild', 'edit_configuration'):
        return await check_user_guild_capability(user_id, guild_id, 'edit_configuration')
    return await check_user_guild_capability(user_id, guild_id, permission)


configure_operations(internal_app, lambda: _INTERNAL_KEY, lambda: bot_instance, lambda: db_manager)


# ---------------------------------------------------------------------------
# Guild modules & permissions endpoints (Category C — guild management)


def _module_runtime_health(definition, guild: discord.Guild, enabled: bool) -> dict:
    candidate = next(
        (item for item in getattr(bot_instance, 'all_bots', [bot_instance]) if item and item.get_guild(guild.id)),
        None,
    )
    member = guild.me
    permissions = getattr(member, 'guild_permissions', None)
    intents = getattr(candidate, 'intents', None)
    missing_permissions = [
        name
        for name in definition.required_bot_permissions
        if permissions is None or not bool(getattr(permissions, name, False))
    ]
    missing_intents = [
        name for name in definition.required_intents if intents is None or not bool(getattr(intents, name, False))
    ]
    status = 'disabled' if not enabled else 'degraded' if missing_permissions or missing_intents else 'healthy'
    return {
        'status': status,
        'missing_bot_permissions': missing_permissions,
        'missing_intents': missing_intents,
        'checked_at': datetime.now(UTC),
    }


def _discord_user_snapshot(user: discord.abc.User) -> dict[str, str]:
    return {
        'user_id': str(user.id),
        'username': user.name,
        'display_name': getattr(user, 'display_name', user.name),
    }


def _validate_web_moderation_target(
    guild: discord.Guild,
    actor: Optional[discord.Member],
    target: discord.Member,
    *,
    actor_is_owner: bool = False,
) -> None:
    if target.bot:
        raise HTTPException(status_code=400, detail='Bots cannot be targeted by this moderation action.')
    if target.id == guild.owner_id:
        raise HTTPException(status_code=400, detail='The server owner cannot be moderated.')
    if (
        actor is not None
        and not actor_is_owner
        and not actor.guild_permissions.administrator
        and target.top_role >= actor.top_role
    ):
        raise HTTPException(status_code=403, detail='The selected member is at or above your highest role.')
    bot_member = guild.me
    if bot_member is None or target.top_role >= bot_member.top_role:
        raise HTTPException(status_code=400, detail='Ayla cannot moderate a member at or above its highest role.')


_NATIVE_PERMISSION_FOR_ACTION = {
    'timeout': 'moderate_members',
    'kick': 'kick_members',
    'ban': 'ban_members',
    'unban': 'ban_members',
}


def _actor_has_action_permission(actor: Optional[discord.Member], action: str) -> bool:
    """Whether the acting member holds the native Discord permission for *action*.

    Application role grants and the shared ``manage_moderation`` capability are
    deliberately not consulted: executing a Discord-level action must require
    the matching native Discord permission, matching the slash-command surface.
    """
    if actor is None:
        return False
    if actor.guild_permissions.administrator:
        return True
    required = _NATIVE_PERMISSION_FOR_ACTION.get(action)
    if required is None:
        return True
    return bool(getattr(actor.guild_permissions, required, False))


async def _execute_web_moderation_action(
    action: str,
    target: discord.Member,
    reason: str,
    duration_seconds: Optional[int],
) -> Optional[datetime]:
    if action == 'timeout':
        expires_at = datetime.now(UTC) + timedelta(seconds=duration_seconds or 0)
        await target.timeout(expires_at, reason=reason)
        return expires_at
    if action == 'kick':
        await target.kick(reason=reason)
    elif action == 'ban':
        await target.ban(reason=reason)
    return None


async def _notify_web_case(target: discord.Member, case: dict, reason: str) -> None:
    try:
        locale = await i18n.get_guild_locale(target.guild.id) or 'en'
        message = i18n.get_text(
            'commands.casebook.dm_notification',
            locale,
            action=case['action'],
            server=target.guild.name,
            reason=reason[:1000],
            case_id=case['case_id'],
        )
        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label=i18n.get_text('commands.casebook.appeal_button', locale),
                url=f'{Config.WEB_URL.rstrip("/")}/appeal?guild_id={target.guild.id}&case_id={case["case_id"]}',
            )
        )
        await target.send(message, view=view)
    except (discord.Forbidden, discord.HTTPException):
        logger.info('Could not notify member %s about web-created case', target.id)


async def _require_ticket_access(user_id: int, guild_id: int) -> tuple[dict, discord.Guild]:
    user = {'id': user_id}
    if not await check_user_guild_capability(user_id, guild_id, 'manage_tickets'):
        raise HTTPException(status_code=403, detail='You do not have permission to manage tickets.')
    guild = _find_guild(guild_id)
    if guild is None:
        raise HTTPException(status_code=404, detail='Server not found.')
    return user, guild


def _cog_for_guild(guild_id: int, name: str):
    if not bot_instance:
        return None
    for candidate in getattr(bot_instance, 'all_bots', [bot_instance]):
        if candidate.get_guild(guild_id):
            cog = candidate.get_cog(name)
            if cog:
                return cog
    return None


def _validate_ticket_panel_for_guild(guild: discord.Guild, raw: dict) -> dict:
    panel = normalize_ticket_panel(raw)
    guild_role_ids = {role.id for role in guild.roles}
    if set(panel['staff_role_ids']) - guild_role_ids:
        raise HTTPException(status_code=400, detail='A ticket staff role does not belong to this server.')
    if panel['discord_category_id']:
        category = guild.get_channel(panel['discord_category_id'])
        if not isinstance(category, discord.CategoryChannel):
            raise HTTPException(status_code=400, detail='The ticket category is not a server category channel.')
    if panel['inactivity_close_hours'] and (panel['inactivity_close_hours'] <= panel['inactivity_reminder_hours']):
        raise HTTPException(status_code=400, detail='Inactivity closure must occur after the reminder.')
    return panel


def _session_avatar_url(user: dict) -> str:
    user_id = int(user.get('id') or 0)
    avatar_hash = str(user.get('avatar') or '')
    if avatar_hash:
        extension = 'gif' if avatar_hash.startswith('a_') else 'webp'
        return f'https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.{extension}?size=256'
    return f'https://cdn.discordapp.com/embed/avatars/{(user_id >> 22) % 6}.png'


def _guild_preview_context(
    guild: discord.Guild,
    user: dict,
    *,
    member: Optional[discord.Member] = None,
    channel: Optional[discord.abc.GuildChannel] = None,
) -> dict[str, str | int]:
    """Build the real member/server values shown by dashboard previews."""
    member = member or guild.get_member(int(user['id']))
    selected_channel = channel or getattr(guild, 'system_channel', None)
    if selected_channel is None:
        selected_channel = next(iter(getattr(guild, 'text_channels', [])), None)

    username = str(getattr(member, 'name', '') or user.get('username') or 'User')
    display_name = str(getattr(member, 'display_name', '') or username)
    display_avatar = getattr(member, 'display_avatar', None)
    avatar_url = str(getattr(display_avatar, 'url', '') or _session_avatar_url(user))
    member_count = getattr(guild, 'member_count', None)
    if member_count is None:
        member_count = len(getattr(guild, 'members', []))

    channel_id = str(getattr(selected_channel, 'id', '') or '')
    channel_name = str(getattr(selected_channel, 'name', '') or '')
    guild_icon = getattr(guild, 'icon', None)
    guild_banner = getattr(guild, 'banner', None)
    return {
        'user_id': str(user['id']),
        'username': username,
        'display_name': display_name,
        'mention': f'@{display_name}',
        'avatar_url': avatar_url,
        'guild_id': str(guild.id),
        'guild_name': str(guild.name),
        'guild_icon_url': str(guild_icon.url) if guild_icon else '',
        'guild_banner_url': str(guild_banner.url) if guild_banner else '',
        'member_count': int(member_count or 0),
        'channel_id': channel_id,
        'channel_name': channel_name,
        'channel_mention': f'#{channel_name}' if channel_name else '',
    }


# Auth endpoints (Category G — admin check)

# ---------------------------------------------------------------------------
# Manageable guilds endpoint (Category H — guild listing)

# ---------------------------------------------------------------------------
# Guild settings endpoints (Category I — guild configuration)


def _validate_assignable_role(role: discord.Role, invoker: Optional[discord.Member]) -> Optional[str]:
    """Mirror the Discord-side guards for role bindings set via the web panel."""
    if role >= role.guild.me.top_role:
        return 'that role is at or above my highest role.'
    if role.managed:
        return 'that role is managed by an integration and cannot be assigned.'
    if invoker is not None and not invoker.guild_permissions.administrator and role >= invoker.top_role:
        return 'you cannot bind roles at or above your own highest role.'
    return None


async def _resolve_music_banner_url() -> str:
    return await resolve_music_banner_url_async(getattr(Config, 'MUSIC_BANNER_URL', ''))


async def _resolve_music_bar_url() -> str:
    return await resolve_music_bar_url_async(getattr(Config, 'BAR_URL', ''))


# ---------------------------------------------------------------------------
# Greetings endpoints (Category J — welcome/farewell configuration)

# ---------------------------------------------------------------------------
# Reaction-roles generate endpoint (Category K)

# ---------------------------------------------------------------------------
# Custom commands endpoints (Category L)

# ---------------------------------------------------------------------------
# Leveling endpoints (Category M — leaderboards)

# ---------------------------------------------------------------------------
# Giveaway endpoints (Category N — giveaways with live Discord messages)

# Scheduler endpoints (Category S — scheduled announcements and events)


def provide_administration_dependencies() -> AdministrationDependencies:
    return AdministrationDependencies(
        _find_guild=_find_guild,
        _module_runtime_health=_module_runtime_health,
        check_user_guild_capability=check_user_guild_capability,
        check_user_guild_permission=check_user_guild_permission,
        guild_module_model=guild_module_model,
        guild_permission_model=guild_permission_model,
    )


def provide_custom_commands_dependencies() -> CustomCommandsDependencies:
    return CustomCommandsDependencies(
        _find_guild=_find_guild,
        _guild_preview_context=_guild_preview_context,
        bot_instance=bot_instance,
        check_user_guild_permission=check_user_guild_permission,
        custom_command_model=custom_command_model,
        guild_model=guild_model,
    )


def provide_giveaways_dependencies() -> GiveawaysDependencies:
    from cogs.giveaway import GiveawayView, normalize_giveaway_embed_config, parse_duration

    return GiveawaysDependencies(
        _get_guild_member=_get_guild_member,
        bot_instance=bot_instance,
        check_user_guild_permission=check_user_guild_permission,
        giveaway_model=giveaway_model,
        giveaway_creation_model=giveaway_creation_model,
        create_giveaway_view=GiveawayView,
        normalize_giveaway_embed_config=normalize_giveaway_embed_config,
        parse_duration=parse_duration,
    )


def provide_guild_dependencies() -> GuildDependencies:
    return GuildDependencies(
        _find_guild=_find_guild,
        _guild_preview_context=_guild_preview_context,
        check_user_guild_permission=check_user_guild_permission,
        check_user_guild_capability=check_user_guild_capability,
        get_all_guilds=get_all_guilds,
        guild_model=guild_model,
    )


def provide_guild_settings_dependencies() -> GuildSettingsDependencies:
    return GuildSettingsDependencies(
        _find_guild=_find_guild,
        _guild_preview_context=_guild_preview_context,
        _validate_assignable_role=_validate_assignable_role,
        bot_instance=bot_instance,
        check_user_guild_permission=check_user_guild_permission,
        check_user_guild_capability=check_user_guild_capability,
        get_all_voice_clients=get_all_voice_clients,
        guild_model=guild_model,
        i18n=i18n,
    )


def provide_leveling_dependencies() -> LevelingDependencies:
    return LevelingDependencies(
        _find_guild=_find_guild,
        _get_guild_member=_get_guild_member,
        bot=bot,
        bot_instance=bot_instance,
        leveling_model=leveling_model,
    )


def provide_moderation_dependencies() -> ModerationDependencies:
    return ModerationDependencies(
        EVIDENCE_UPLOAD_MAX_BYTES=EVIDENCE_UPLOAD_MAX_BYTES,
        _actor_has_action_permission=_actor_has_action_permission,
        _discord_user_snapshot=_discord_user_snapshot,
        _execute_web_moderation_action=_execute_web_moderation_action,
        _find_guild=_find_guild,
        _notify_web_case=_notify_web_case,
        _validate_web_moderation_target=_validate_web_moderation_target,
        check_user_guild_capability=check_user_guild_capability,
        guild_module_model=guild_module_model,
        moderation_appeal_model=moderation_appeal_model,
        moderation_case_model=moderation_case_model,
        moderation_evidence_model=moderation_evidence_model,
    )


def provide_music_appearance_dependencies() -> MusicAppearanceDependencies:
    return MusicAppearanceDependencies(
        _find_guild=_find_guild,
        _resolve_music_banner_url=_resolve_music_banner_url,
        _resolve_music_bar_url=_resolve_music_bar_url,
        bot=bot,
        bot_instance=bot_instance,
        guild_model=guild_model,
        has_guild_permission=has_guild_permission,
    )


def provide_player_dependencies() -> PlayerDependencies:
    return PlayerDependencies(
        _get_guild_voice_state=_get_guild_voice_state,
        bot_instance=bot_instance,
        get_all_guilds=get_all_guilds,
        get_all_voice_clients=get_all_voice_clients,
        guild_model=guild_model,
        user_model=user_model,
    )


def provide_scheduler_dependencies() -> SchedulerDependencies:
    return SchedulerDependencies(
        _find_guild=_find_guild,
        _get_guild_member=_get_guild_member,
        bot_instance=bot_instance,
        check_user_guild_permission=check_user_guild_permission,
        scheduler_model=scheduler_model,
    )


def provide_tickets_dependencies() -> TicketsDependencies:
    from cogs.tickets import TicketPanelView

    return TicketsDependencies(
        NO_CACHE_HEADERS=NO_CACHE_HEADERS,
        _cog_for_guild=_cog_for_guild,
        _require_ticket_access=_require_ticket_access,
        _validate_ticket_panel_for_guild=_validate_ticket_panel_for_guild,
        check_user_guild_capability=check_user_guild_capability,
        db_manager=db_manager,
        guild_permission_model=guild_permission_model,
        ticket_model=ticket_model,
        ticket_panel_model=ticket_panel_model,
        create_ticket_panel_view=TicketPanelView,
    )


def provide_trap_channel_dependencies() -> TrapChannelDependencies:
    return TrapChannelDependencies(
        _find_guild=_find_guild,
        _guild_preview_context=_guild_preview_context,
        check_user_guild_permission=check_user_guild_permission,
        guild_model=guild_model,
    )


def provide_user_cache_dependencies() -> UserCacheDependencies:
    return UserCacheDependencies(i18n=i18n)


def _register_feature_routes():
    from api.routes import user_cache
    from api.routes import administration
    from api.routes import custom_commands
    from api.routes import giveaways
    from api.routes import guild
    from api.routes import guild_settings
    from api.routes import leveling
    from api.routes import moderation
    from api.routes import music_appearance
    from api.routes import player
    from api.routes import scheduler
    from api.routes import tickets
    from api.routes import trap_channel

    globals().update(user_cache.register_routes(internal_app, provide_user_cache_dependencies))
    globals().update(administration.register_routes(internal_app, provide_administration_dependencies))
    globals().update(custom_commands.register_routes(internal_app, provide_custom_commands_dependencies))
    globals().update(giveaways.register_routes(internal_app, provide_giveaways_dependencies))
    globals().update(guild.register_routes(internal_app, provide_guild_dependencies))
    globals().update(guild_settings.register_routes(internal_app, provide_guild_settings_dependencies))
    globals().update(leveling.register_routes(internal_app, provide_leveling_dependencies))
    globals().update(moderation.register_routes(internal_app, provide_moderation_dependencies))
    globals().update(music_appearance.register_routes(internal_app, provide_music_appearance_dependencies))
    globals().update(player.register_routes(internal_app, provide_player_dependencies))
    globals().update(scheduler.register_routes(internal_app, provide_scheduler_dependencies))
    globals().update(tickets.register_routes(internal_app, provide_tickets_dependencies))
    globals().update(trap_channel.register_routes(internal_app, provide_trap_channel_dependencies))


_register_feature_routes()


class InternalApi(commands.Cog):
    """Starts the internal API server on 127.0.0.1:9000."""

    def __init__(self, bot):
        self.bot = bot
        self.server_task = None
        self._access_filter_task = None

    async def cog_load(self):
        if not getattr(self.bot, 'is_main', True):
            logger.info('[INTERNAL API] Non-main bot; skipping.')
            return
        set_bot_instance(self.bot)
        key = os.getenv('AYLA_INTERNAL_KEY', '').strip()
        if Config.INTERNAL_API_HOST not in ('127.0.0.1', 'localhost', '::1') and len(key) < 32:
            raise ValueError('A network-accessible internal API requires AYLA_INTERNAL_KEY of at least 32 characters')
        if not key:
            key = secrets.token_hex(32)
            try:
                key_path = Config.PROJECT_ROOT / '.internal_key'
                key_path.write_text(key, encoding='utf-8')
            except Exception as e:
                raise RuntimeError('Failed to share the internal API key with the web process') from e
        set_internal_key(key)
        os.environ['AYLA_INTERNAL_KEY'] = key
        config = uvicorn.Config(
            internal_app,
            host=Config.INTERNAL_API_HOST,
            port=Config.INTERNAL_API_PORT,
            log_level='info',
            loop='asyncio',
        )
        self.server = uvicorn.Server(config)
        self.server_task = asyncio.create_task(self.server.serve())
        self._access_filter_task = asyncio.create_task(silence_uvicorn_200_after_startup(self.server))
        logger.info('[INTERNAL API] Starting on %s:%s', Config.INTERNAL_API_HOST, Config.INTERNAL_API_PORT)

    async def cog_unload(self):
        if not getattr(self.bot, 'is_main', True):
            return
        if self._access_filter_task:
            self._access_filter_task.cancel()
        if self.server_task:
            self.server.should_exit = True
            try:
                await asyncio.wait_for(self.server_task, timeout=5)
            except (asyncio.CancelledError, TimeoutError):
                pass
        try:
            key_path = Config.PROJECT_ROOT / '.internal_key'
            if key_path.exists():
                key_path.unlink()
        except Exception:
            pass


async def setup(bot):
    await bot.add_cog(InternalApi(bot))
