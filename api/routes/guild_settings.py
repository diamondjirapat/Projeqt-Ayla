"""Guild Settings internal HTTP endpoints."""

from collections.abc import Callable
from typing import Any
from fastapi import FastAPI, Body, HTTPException, Header, Query
from api.dependencies.guild_settings import GuildSettingsDependencies
from api.dependencies.validation import validate_dependencies
from config import Config
from services.social_runtime import social_runtime
from services.social_alerts import (
    SOCIAL_ALERT_EVENTS,
    SOCIAL_ALERT_MAX_ENTRIES,
    SOCIAL_ALERT_PLATFORMS,
    _legacy_stream_alerts_from_social,
    _normalize_social_alerts,
    _normalize_stream_alerts,
    _social_alerts_from_legacy,
)
from utils.queue import CustomPlayer
from utils.web_validation import parse_integer as _parse_integer
import asyncio
import discord
import logging
import re

logger = logging.getLogger(__name__)


def _check_alert_mention(
    guild: discord.Guild,
    channel: discord.abc.GuildChannel,
    invoker: discord.Member | None,
    role: str,
    field: str,
    previous: tuple[str, str] | None,
    channel_id: str,
) -> str:
    """Validate a social/stream alert mentionRole against the configurer's own ping authority.

    An entry that keeps the exact mention and channel it already had is accepted unchanged, so an
    editor without Mention Everyone can still save other settings without dropping existing pings.
    """
    role = (role or 'none').strip() or 'none'
    if role == 'none':
        return role
    if role not in ('everyone', 'here') and not role.isdigit():
        raise HTTPException(status_code=400, detail=f'{field}.mentionRole is invalid.')
    if previous is not None and previous == (role, channel_id):
        return role
    can_ping_all = False
    if invoker is not None:
        try:
            can_ping_all = bool(channel.permissions_for(invoker).mention_everyone)
        except Exception:
            can_ping_all = False
    if role in ('everyone', 'here'):
        if not can_ping_all:
            raise HTTPException(status_code=403, detail=f'{field}: Mention Everyone permission is required in that channel.')
        return role
    target = guild.get_role(int(role))
    if target is None or target.is_default():
        raise HTTPException(status_code=400, detail=f'{field}.mentionRole must identify a role in this server.')
    if not target.mentionable and not can_ping_all:
        raise HTTPException(status_code=403, detail=f'{field}: you cannot mention that role.')
    return role


