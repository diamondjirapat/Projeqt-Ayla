"""Leveling internal HTTP endpoints."""

from collections.abc import Callable
from typing import Any
from fastapi import FastAPI, HTTPException, Header, Query
from api.dependencies.leveling import LevelingDependencies
from api.dependencies.validation import validate_dependencies
from config import Config
from utils.discord_profile import enrich_leaderboard_profiles


def register_routes(
    app: FastAPI,
    provide: Callable[[], LevelingDependencies],
) -> dict[str, Callable[..., Any]]:
    validate_dependencies(provide(), LevelingDependencies)

    @app.get('/internal/leveling/guilds')
    async def internal_list_leveling_guilds(
        user_id: int = Header(..., alias='X-User-Id'),
    ):
        services = provide()
        is_admin = int(user_id) in Config.OWNER_IDS

        guilds_list = []
        seen_ids = set()
        if services.bot_instance:
            for b in getattr(services.bot_instance, 'all_bots', [services.bot_instance]):
                for g in getattr(b, 'guilds', []):
                    if g.id in seen_ids:
                        continue
                    if not is_admin and g.get_member(user_id) is None:
                        continue
                    seen_ids.add(g.id)
                    icon_url = str(g.icon.url) if g.icon else ''
                    guilds_list.append({'id': str(g.id), 'name': g.name, 'icon': icon_url})
        guilds_list.sort(key=lambda x: x['name'].lower())
        return guilds_list

    @app.get('/internal/leveling/leaderboard/global')
    async def internal_get_global_leaderboard(
        user_id: int = Header(..., alias='X-User-Id'),
        page: int = Query(1, ge=1),
        limit: int = Query(10, ge=1, le=50),
    ):
        services = provide()
        try:
            data = await services.leveling_model.get_global_leaderboard(page=page, limit=limit)
            target_bot = services.bot_instance or services.bot
            if target_bot and data.get('entries'):
                await enrich_leaderboard_profiles(target_bot, data['entries'], guild_id=None)
            return data
        except RuntimeError:
            return {'entries': [], 'page': page, 'limit': limit, 'total': 0}

    @app.get('/internal/leveling/leaderboard/server/{guild_id}')
    async def internal_get_server_leaderboard(
        guild_id: int,
        user_id: int = Header(..., alias='X-User-Id'),
        page: int = Query(1, ge=1),
        limit: int = Query(10, ge=1, le=50),
    ):
        services = provide()
        if int(user_id) not in Config.OWNER_IDS and services._get_guild_member(guild_id, int(user_id)) is None:
            raise HTTPException(status_code=403, detail='You are not a member of this server.')

        data = await services.leveling_model.get_guild_leaderboard(guild_id=guild_id, page=page, limit=limit)
        guild = services._find_guild(guild_id)
        if guild:
            data['guild_name'] = guild.name
            data['guild_icon'] = str(guild.icon.url) if guild.icon else ''
        target_bot = services.bot_instance or services.bot
        if target_bot and data.get('entries'):
            await enrich_leaderboard_profiles(target_bot, data['entries'], guild_id=guild_id)
        return data

    return {
        'internal_list_leveling_guilds': internal_list_leveling_guilds,
        'internal_get_global_leaderboard': internal_get_global_leaderboard,
        'internal_get_server_leaderboard': internal_get_server_leaderboard,
    }
