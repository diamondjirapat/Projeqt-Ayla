"""Leveling persistence and domain rules."""

from datetime import UTC, datetime
from typing import Optional, Dict, Any
import asyncio
import logging
import math
import weakref

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database.base import BaseModel
from database.connection import db_manager
from utils.cache import AsyncTTLCache

logger = logging.getLogger(__name__)
_global_caches = weakref.WeakSet()

GLOBAL_LEVELS_COLLECTION = 'user_global_levels'
_global_rebuild_task: Optional[asyncio.Task] = None


def calculate_level(xp: int) -> int:
    """Calculate level from total XP using 50 * L^2 + 100 * L = XP."""
    if xp <= 0:
        return 0
    return math.isqrt(1 + xp // 50) - 1


def xp_for_level(level: int) -> int:
    """Calculate total cumulative XP required to reach level."""
    if level <= 0:
        return 0
    return int(50 * (level**2) + 100 * level)


def get_level_progress(xp: int) -> Dict[str, Any]:
    """Calculate detailed level progress stats for given XP."""
    level = calculate_level(xp)
    current_level_xp = xp_for_level(level)
    next_level_xp = xp_for_level(level + 1)

    xp_in_level = xp - current_level_xp
    xp_needed = next_level_xp - current_level_xp
    progress_pct = (xp_in_level / xp_needed * 100.0) if xp_needed > 0 else 0.0
    progress_pct = min(100.0, max(0.0, progress_pct))

    return {
        'level': level,
        'xp': xp,
        'current_level_xp': current_level_xp,
        'next_level_xp': next_level_xp,
        'xp_in_level': xp_in_level,
        'xp_needed_for_next': xp_needed,
        'progress_pct': round(progress_pct, 1),
    }


class LevelingModel(BaseModel):
    """Per-guild XP in ``user_levels`` plus a per-user running total in ``user_global_levels``.

    The global collection lets rank and leaderboard queries use an index instead of
    grouping every guild record. ``rebuild_global_totals`` recomputes it from
    ``user_levels`` (the source of truth) and runs once per process at startup.
    """

    def __init__(self):
        super().__init__('user_levels')
        # Global totals may be up to 15 seconds old, including routine XP awards.
        # Only explicit XP corrections invalidate early, preserving reuse during chat activity.
        self._global_cache = AsyncTTLCache(ttl=15, max_size=128, name='leveling.global')
        _global_caches.add(self._global_cache)

    @property
    def global_collection(self):
        injected = getattr(self, '_global_collection', None)
        if injected is not None:
            return injected
        return db_manager.get_collection(GLOBAL_LEVELS_COLLECTION)

    async def rebuild_global_totals(self) -> None:
        """Atomically replace the global totals with a fresh aggregate of ``user_levels``.

        ``$out`` swaps the target collection in one step and keeps its indexes, so readers
        never observe a partially built collection.
        """
        pipeline = [
            {
                '$group': {
                    '_id': '$user_id',
                    'user_id': {'$first': '$user_id'},
                    'xp': {'$sum': '$xp'},
                    'messages_count': {'$sum': '$messages_count'},
                    'guilds_count': {'$sum': 1},
                }
            },
            {'$project': {'_id': 0}},
            {'$out': GLOBAL_LEVELS_COLLECTION},
        ]
        cursor = await self.collection.aggregate(pipeline)
        await cursor.to_list(length=None)
        for cache in tuple(_global_caches):
            cache.clear()

    async def ensure_global_totals(self) -> None:
        """Rebuild global totals once per process; concurrent callers share the same run."""
        global _global_rebuild_task
        task = _global_rebuild_task
        loop = asyncio.get_running_loop()
        failed = task is not None and task.done() and (task.cancelled() or task.exception() is not None)
        if task is None or failed or (not task.done() and task.get_loop() is not loop):
            task = _global_rebuild_task = loop.create_task(self.rebuild_global_totals())
        await asyncio.shield(task)

    async def _increment_global(self, user_id: int, xp: int, messages: int, guilds: int) -> None:
        mutation = {'$inc': {'xp': xp, 'messages_count': messages, 'guilds_count': guilds}}
        try:
            await self.global_collection.update_one({'user_id': user_id}, mutation, upsert=True)
        except DuplicateKeyError:
            # A concurrent first award inserted the unique user document first.
            await self.global_collection.update_one({'user_id': user_id}, mutation)
        except Exception:
            # user_levels stays authoritative; the startup rebuild repairs any drift.
            logger.exception('Failed to update global XP totals for user %s', user_id)

    async def _global_rank(self, xp: int) -> int:
        async def load():
            higher = await self.global_collection.count_documents({'xp': {'$gt': xp}})
            return higher + 1

        return await self._global_cache.get_or_load(('rank', xp), load)

    async def add_xp(
        self,
        guild_id: int,
        user_id: int,
        xp_amount: int,
        username: Optional[str] = None,
        avatar_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Add XP to user in a specific guild and check for level up."""
        now = datetime.now(UTC)
        identity = {'guild_id': guild_id, 'user_id': user_id}
        mutation = {
            '$set': {**identity, 'updated_at': now, 'last_xp_at': now},
            '$inc': {'xp': xp_amount, 'messages_count': 1},
            '$setOnInsert': {'created_at': now},
            # Level is always derived from XP; drop the legacy stored copy so it cannot go stale.
            '$unset': {'level': ''},
        }
        inserted = False
        try:
            # The pre-image is atomic: it gives the exact XP before this award and
            # reveals whether this call created the guild record.
            before = await self.collection.find_one_and_update(
                identity, mutation, upsert=True, return_document=ReturnDocument.BEFORE
            )
            inserted = before is None
        except DuplicateKeyError:
            # A concurrent first award may have inserted the unique guild/user pair.
            before = await self.collection.find_one_and_update(
                identity, mutation, return_document=ReturnDocument.BEFORE
            )
            if before is None:
                raise RuntimeError('Leveling record disappeared during XP award.')
        old_xp = (before or {}).get('xp', 0)
        new_xp = old_xp + xp_amount
        await self._increment_global(user_id, xp_amount, 1, 1 if inserted else 0)
        old_level = calculate_level(old_xp)
        new_level = calculate_level(new_xp)
        return {
            'leveled_up': new_level > old_level,
            'old_level': old_level,
            'new_level': new_level,
            'xp': new_xp,
            'guild_id': guild_id,
            'user_id': user_id,
        }

    async def set_xp(
        self,
        guild_id: int,
        user_id: int,
        total_xp: int,
        username: Optional[str] = None,
        avatar_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Set total XP for user in a guild."""
        total_xp = max(0, total_xp)
        new_level = calculate_level(total_xp)
        now = datetime.now(UTC)
        identity = {'guild_id': guild_id, 'user_id': user_id}
        mutation = {
            '$set': {**identity, 'xp': total_xp, 'updated_at': now},
            '$setOnInsert': {'messages_count': 0, 'created_at': now},
            '$unset': {'level': ''},
        }
        try:
            before = await self.collection.find_one_and_update(
                identity, mutation, upsert=True, return_document=ReturnDocument.BEFORE
            )
            inserted = before is None
        except DuplicateKeyError:
            before = await self.collection.find_one_and_update(
                identity, mutation, return_document=ReturnDocument.BEFORE
            )
            inserted = False
        delta = total_xp - (before or {}).get('xp', 0)
        await self._increment_global(user_id, delta, 0, 1 if inserted else 0)

        for cache in tuple(_global_caches):
            cache.clear()

        return {'guild_id': guild_id, 'user_id': user_id, 'xp': total_xp, 'level': new_level}

    async def get_user_stats(self, guild_id: int, user_id: int) -> Dict[str, Any]:
        """Get comprehensive leveling stats for a user in a guild and globally."""
        guild_doc = await self.collection.find_one({'guild_id': guild_id, 'user_id': user_id})
        guild_xp = guild_doc.get('xp', 0) if guild_doc else 0
        guild_level = calculate_level(guild_xp)
        guild_messages = guild_doc.get('messages_count', 0) if guild_doc else 0
        username = guild_doc.get('username', 'Unknown User') if guild_doc else 'Unknown User'
        avatar_url = guild_doc.get('avatar_url', '') if guild_doc else ''

        guild_rank = 1
        if guild_doc and guild_xp > 0:
            higher_guild = await self.collection.count_documents({'guild_id': guild_id, 'xp': {'$gt': guild_xp}})
            guild_rank = higher_guild + 1

        pipeline = [
            {'$match': {'user_id': user_id}},
            {
                '$group': {
                    '_id': '$user_id',
                    'global_xp': {'$sum': '$xp'},
                    'global_messages': {'$sum': '$messages_count'},
                }
            },
        ]
        agg_result = await (await self.collection.aggregate(pipeline)).to_list(length=1)
        global_xp = agg_result[0]['global_xp'] if agg_result else 0
        global_messages = agg_result[0]['global_messages'] if agg_result else 0
        global_level = calculate_level(global_xp)

        global_rank = 1
        if global_xp > 0:
            global_rank = await self._global_rank(global_xp)

        guild_progress = get_level_progress(guild_xp)
        global_progress = get_level_progress(global_xp)

        return {
            'user_id': user_id,
            'guild_id': guild_id,
            'username': username,
            'avatar_url': avatar_url,
            'guild_rank': guild_rank,
            'guild_xp': guild_xp,
            'guild_level': guild_level,
            'guild_messages': guild_messages,
            'guild_progress': guild_progress,
            'global_rank': global_rank,
            'global_xp': global_xp,
            'global_level': global_level,
            'global_messages': global_messages,
            'global_progress': global_progress,
        }

    async def get_guild_leaderboard(self, guild_id: int, page: int = 1, limit: int = 10) -> Dict[str, Any]:
        """Get paginated server leaderboard for a specific guild."""
        skip = (max(1, page) - 1) * limit
        total_count = await self.collection.count_documents({'guild_id': guild_id})

        cursor = self.collection.find({'guild_id': guild_id}).sort('xp', -1).skip(skip).limit(limit)
        docs = await cursor.to_list(length=limit)

        entries = []
        for idx, doc in enumerate(docs):
            rank = skip + idx + 1
            xp = doc.get('xp', 0)
            level = calculate_level(xp)
            progress = get_level_progress(xp)
            entries.append(
                {
                    'rank': rank,
                    'user_id': doc.get('user_id'),
                    'username': doc.get('username', 'Unknown User'),
                    'avatar_url': doc.get('avatar_url', ''),
                    'xp': xp,
                    'level': level,
                    'messages_count': doc.get('messages_count', 0),
                    'progress': progress,
                }
            )

        total_pages = max(1, math.ceil(total_count / limit))
        return {'entries': entries, 'total': total_count, 'page': page, 'limit': limit, 'total_pages': total_pages}

    async def get_global_leaderboard(self, page: int = 1, limit: int = 10) -> Dict[str, Any]:
        """Get paginated global leaderboard across all servers."""
        skip = (max(1, page) - 1) * limit

        async def load():
            total = await self.global_collection.count_documents({})
            cursor = self.global_collection.find({}).sort([('xp', -1), ('user_id', 1)]).skip(skip).limit(limit)
            data = await cursor.to_list(length=limit)
            return [{'metadata': [{'total': total}], 'data': data}]

        result = await self._global_cache.get_or_load(('leaderboard', skip, limit), load)

        total_count = 0
        raw_data = []
        if result:
            meta = result[0].get('metadata', [])
            if meta:
                total_count = meta[0].get('total', 0)
            raw_data = result[0].get('data', [])

        entries = []
        for idx, doc in enumerate(raw_data):
            rank = skip + idx + 1
            xp = doc.get('xp', 0)
            level = calculate_level(xp)
            progress = get_level_progress(xp)
            entries.append(
                {
                    'rank': rank,
                    'user_id': doc.get('user_id'),
                    'username': doc.get('username', 'Unknown User'),
                    'avatar_url': doc.get('avatar_url', ''),
                    'xp': xp,
                    'level': level,
                    'messages_count': doc.get('messages_count', 0),
                    'guilds_count': doc.get('guilds_count', 1),
                    'progress': progress,
                }
            )

        total_pages = max(1, math.ceil(total_count / limit))
        return {'entries': entries, 'total': total_count, 'page': page, 'limit': limit, 'total_pages': total_pages}
