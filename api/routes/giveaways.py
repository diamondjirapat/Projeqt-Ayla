"""Giveaways internal HTTP endpoints."""

from collections.abc import Callable
from typing import Any
from fastapi import FastAPI, Body, HTTPException, Header, Query
from api.dependencies.giveaways import GiveawaysDependencies
from api.dependencies.validation import validate_dependencies
from config import Config
from datetime import UTC, datetime
from services.serialization import _serialize_giveaway
from services.giveaway_creation import create_giveaway_safely
from database.giveaways import is_giveaway_closed
from utils.web_validation import parse_integer as _parse_integer
import logging

logger = logging.getLogger(__name__)


def _member_can_view_channel(bot, guild_id: int, channel_id: Any, member: Any) -> bool:
    """True only when the giveaway channel resolves inside the guild and the member can view it.

    Any of the bots may be the one that sees the channel, so every registered bot is consulted.
    """
    if bot is None or member is None:
        return False
    try:
        wanted = int(channel_id or 0)
    except (TypeError, ValueError):
        return False
    for candidate in getattr(bot, 'all_bots', None) or [bot]:
        channel = candidate.get_channel(wanted)
        if channel is None or getattr(getattr(channel, 'guild', None), 'id', None) != int(guild_id):
            continue
        try:
            return bool(channel.permissions_for(member).view_channel)
        except Exception:
            return False
    return False