def register_routes(
    app: FastAPI,
    provide: Callable[[], GuildSettingsDependencies],
) -> dict[str, Callable[..., Any]]:
    validate_dependencies(provide(), GuildSettingsDependencies)

    @app.get('/internal/guild/autoplay/channel')
    async def internal_resolve_autoplay_channel(
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
        input: str = Query(..., min_length=1, max_length=2048),
    ):
        services = provide()
        if not await services.check_user_guild_permission(user_id, guild_id):
            raise HTTPException(status_code=403, detail='You do not have permission to edit settings for this server.')
        from services.youtube_channels import ChannelLookupError, resolve_channel

        try:
            return await resolve_channel(input)
        except ChannelLookupError as error:
            status = {'invalid_input': 422, 'not_found': 404, 'lookup_unavailable': 503}[error.code]
            raise HTTPException(status_code=status, detail=error.code) from None

    @app.get('/internal/guild/settings')
    async def internal_get_guild_settings(
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
    ):
        services = provide()
        can_edit = await services.check_user_guild_permission(user_id, guild_id)

        guild = services._find_guild(guild_id)
        if not guild:
            raise HTTPException(status_code=404, detail='Server not found or bot is not in it.')

        is_global_admin = int(user_id) in Config.OWNER_IDS
        member = guild.get_member(user_id)
        if not member and not is_global_admin:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                raise HTTPException(status_code=403, detail='You are not a member of this server.')

        user = {'id': user_id, 'username': str(user_id)}

        def visible_text_channels() -> list[dict[str, str]]:
            if is_global_admin or member is None:
                return [{'id': str(ch.id), 'name': ch.name} for ch in guild.text_channels]
            return [{'id': str(ch.id), 'name': ch.name} for ch in guild.text_channels if ch.permissions_for(member).view_channel]

        can_view = can_edit or is_global_admin or await services.check_user_guild_capability(user_id, guild_id, 'view_configuration')
        if not can_view:
            # Plain members (scheduler, giveaway and custom-command pages) only need the prefix and the
            # channels they can already see; the guild configuration itself stays with configurers.
            from utils.prefix_manager import prefix_manager

            return {
                'guild_id': str(guild_id),
                'guild_name': guild.name,
                'guild_icon_url': str(guild.icon.url) if guild.icon else '',
                'preview_context': services._guild_preview_context(guild, user, member=member),
                'can_edit': False,
                'limited': True,
                'prefix': await prefix_manager.get_guild_prefix(guild_id) or prefix_manager.default_prefix,
                'text_channels': visible_text_channels(),
            }

        default_vol = await services.guild_model.get_default_volume(guild_id)
        music_chan = await services.guild_model.get_music_channel(guild_id)
        music_msg = await services.guild_model.get_music_message(guild_id)
        level_chan = await services.guild_model.get_level_channel(guild_id)

        from utils.prefix_manager import prefix_manager

        prefix = await prefix_manager.get_guild_prefix(guild_id) or prefix_manager.default_prefix

        db_guild = await services.guild_model.get_guild(guild_id)
        locale = db_guild.get('locale', 'en') if db_guild else 'en'
        auto_role_id = db_guild.get('auto_role_id') if db_guild else None

        channels = visible_text_channels()

        roles = []
        role_names_by_id = {}
        bot_top_role = guild.me.top_role if guild.me else None
        for r in guild.roles:
            if r.name != '@everyone':
                role_id = str(r.id)
                assignable = not r.managed and (bot_top_role is None or r < bot_top_role)
                roles.append({'id': role_id, 'name': r.name, 'color': str(r.color), 'assignable': assignable})
                role_names_by_id[role_id] = r.name

        guild_emojis = []
        for e in guild.emojis:
            guild_emojis.append({'id': str(e.id), 'name': e.name, 'url': e.url, 'animated': getattr(e, 'animated', False)})

        raw_reaction_roles = db_guild.get('reaction_roles', {}) if db_guild else {}
        reaction_roles = {str(message_id): {str(emoji): str(role_id) for emoji, role_id in mappings.items()} for message_id, mappings in raw_reaction_roles.items() if isinstance(mappings, dict)}
        stored_role_names = db_guild.get('reaction_role_names', {}) if db_guild else {}
        reaction_role_names = dict(stored_role_names) if isinstance(stored_role_names, dict) else {}
        for mappings in reaction_roles.values():
            for raw_role_id in mappings.values():
                role_id = str(raw_role_id)
                if role_id in role_names_by_id:
                    reaction_role_names[role_id] = role_names_by_id[role_id]
        stored_reaction_role_embeds = db_guild.get('reaction_role_embeds', {}) if db_guild else {}
        reaction_role_embeds = {str(message_id): dict(embed_settings) for message_id, embed_settings in stored_reaction_role_embeds.items() if isinstance(embed_settings, dict)}
        stored_reaction_role_inline = db_guild.get('reaction_role_inline', {}) if db_guild else {}
        reaction_role_inline = {str(message_id): {str(emoji): bool(is_inline) for emoji, is_inline in inline_settings.items()} for message_id, inline_settings in stored_reaction_role_inline.items() if isinstance(inline_settings, dict)}
        reaction_role_titles = {}

        async def fetch_message_embed(guild, msg_id_str):
            try:
                msg_id = int(msg_id_str)
            except (TypeError, ValueError):
                return {
                    'title': 'Invalid ID',
                    'description': '',
                    'image_url': '',
                    'thumbnail_url': '',
                    'footer_text': '',
                    'footer_url': '',
                }

            def serialize_message_embed(message):
                embed = message.embeds[0] if message.embeds else None
                if not embed:
                    return {
                        'title': 'Reaction Roles',
                        'description': '',
                        'image_url': '',
                        'thumbnail_url': '',
                        'footer_text': '',
                        'footer_url': '',
                    }
                return {
                    'title': embed.title or 'Reaction Roles',
                    'description': embed.description or '',
                    'image_url': getattr(embed.image, 'url', '') or '',
                    'thumbnail_url': getattr(embed.thumbnail, 'url', '') or '',
                    'footer_text': getattr(embed.footer, 'text', '') or '',
                    'footer_url': getattr(embed.footer, 'icon_url', '') or '',
                }

            from utils.reaction_role_access import channel_hint, resolve_reaction_role_message

            hint = channel_hint(db_guild, msg_id)
            msg = await resolve_reaction_role_message(services.bot_instance, guild, msg_id, member, hint=hint)
            if msg is None:
                return None
            if getattr(msg.channel, 'id', None) != hint:
                discovered_channels[msg_id_str] = msg.channel.id
            return serialize_message_embed(msg)

        discovered_channels: dict[str, int] = {}
        if reaction_roles:
            tasks = [fetch_message_embed(guild, msg_id) for msg_id in reaction_roles.keys()]
            fetched_embeds = await asyncio.gather(*tasks)
            if discovered_channels:
                # Remember where scanned messages live so later loads fetch them directly.
                from utils.reaction_role_access import CHANNEL_HINTS_FIELD

                try:
                    await services.guild_model.update_guild(
                        guild_id,
                        {f'{CHANNEL_HINTS_FIELD}.{key}': value for key, value in discovered_channels.items()},
                    )
                except Exception:
                    logger.warning('Could not store reaction-role channel hints for guild %s', guild_id)
            reaction_role_embeds = {key: value for key, value in reaction_role_embeds.items() if key in reaction_roles}
            for msg_id, fetched_embed in zip(reaction_roles.keys(), fetched_embeds):
                if fetched_embed is None:
                    reaction_role_embeds.pop(msg_id, None)
                    continue
                stored_embed = reaction_role_embeds.setdefault(msg_id, {})
                for key, value in fetched_embed.items():
                    stored_embed.setdefault(key, value)
                reaction_role_titles[msg_id] = stored_embed.get('title') or 'Reaction Roles'

        leveling_on = await services.guild_model.is_leveling_enabled(guild_id)
        level_alert_cfg = await services.guild_model.get_level_alert_config(guild_id)
        if level_alert_cfg.get('channel_id'):
            level_alert_cfg['channel_id'] = str(level_alert_cfg['channel_id'])
        else:
            level_alert_cfg['channel_id'] = str(level_chan) if level_chan else None
        level_alert_cfg['leveling_enabled'] = leveling_on

        stored_stream_alerts = db_guild.get('stream_alerts') if db_guild else None
        stored_social_alerts = db_guild.get('social_alerts') if db_guild else None
        if stored_social_alerts is None:
            social_alerts = _social_alerts_from_legacy(stored_stream_alerts)
        else:
            social_alerts = _normalize_social_alerts(stored_social_alerts)
        stream_alerts = _legacy_stream_alerts_from_social(social_alerts)
        if stored_social_alerts is None and stored_stream_alerts is not None:
            stream_alerts = _normalize_stream_alerts(stored_stream_alerts)

        autoplay_on = await services.guild_model.get_autoplay_enabled(guild_id)
        autoplay_state = await services.guild_model.get_autoplay_settings(guild_id)
        twenty_four_seven_on = await services.guild_model.get_247_enabled(guild_id)

        return {
            'guild_id': str(guild_id),
            'guild_name': guild.name,
            'guild_icon_url': str(guild.icon.url) if guild.icon else '',
            'guild_banner_url': str(guild.banner.url) if getattr(guild, 'banner', None) else '',
            'guild_member_count': int(getattr(guild, 'member_count', None) or len(getattr(guild, 'members', []))),
            'preview_context': services._guild_preview_context(guild, user, member=member),
            'can_edit': can_edit,
            'prefix': prefix,
            'default_volume': default_vol,
            'music_channel_id': str(music_chan) if music_chan else None,
            'music_message_id': str(music_msg) if music_msg else None,
            'autoplay': autoplay_on,
            'autoplay_preset': autoplay_state['preset'],
            'autoplay_config': autoplay_state['config'],
            'autoplay_custom': autoplay_state['custom'],
            'autoplay_default': autoplay_state['default'],
            'autoplay_default_version': autoplay_state['default_version'],
            'autoplay_custom_source_version': autoplay_state['custom_source_version'],
            'twenty_four_seven': twenty_four_seven_on,
            'level_channel_id': str(level_chan) if level_chan else None,
            'leveling_enabled': leveling_on,
            'level_alert_config': level_alert_cfg,
            'stream_alerts': stream_alerts,
            'social_alerts': social_alerts,
            'social_alerts_runtime': await social_runtime(guild_id, social_alerts),
            'locale': locale,
            'auto_role_id': str(auto_role_id) if auto_role_id else None,
            'text_channels': channels,
            'roles': roles,
            'guild_emojis': guild_emojis,
            'reaction_roles': reaction_roles,
            'reaction_role_names': reaction_role_names,
            'reaction_role_titles': reaction_role_titles,
            'reaction_role_embeds': reaction_role_embeds,
            'reaction_role_inline': reaction_role_inline,
        }

    @app.post('/internal/guild/settings')
    async def internal_save_guild_settings(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        raw_guild_id = data.get('guild_id')
        if raw_guild_id is None:
            raise HTTPException(status_code=400, detail='Missing guild_id in request body.')
        guild_id = _parse_integer(raw_guild_id, 'guild_id', minimum=1)

        can_edit = await services.check_user_guild_permission(user_id, guild_id)
        if not can_edit:
            raise HTTPException(status_code=403, detail='You do not have permission to edit settings for this server.')

        guild = services._find_guild(guild_id)
        if guild is None:
            raise HTTPException(status_code=404, detail='Server not found.')

        invoker_member = guild.get_member(int(user_id))
        if invoker_member is None:
            try:
                invoker_member = await guild.fetch_member(int(user_id))
            except discord.HTTPException:
                invoker_member = None

        # 1. Prefix
        if 'prefix' in data:
            prefix_val = data.get('prefix')
            from utils.prefix_manager import prefix_manager

            if prefix_val is not None and str(prefix_val).strip() != '':
                success, msg = await prefix_manager.set_guild_prefix(guild_id, str(prefix_val))
                if not success:
                    raise HTTPException(status_code=400, detail=msg)
            else:
                await prefix_manager.remove_guild_prefix(guild_id)

        # 2. Volume
        if 'default_volume' in data:
            default_vol = data.get('default_volume')
            if default_vol is not None:
                volume = _parse_integer(default_vol, 'default_volume', minimum=1, maximum=100)
                await services.guild_model.set_default_volume(guild_id, volume)

        # 3. Channel
        if 'music_channel_id' in data:
            music_chan_id = data.get('music_channel_id')
            if music_chan_id is not None and music_chan_id != '' and str(music_chan_id).lower() != 'null':
                channel_id = _parse_integer(music_chan_id, 'music_channel_id', minimum=1)
                channel = guild.get_channel(channel_id)
                if channel is None or not isinstance(channel, discord.TextChannel):
                    raise HTTPException(
                        status_code=400,
                        detail='music_channel_id must identify a text channel in this server.',
                    )
                await services.guild_model.set_music_channel(guild_id, channel_id)
            else:
                await services.guild_model.remove_music_channel(guild_id)

        # 3b. Music Message ID
        if 'music_message_id' in data:
            music_msg_id = data.get('music_message_id')
            if music_msg_id is not None and music_msg_id != '' and str(music_msg_id).lower() != 'null':
                msg_id = _parse_integer(music_msg_id, 'music_message_id', minimum=1)
                await services.guild_model.set_music_message(guild_id, msg_id)
            else:
                await services.guild_model.set_music_message(guild_id, 0)

        # 4. Music Playback Settings (Autoplay)
        active_players = [player for player in services.get_all_voice_clients() if isinstance(player, CustomPlayer) and player.guild.id == guild_id]

        if 'autoplay' in data:
            autoplay_val = bool(data['autoplay'])
            await services.guild_model.set_autoplay_enabled(guild_id, autoplay_val)
            for active_player in active_players:
                active_player.autoplay_enabled = autoplay_val
                music_cog = active_player.client.get_cog('Music')
                if music_cog:
                    if autoplay_val and active_player.current is not None:
                        music_cog._schedule_autoplay_prefetch(active_player, active_player.current)
                    elif not autoplay_val:
                        music_cog._clear_autoplay_prefetch(guild_id)

        # Autoplay preference preset (default is immutable; custom is the guild's own copy)
        if 'autoplay_preset' in data or 'autoplay_config' in data:
            from utils.autoplay import AUTOPLAY_PRESET_CUSTOM, AUTOPLAY_PRESETS

            preset = data.get('autoplay_preset')
            custom = data.get('autoplay_config')
            if preset is not None:
                normalized_preset = str(preset).strip().lower()
                if normalized_preset not in AUTOPLAY_PRESETS:
                    raise HTTPException(status_code=400, detail='autoplay_preset must be "default" or "custom".')
                if normalized_preset == AUTOPLAY_PRESET_CUSTOM:
                    base = custom if isinstance(custom, dict) else await services.guild_model.get_autoplay_custom_config(guild_id)
                    await services.guild_model.set_autoplay_custom_config(guild_id, base if isinstance(base, dict) else {})
                else:
                    await services.guild_model.set_autoplay_preset(guild_id, normalized_preset)
            elif isinstance(custom, dict):
                await services.guild_model.set_autoplay_custom_config(guild_id, custom)
            for active_player in active_players:
                music_cog = active_player.client.get_cog('Music')
                if music_cog and hasattr(music_cog, 'apply_autoplay_settings'):
                    try:
                        await music_cog.apply_autoplay_settings(active_player)
                        music_cog._clear_autoplay_prefetch(guild_id)
                        if active_player.current is not None:
                            music_cog._schedule_autoplay_prefetch(active_player, active_player.current)
                    except Exception:
                        pass

        if 'twenty_four_seven' in data or '247' in data:
            raw_stay = data.get('twenty_four_seven')
            if raw_stay is None:
                raw_stay = data.get('247')
            stay_val = bool(raw_stay)
            await services.guild_model.set_247_enabled(guild_id, stay_val)
            for active_player in active_players:
                active_player.twenty_four_seven = stay_val
                music_cog = active_player.client.get_cog('Music')
                if music_cog:
                    music_cog.cancel_timeout(guild_id)
                    alone = active_player.channel is not None and len(active_player.channel.members) == 1 and active_player.channel.members[0].id == active_player.client.user.id
                    if not stay_val and ((active_player.queue.is_empty and not active_player.is_playing) or alone):
                        music_cog.start_timeout(guild_id, active_player)

        # 5. Leveling Master & Alert Config / Level Channel
        if 'leveling_enabled' in data:
            await services.guild_model.set_leveling_enabled(guild_id, bool(data['leveling_enabled']))

        if 'level_alert_config' in data:
            cfg = data.get('level_alert_config')
            if isinstance(cfg, dict):
                raw_c = cfg.get('channel_id')
                if raw_c is not None and str(raw_c).lower() not in ('null', 'none', ''):
                    c_id = _parse_integer(raw_c, 'level_alert_config.channel_id', minimum=1)
                    ch = guild.get_channel(c_id)
                    if ch is None or not isinstance(ch, discord.TextChannel):
                        raise HTTPException(
                            status_code=400,
                            detail='level_alert_config.channel_id must identify a text channel in this server.',
                        )
                    cfg['channel_id'] = c_id
                else:
                    cfg['channel_id'] = None
                await services.guild_model.set_level_alert_config(guild_id, cfg)
        elif 'level_channel_id' in data:
            level_chan_id = data.get('level_channel_id')
            if level_chan_id is not None and level_chan_id != '' and str(level_chan_id).lower() not in ('null', 'none'):
                channel_id = _parse_integer(level_chan_id, 'level_channel_id', minimum=1)
                channel = guild.get_channel(channel_id)
                if channel is None or not isinstance(channel, discord.TextChannel):
                    raise HTTPException(
                        status_code=400,
                        detail='level_channel_id must identify a text channel in this server.',
                    )
                await services.guild_model.set_level_channel(guild_id, channel_id)
            else:
                await services.guild_model.remove_level_channel(guild_id)

        # 6. Social Alerts (Twitch / YouTube / X announcements).
        # New clients post `social_alerts` (list). Legacy clients post `stream_alerts` (dict).
        if 'social_alerts' in data:
            raw_social = data.get('social_alerts')
            if not isinstance(raw_social, list):
                raise HTTPException(status_code=400, detail='social_alerts must be a list.')
            if len(raw_social) > SOCIAL_ALERT_MAX_ENTRIES:
                raise HTTPException(
                    status_code=400,
                    detail=f'social_alerts must contain at most {SOCIAL_ALERT_MAX_ENTRIES} entries.',
                )
            for index, item in enumerate(raw_social):
                if not isinstance(item, dict):
                    raise HTTPException(status_code=400, detail=f'social_alerts[{index}] must be an object.')
                platform = str(item.get('platform', '') or '').strip().lower()
                if platform not in SOCIAL_ALERT_PLATFORMS:
                    raise HTTPException(
                        status_code=400,
                        detail=f'social_alerts[{index}].platform must be one of {", ".join(SOCIAL_ALERT_PLATFORMS)}.',
                    )
                raw_events = item.get('events', item.get('event_types', item.get('eventTypes')))
                if raw_events is not None:
                    if not isinstance(raw_events, list) or not raw_events:
                        raise HTTPException(status_code=400, detail=f'social_alerts[{index}].events must be a non-empty list.')
                    allowed = SOCIAL_ALERT_EVENTS[platform]
                    for event_name in raw_events:
                        if str(event_name or '').strip().lower() not in allowed:
                            raise HTTPException(
                                status_code=400,
                                detail=f'social_alerts[{index}].events must be a subset of {", ".join(allowed)}.',
                            )
            normalized_social = _normalize_social_alerts(raw_social)
            stored_guild = await services.guild_model.get_guild(guild_id)
            stored_social = (stored_guild or {}).get('social_alerts')
            if stored_social is None:
                stored_social = _social_alerts_from_legacy((stored_guild or {}).get('stream_alerts'))
            previous_mentions = {str(item.get('id')): (str(item.get('mentionRole') or 'none'), str(item.get('channelId') or '')) for item in _normalize_social_alerts(stored_social)}
            for entry in normalized_social:
                if entry['channelId'] is not None:
                    channel_id = _parse_integer(entry['channelId'], f'social_alerts.{entry["id"]}.channelId', minimum=1)
                    channel = guild.get_channel(channel_id)
                    if channel is None or not isinstance(channel, discord.TextChannel):
                        raise HTTPException(
                            status_code=400,
                            detail=f'social_alerts.{entry["id"]}.channelId must identify a text channel in this server.',
                        )
                    entry['channelId'] = str(channel_id)
                    entry['mentionRole'] = _check_alert_mention(
                        guild,
                        channel,
                        invoker_member,
                        entry['mentionRole'],
                        f'social_alerts.{entry["id"]}',
                        previous_mentions.get(str(entry['id'])),
                        entry['channelId'],
                    )
                elif entry['mentionRole'] not in ('none', 'everyone', 'here') and not entry['mentionRole'].isdigit():
                    raise HTTPException(status_code=400, detail=f'social_alerts.{entry["id"]}.mentionRole is invalid.')
            legacy_alerts = _legacy_stream_alerts_from_social(normalized_social)
            await services.guild_model.update_guild(guild_id, {'social_alerts': normalized_social, 'stream_alerts': legacy_alerts})
        elif 'stream_alerts' in data:
            alerts = data.get('stream_alerts')
            if not isinstance(alerts, dict):
                raise HTTPException(status_code=400, detail='stream_alerts must be an object.')
            normalized_alerts = _normalize_stream_alerts(alerts)
            stored_guild = await services.guild_model.get_guild(guild_id)
            stored_stream = _normalize_stream_alerts((stored_guild or {}).get('stream_alerts') or {})
            for platform, cfg in normalized_alerts.items():
                if cfg['channelId'] is not None:
                    channel_id = _parse_integer(cfg['channelId'], f'stream_alerts.{platform}.channelId', minimum=1)
                    channel = guild.get_channel(channel_id)
                    if channel is None or not isinstance(channel, discord.TextChannel):
                        raise HTTPException(
                            status_code=400,
                            detail=f'stream_alerts.{platform}.channelId must identify a text channel in this server.',
                        )
                    cfg['channelId'] = str(channel_id)
                    before = stored_stream.get(platform)
                    cfg['mentionRole'] = _check_alert_mention(
                        guild,
                        channel,
                        invoker_member,
                        cfg['mentionRole'],
                        f'stream_alerts.{platform}',
                        (str(before.get('mentionRole') or 'none'), str(before.get('channelId') or '')) if before else None,
                        cfg['channelId'],
                    )
            await services.guild_model.update_guild(guild_id, {'stream_alerts': normalized_alerts})

        # 5. Locale
        if 'locale' in data:
            locale = data.get('locale')
            if locale:
                if not await services.i18n.set_guild_locale(guild_id, str(locale)):
                    raise HTTPException(status_code=400, detail='Unsupported locale.')

        # 6. AutoRole
        if 'auto_role_id' in data:
            auto_role_id_raw = data.get('auto_role_id')
            auto_role_id = None
            if auto_role_id_raw and str(auto_role_id_raw).lower() not in ('none', 'null', ''):
                auto_role_id = _parse_integer(auto_role_id_raw, 'auto_role_id', minimum=1)
                role = guild.get_role(auto_role_id)
                if role is None:
                    raise HTTPException(status_code=400, detail='auto_role_id must identify a role in this server.')
                problem = services._validate_assignable_role(role, invoker_member)
                if problem:
                    raise HTTPException(status_code=400, detail=f'auto_role_id rejected: {problem}')
            await services.guild_model.update_guild(guild_id, {'auto_role_id': auto_role_id})

        # 7. Reaction Roles
        if 'reaction_roles' in data:
            reaction_roles = data.get('reaction_roles')
            if not isinstance(reaction_roles, dict):
                raise HTTPException(status_code=400, detail='reaction_roles must be an object.')

            stored_guild = await services.guild_model.get_guild(guild_id)
            stored_role_names = stored_guild.get('reaction_role_names', {}) if stored_guild else {}
            if not isinstance(stored_role_names, dict):
                stored_role_names = {}
            provided_role_names = data.get('reaction_role_names', {})
            if not isinstance(provided_role_names, dict):
                provided_role_names = {}
            stored_embed_settings = stored_guild.get('reaction_role_embeds', {}) if stored_guild else {}
            if not isinstance(stored_embed_settings, dict):
                stored_embed_settings = {}
            provided_embed_settings = data.get('reaction_role_embeds', {})
            if not isinstance(provided_embed_settings, dict):
                provided_embed_settings = {}
            stored_inline_settings = stored_guild.get('reaction_role_inline', {}) if stored_guild else {}
            if not isinstance(stored_inline_settings, dict):
                stored_inline_settings = {}
            provided_inline_settings = data.get('reaction_role_inline', {})
            if not isinstance(provided_inline_settings, dict):
                provided_inline_settings = {}
            normalized_reaction_roles = {}
            reaction_role_names = {}
            reaction_role_embeds = {}
            reaction_role_inline = {}
            skipped_reaction_roles: list[str] = []
            for message_id, emoji_roles in reaction_roles.items():
                from utils.reaction_role_access import resolve_reaction_role_message

                message = await resolve_reaction_role_message(services.bot_instance, guild, message_id, invoker_member)
                if message is None:
                    raise HTTPException(status_code=403, detail='Reaction-role message is not accessible in this server.')
                if not isinstance(emoji_roles, dict):
                    continue
                normalized_emoji_roles = {}
                for emoji, raw_role_id in emoji_roles.items():
                    try:
                        role_id = str(_parse_integer(raw_role_id, 'role_id', minimum=1))
                    except HTTPException:
                        skipped_reaction_roles.append(f'{emoji} -> {raw_role_id}')
                        continue
                    role = guild.get_role(int(role_id))
                    if role is None:
                        skipped_reaction_roles.append(f'{emoji} -> {role_id} (missing)')
                        continue
                    problem = services._validate_assignable_role(role, invoker_member)
                    if problem:
                        skipped_reaction_roles.append(f'{emoji} -> {role.name} ({problem})')
                        continue
                    normalized_emoji_roles[str(emoji)] = int(role_id)
                    role_name = role.name or provided_role_names.get(role_id) or stored_role_names.get(role_id)
                    if role_name:
                        reaction_role_names[role_id] = str(role_name)
                message_key = str(message_id)
                normalized_reaction_roles[message_key] = normalized_emoji_roles

                provided_embed = provided_embed_settings.get(message_key, {})
                stored_embed = stored_embed_settings.get(message_key, {})
                if not isinstance(provided_embed, dict):
                    provided_embed = {}
                if not isinstance(stored_embed, dict):
                    stored_embed = {}
                embed_settings = {}
                for field in ('title', 'description', 'image_url', 'thumbnail_url', 'footer_text', 'footer_url'):
                    if field in provided_embed:
                        embed_settings[field] = str(provided_embed[field] or '')
                    elif field in stored_embed:
                        embed_settings[field] = str(stored_embed[field] or '')
                if embed_settings:
                    reaction_role_embeds[message_key] = embed_settings

                provided_inline = provided_inline_settings.get(message_key, {})
                stored_inline = stored_inline_settings.get(message_key, {})
                if not isinstance(provided_inline, dict):
                    provided_inline = {}
                if not isinstance(stored_inline, dict):
                    stored_inline = {}
                reaction_role_inline[message_key] = {emoji: bool(provided_inline[emoji]) if emoji in provided_inline else bool(stored_inline.get(emoji, True)) for emoji in normalized_emoji_roles}

            if skipped_reaction_roles:
                logger.warning(
                    '[INTERNAL API] Skipped %d non-assignable reaction role(s) for guild %s: %s',
                    len(skipped_reaction_roles),
                    guild_id,
                    ', '.join(skipped_reaction_roles),
                )

            await services.guild_model.update_guild(
                guild_id,
                {
                    'reaction_roles': normalized_reaction_roles,
                    'reaction_role_names': reaction_role_names,
                    'reaction_role_embeds': reaction_role_embeds,
                    'reaction_role_inline': reaction_role_inline,
                },
            )

            for b in getattr(services.bot_instance, 'all_bots', [services.bot_instance]):
                rr_cog = b.get_cog('ReactionRolesCog')
                if rr_cog:
                    rr_cog.reaction_roles[guild_id] = {}
                    for msg_id_str, emoji_roles in normalized_reaction_roles.items():
                        msg_id = int(msg_id_str)
                        rr_cog.reaction_roles[guild_id][msg_id] = {}
                        for emoji, role_id in emoji_roles.items():
                            rr_cog.reaction_roles[guild_id][msg_id][emoji] = role_id

                    async def sync_messages(cog, gid, r_roles):
                        guild = cog.bot.get_guild(gid)
                        if not guild:
                            return
                        for m_id_str, e_roles in r_roles.items():
                            m_id = int(m_id_str)
                            for ch in guild.text_channels:
                                try:
                                    msg = await ch.fetch_message(m_id)
                                    from utils.reaction_role_access import can_read_message

                                    if msg and can_read_message(msg, guild, guild.get_member(int(user_id))):
                                        await cog._update_message_embed(msg, gid, m_id)
                                        for emoji_str in e_roles.keys():
                                            if emoji_str.startswith('<'):
                                                match = re.match(r'<a?:.+?:(\d+)>', emoji_str)
                                                if match:
                                                    e_id = int(match.group(1))
                                                    custom_emoji = cog.bot.get_emoji(e_id)
                                                    if custom_emoji:
                                                        await msg.add_reaction(custom_emoji)
                                            else:
                                                await msg.add_reaction(emoji_str)
                                        break
                                except discord.HTTPException:
                                    pass

                    asyncio.create_task(sync_messages(rr_cog, guild_id, normalized_reaction_roles))

        if 'music_channel_id' in data or 'music_message_id' in data:
            # Channel changes must reach Discord even when no playback event
            # occurs. One cog refreshes the shared directory through its owner.
            primary_bot = services.bot_instance
            for bot in getattr(primary_bot, 'all_bots', [primary_bot]):
                if bot is None or bot.get_guild(guild_id) is None:
                    continue
                music_cog = bot.get_cog('Music')
                if music_cog:
                    try:
                        await music_cog._update_static_embed(guild_id)
                    except Exception:
                        logger.exception('Failed to refresh music directory after settings save for guild %s', guild_id)
                    break

        return {'success': True}

    return {
        'internal_resolve_autoplay_channel': internal_resolve_autoplay_channel,
        'internal_get_guild_settings': internal_get_guild_settings,
        'internal_save_guild_settings': internal_save_guild_settings,
    }