def register_routes(
    app: FastAPI,
    provide: Callable[[], GiveawaysDependencies],
) -> dict[str, Callable[..., Any]]:
    validate_dependencies(provide(), GiveawaysDependencies)

    @app.get('/internal/guild/giveaways')
    async def internal_get_giveaways(
        user_id: int = Header(..., alias='X-User-Id'),
        guild_id: int = Query(...),
        include_ended: bool = True,
    ):
        services = provide()
        can_manage = await services.check_user_guild_permission(user_id, guild_id)
        member = None if can_manage else services._get_guild_member(guild_id, int(user_id))
        if not can_manage and member is None:
            raise HTTPException(status_code=403, detail='You are not a member of this server.')

        giveaways = await services.giveaway_model.get_guild_giveaways(guild_id, include_ended=include_ended)

        serialized = []
        for g in giveaways:
            if not can_manage and not _member_can_view_channel(
                services.bot_instance, guild_id, g.get('channel_id'), member
            ):
                continue
            item = _serialize_giveaway(g)
            item['user_entered'] = int(user_id) in g.get('entries', [])
            item['entries_count'] = len(g.get('entries', []))
            if not can_manage:
                item.pop('entries', None)
            serialized.append(item)

        return {'giveaways': serialized, 'can_manage': can_manage}

    @app.post('/internal/guild/giveaways/enter')
    async def internal_enter_giveaway(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        message_id = _parse_integer(data.get('message_id'), 'message_id')
        giveaway = await services.giveaway_model.get_giveaway(message_id)
        if not giveaway:
            raise HTTPException(status_code=404, detail='Giveaway not found.')
        if is_giveaway_closed(giveaway):
            raise HTTPException(status_code=400, detail='Giveaway has already ended.')

        giveaway_guild_id = giveaway.get('guild_id')
        if int(user_id) not in Config.OWNER_IDS:
            member = services._get_guild_member(int(giveaway_guild_id), int(user_id)) if giveaway_guild_id else None
            if member is None:
                raise HTTPException(status_code=403, detail='You are not a member of this server.')
            if not _member_can_view_channel(
                services.bot_instance, int(giveaway_guild_id), giveaway.get('channel_id'), member
            ) and not await services.check_user_guild_permission(user_id, int(giveaway_guild_id)):
                raise HTTPException(status_code=403, detail='You cannot access this giveaway.')

        # Same toggle as the Discord button: add_entry is atomic, so only fall back to leaving
        # when the add did nothing because the user is already entered. Reading first and then
        # choosing add/remove would let two quick requests enter and immediately leave.
        if await services.giveaway_model.add_entry(message_id, int(user_id)):
            entered = True
        else:
            fresh = await services.giveaway_model.get_giveaway(message_id)
            if not fresh:
                raise HTTPException(status_code=404, detail='Giveaway not found.')
            if is_giveaway_closed(fresh):
                raise HTTPException(status_code=400, detail='Giveaway has already ended.')
            if int(user_id) in (fresh.get('entries') or []):
                await services.giveaway_model.remove_entry(message_id, int(user_id))
            refreshed = await services.giveaway_model.get_giveaway(message_id)
            entered = bool(refreshed) and int(user_id) in (refreshed.get('entries') or [])

        channel_id = giveaway.get('channel_id')
        primary_bot = services.bot_instance
        bots = getattr(primary_bot, 'all_bots', [primary_bot]) if primary_bot else []
        # The giveaway may have been posted by any of the bots; edit it through one that can see it.
        for candidate in bots:
            channel = candidate.get_channel(channel_id) if channel_id else None
            cog = candidate.get_cog('Giveaway') if channel else None
            if not cog:
                continue
            try:
                msg = await channel.fetch_message(message_id)
                updated = await services.giveaway_model.get_giveaway(message_id)
                if updated and not updated.get('ended', False):
                    embed = await cog.build_giveaway_embed(updated, channel)
                    await msg.edit(embed=embed)
            except Exception as e:
                logger.debug(f'Failed to update giveaway embed: {e}')
            break

        updated = await services.giveaway_model.get_giveaway(message_id)
        return {'success': True, 'entered': entered, 'entries_count': len(updated.get('entries', [])) if updated else 0}

    @app.post('/internal/guild/giveaways')
    async def internal_create_giveaway(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
        idempotency_key: str | None = Header(None, alias='Idempotency-Key'),
    ):
        services = provide()
        guild_id = _parse_integer(data.get('guild_id'), 'guild_id')
        can_manage = await services.check_user_guild_permission(user_id, guild_id)
        if not can_manage:
            raise HTTPException(status_code=403, detail='Permission denied to manage giveaways in this server.')

        channel_id = _parse_integer(data.get('channel_id'), 'channel_id')
        prize = str(data.get('prize', '')).strip()
        if not prize:
            raise HTTPException(status_code=400, detail='Prize description is required.')
        if len(prize) > 1000:
            raise HTTPException(status_code=400, detail='Prize description must be 1000 characters or fewer.')

        winners_count = _parse_integer(data.get('winners_count', 1), 'winners_count', minimum=1, maximum=50)
        duration_str = str(data.get('duration', '1h')).strip()

        raw_embed = data.get('embed') or {}
        if not isinstance(raw_embed, dict):
            raise HTTPException(status_code=400, detail='embed must be an object.')
        raw_embed = dict(raw_embed)
        title = str(data.get('title', raw_embed.get('title', '')) or '').strip()
        if title:
            raw_embed['title'] = title
        try:
            embed_config = services.normalize_giveaway_embed_config(raw_embed, strict=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        td = services.parse_duration(duration_str)
        if not td:
            raise HTTPException(status_code=400, detail='Invalid duration format. Example: 30m, 2h, 1d')

        end_time = datetime.now(UTC) + td
        extra_data = {}
        if title:
            extra_data['title'] = title
        if embed_config:
            extra_data['embed'] = embed_config

        if not services.bot_instance:
            raise HTTPException(status_code=503, detail='Bot is not ready.')

        channel = services.bot_instance.get_channel(channel_id)
        if not channel:
            try:
                channel = await services.bot_instance.fetch_channel(channel_id)
            except Exception:
                raise HTTPException(status_code=404, detail='Discord channel not found.')

        if getattr(channel, 'guild', None) is None or channel.guild.id != guild_id:
            raise HTTPException(status_code=400, detail='channel_id does not belong to the specified server.')

        cog = services.bot_instance.get_cog('Giveaway')
        if not cog:
            raise HTTPException(status_code=500, detail='Giveaway system cog not loaded.')

        dummy_giveaway = {
            'prize': prize,
            'host_id': int(user_id),
            'winners_count': winners_count,
            'end_time': end_time,
            'entries': [],
            'extra_data': extra_data,
        }

        embed = await cog.build_giveaway_embed(dummy_giveaway, channel)
        view = services.create_giveaway_view()

        giveaway_doc = await create_giveaway_safely(
            channel,
            embed,
            view,
            services.giveaway_model,
            services.giveaway_creation_model,
            {
                'guild_id': guild_id,
                'channel_id': channel_id,
                'host_id': int(user_id),
                'prize': prize,
                'winners_count': winners_count,
                'end_time': end_time,
                'extra_data': extra_data,
            },
            data,
            idempotency_key,
        )
        return {'success': True, 'giveaway': giveaway_doc}

    @app.post('/internal/guild/giveaways/end')
    async def internal_end_giveaway(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        message_id = _parse_integer(data.get('message_id'), 'message_id')
        giveaway = await services.giveaway_model.get_giveaway(message_id)
        if not giveaway:
            raise HTTPException(status_code=404, detail='Giveaway not found.')

        can_manage = await services.check_user_guild_permission(user_id, giveaway['guild_id'])
        if not can_manage:
            raise HTTPException(status_code=403, detail='Permission denied to manage giveaways.')

        if giveaway.get('ended', False):
            raise HTTPException(status_code=400, detail='Giveaway is already ended.')

        if not services.bot_instance:
            raise HTTPException(status_code=503, detail='Bot is not ready.')

        cog = services.bot_instance.get_cog('Giveaway')
        if cog:
            await cog.finalize_giveaway(giveaway, actor_id=user_id, source='web')
        else:
            entries = giveaway.get('entries', [])
            winners_count = giveaway.get('winners_count', 1)
            import random

            unique_entries = list(set(entries))
            winners = random.sample(unique_entries, min(winners_count, len(unique_entries))) if unique_entries else []
            await services.giveaway_model.end_giveaway(message_id, winners)

        return {'success': True}

    @app.post('/internal/guild/giveaways/reroll')
    async def internal_reroll_giveaway(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        message_id = _parse_integer(data.get('message_id'), 'message_id')
        winners_count = _parse_integer(data.get('winners_count', 1), 'winners_count', minimum=1, maximum=50)
        giveaway = await services.giveaway_model.get_giveaway(message_id)
        if not giveaway:
            raise HTTPException(status_code=404, detail='Giveaway not found.')

        can_manage = await services.check_user_guild_permission(user_id, giveaway['guild_id'])
        if not can_manage:
            raise HTTPException(status_code=403, detail='Permission denied to reroll giveaways.')

        entries = giveaway.get('entries', [])
        if not entries:
            raise HTTPException(status_code=400, detail='No entries in this giveaway to pick winners from.')

        import random

        unique_entries = list(set(entries))
        new_winners = random.sample(unique_entries, min(winners_count, len(unique_entries)))
        await services.giveaway_model.set_winners(message_id, new_winners)

        if services.bot_instance:
            cog = services.bot_instance.get_cog('Giveaway')
            channel_id = giveaway.get('channel_id')
            channel = services.bot_instance.get_channel(channel_id)
            if channel and cog:
                try:
                    orig_msg = await channel.fetch_message(message_id)
                    if orig_msg:
                        ended_embed = await cog.build_giveaway_embed(giveaway, channel, ended=True, winners=new_winners)
                        await orig_msg.edit(embed=ended_embed)
                        winner_mentions = ', '.join(f'<@{w}>' for w in new_winners)
                        await channel.send(
                            f'Giveaway Rerolled! New winner(s): {winner_mentions} '
                            f'for **{giveaway.get("prize", "Prize")}**!',
                            reference=orig_msg.to_reference(fail_if_not_exists=False),
                        )
                except Exception as e:
                    logger.debug(f'Failed to update message on reroll: {e}')

        return {'success': True, 'winners': new_winners}

    @app.post('/internal/guild/giveaways/delete')
    async def internal_delete_giveaway(
        user_id: int = Header(..., alias='X-User-Id'),
        data: dict = Body(...),
    ):
        services = provide()
        message_id = _parse_integer(data.get('message_id'), 'message_id')
        giveaway = await services.giveaway_model.get_giveaway(message_id)
        if not giveaway:
            raise HTTPException(status_code=404, detail='Giveaway not found.')

        can_manage = await services.check_user_guild_permission(user_id, giveaway['guild_id'])
        if not can_manage:
            raise HTTPException(status_code=403, detail='Permission denied to delete giveaways.')

        success = await services.giveaway_model.delete_giveaway(message_id)
        return {'success': success}

    return {
        'internal_get_giveaways': internal_get_giveaways,
        'internal_enter_giveaway': internal_enter_giveaway,
        'internal_create_giveaway': internal_create_giveaway,
        'internal_end_giveaway': internal_end_giveaway,
        'internal_reroll_giveaway': internal_reroll_giveaway,
        'internal_delete_giveaway': internal_delete_giveaway,
    }
