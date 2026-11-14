"""Moderation persistence and domain rules."""

from datetime import UTC, datetime, timedelta
from typing import Optional, Dict, Any
import logging
import secrets
from pymongo import ReturnDocument
from database.connection import db_manager
from utils.security import redact_sensitive

from database.base import BaseModel

logger = logging.getLogger(__name__)


class ModerationCaseModel(BaseModel):
    """Sequential, server-scoped moderation cases with revision history."""

    ACTIONS = frozenset({'warn', 'note', 'timeout', 'kick', 'ban', 'unban'})
    STATUSES = frozenset({'active', 'revoked', 'expired'})

    def __init__(self):
        super().__init__('moderation_cases')
        self.counter_collection_name = 'moderation_counters'

    @property
    def counter_collection(self):
        return db_manager.get_collection(self.counter_collection_name)

    async def _next_case_id(self, guild_id: int) -> int:
        counter = await self.counter_collection.find_one_and_update(
            {'guild_id': int(guild_id)},
            {'$inc': {'next_case_id': 1}, '$setOnInsert': {'guild_id': int(guild_id)}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(counter['next_case_id'])

    async def create_case(
        self,
        guild_id: int,
        action: str,
        target_user_id: int,
        moderator_user_id: int,
        *,
        reason: str = '',
        internal_note: str = '',
        duration_seconds: Optional[int] = None,
        expires_at: Any = None,
        target_snapshot: Optional[Dict[str, Any]] = None,
        moderator_snapshot: Optional[Dict[str, Any]] = None,
        evidence: Optional[list[str]] = None,
        points: int = 0,
        source: str = 'discord',
        retention_days: int = 3650,
    ) -> Dict[str, Any]:
        if action not in self.ACTIONS:
            raise ValueError(f'Unsupported moderation action: {action}')
        now = datetime.now(UTC)
        case = {
            'guild_id': int(guild_id),
            'case_id': await self._next_case_id(guild_id),
            'action': action,
            'status': 'active',
            'target_user_id': int(target_user_id),
            'moderator_user_id': int(moderator_user_id),
            'target_snapshot': redact_sensitive(target_snapshot or {}),
            'moderator_snapshot': redact_sensitive(moderator_snapshot or {}),
            'reason': str(reason or '')[:1024],
            'internal_note': str(internal_note or '')[:4096],
            'duration_seconds': int(duration_seconds) if duration_seconds is not None else None,
            'expires_at': expires_at,
            'evidence': [str(item)[:2048] for item in (evidence or [])[:20]],
            'points': max(0, min(100, int(points))),
            'source': source,
            'data_expires_at': now + timedelta(days=max(30, min(3650, int(retention_days)))),
            'revisions': [
                {
                    'actor_id': int(moderator_user_id),
                    'action': 'create',
                    'changes': redact_sensitive({'action': action, 'reason': reason}),
                    'created_at': now,
                }
            ],
            'created_at': now,
            'updated_at': now,
        }
        await self.collection.insert_one(case)
        return case

    async def get_case(self, guild_id: int, case_id: int) -> Optional[Dict[str, Any]]:
        return await self.collection.find_one({'guild_id': int(guild_id), 'case_id': int(case_id)})

    async def list_cases(
        self,
        guild_id: int,
        *,
        target_user_id: Optional[int] = None,
        moderator_user_id: Optional[int] = None,
        action: Optional[str] = None,
        status: Optional[str] = None,
        case_id: Optional[int] = None,
        created_after: Any = None,
        created_before: Any = None,
        page: int = 1,
        limit: int = 50,
    ) -> Dict[str, Any]:
        page = max(1, int(page))
        limit = max(1, min(100, int(limit)))
        query: Dict[str, Any] = {'guild_id': int(guild_id)}
        if target_user_id is not None:
            query['target_user_id'] = int(target_user_id)
        if moderator_user_id is not None:
            query['moderator_user_id'] = int(moderator_user_id)
        if action:
            query['action'] = action
        if status:
            query['status'] = status
        if case_id is not None:
            query['case_id'] = int(case_id)
        if created_after is not None or created_before is not None:
            query['created_at'] = {}
            if created_after is not None:
                query['created_at']['$gte'] = created_after
            if created_before is not None:
                query['created_at']['$lte'] = created_before
        total = await self.collection.count_documents(query)
        cursor = self.collection.find(query).sort('case_id', -1).skip((page - 1) * limit).limit(limit)
        cases = [case async for case in cursor]
        return {'items': cases, 'page': page, 'limit': limit, 'total': total}

    async def update_case(
        self,
        guild_id: int,
        case_id: int,
        actor_id: int,
        changes: Dict[str, Any],
        *,
        revision_action: str = 'update',
    ) -> bool:
        allowed = {'reason', 'internal_note', 'evidence', 'status', 'expires_at', 'duration_seconds', 'points'}
        update = {key: value for key, value in changes.items() if key in allowed}
        if not update:
            return False
        if 'status' in update and update['status'] not in self.STATUSES:
            raise ValueError(f'Unsupported moderation case status: {update["status"]}')
        revision = {
            'actor_id': int(actor_id),
            'action': revision_action,
            'changes': redact_sensitive(update),
            'created_at': datetime.now(UTC),
        }
        result = await self.collection.update_one(
            {'guild_id': int(guild_id), 'case_id': int(case_id)},
            {'$set': {**update, 'updated_at': datetime.now(UTC)}, '$push': {'revisions': revision}},
        )
        return result.modified_count > 0

    async def record_revision(
        self,
        guild_id: int,
        case_id: int,
        actor_id: int,
        action: str,
        changes: Dict[str, Any],
    ) -> bool:
        now = datetime.now(UTC)
        result = await self.collection.update_one(
            {'guild_id': int(guild_id), 'case_id': int(case_id)},
            {
                '$set': {'updated_at': now},
                '$push': {
                    'revisions': {
                        'actor_id': int(actor_id),
                        'action': str(action)[:100],
                        'changes': redact_sensitive(changes),
                        'created_at': now,
                    }
                },
            },
        )
        return result.modified_count > 0

    async def warning_summary(self, guild_id: int, target_user_id: int, *, decay_days: int = 90) -> Dict[str, Any]:
        cutoff = datetime.now(UTC) - timedelta(days=max(1, int(decay_days)))
        cursor = self.collection.find(
            {
                'guild_id': int(guild_id),
                'target_user_id': int(target_user_id),
                'action': 'warn',
                'status': 'active',
                'created_at': {'$gte': cutoff},
            }
        ).sort('created_at', -1)
        cases = [case async for case in cursor]
        return {
            'target_user_id': int(target_user_id),
            'points': sum(max(0, int(case.get('points', 1))) for case in cases),
            'warning_count': len(cases),
            'decay_days': max(1, int(decay_days)),
            'items': cases,
        }

    async def expire_due_cases(self, *, now: Any = None, limit: int = 100) -> list[Dict[str, Any]]:
        now = now or datetime.now(UTC)
        cursor = (
            self.collection.find(
                {
                    'status': 'active',
                    'expires_at': {'$ne': None, '$lte': now},
                }
            )
            .sort('expires_at', 1)
            .limit(max(1, min(500, int(limit))))
        )
        expired = []
        async for case in cursor:
            revision = {
                'actor_id': None,
                'action': 'expire',
                'changes': {'status': 'expired'},
                'created_at': now,
            }
            result = await self.collection.update_one(
                {'_id': case['_id'], 'status': 'active'},
                {'$set': {'status': 'expired', 'updated_at': now}, '$push': {'revisions': revision}},
            )
            if result.modified_count:
                expired.append({**case, 'status': 'expired', 'updated_at': now})
        return expired

    async def export_cases(self, guild_id: int, **filters: Any) -> list[Dict[str, Any]]:
        query: Dict[str, Any] = {'guild_id': int(guild_id)}
        for field in ('target_user_id', 'moderator_user_id', 'case_id'):
            if filters.get(field) is not None:
                query[field] = int(filters[field])
        for field in ('action', 'status'):
            if filters.get(field):
                query[field] = filters[field]
        if filters.get('created_after') is not None or filters.get('created_before') is not None:
            query['created_at'] = {}
            if filters.get('created_after') is not None:
                query['created_at']['$gte'] = filters['created_after']
            if filters.get('created_before') is not None:
                query['created_at']['$lte'] = filters['created_before']
        cursor = self.collection.find(query).sort('case_id', 1).limit(10_000)
        return [case async for case in cursor]


class ModerationAppealModel(BaseModel):
    """Member-submitted appeals with a staff review trail."""

    STATUSES = frozenset({'pending', 'reviewing', 'accepted', 'declined'})

    def __init__(self):
        super().__init__('moderation_appeals')

    async def create_appeal(
        self,
        guild_id: int,
        case_id: int,
        target_user_id: int,
        statement: str,
        retention_days: int = 3650,
    ) -> Dict[str, Any]:
        existing = await self.collection.find_one(
            {
                'guild_id': int(guild_id),
                'case_id': int(case_id),
                'target_user_id': int(target_user_id),
                'status': {'$in': ['pending', 'reviewing']},
            }
        )
        if existing:
            return existing
        now = datetime.now(UTC)
        appeal = {
            'guild_id': int(guild_id),
            'case_id': int(case_id),
            'target_user_id': int(target_user_id),
            'statement': str(statement or '')[:4000],
            'status': 'pending',
            'reviewer_user_id': None,
            'review_reason': '',
            'created_at': now,
            'updated_at': now,
            'data_expires_at': now + timedelta(days=max(30, min(3650, int(retention_days)))),
        }
        await self.collection.insert_one(appeal)
        return appeal

    async def list_appeals(
        self,
        guild_id: int,
        *,
        status: Optional[str] = None,
        target_user_id: Optional[int] = None,
        page: int = 1,
        limit: int = 50,
    ) -> Dict[str, Any]:
        query: Dict[str, Any] = {'guild_id': int(guild_id)}
        if status:
            query['status'] = status
        if target_user_id is not None:
            query['target_user_id'] = int(target_user_id)
        page = max(1, int(page))
        limit = max(1, min(100, int(limit)))
        total = await self.collection.count_documents(query)
        cursor = self.collection.find(query).sort('created_at', -1).skip((page - 1) * limit).limit(limit)
        return {'items': [appeal async for appeal in cursor], 'page': page, 'limit': limit, 'total': total}

    async def get_pending_appeal(self, guild_id: int, case_id: int):
        return await self.collection.find_one(
            {
                'guild_id': int(guild_id),
                'case_id': int(case_id),
                'status': {'$in': ['pending', 'reviewing']},
            }
        )

    async def review_appeal(
        self,
        guild_id: int,
        case_id: int,
        reviewer_user_id: int,
        status: str,
        reason: str = '',
    ) -> bool:
        if status not in {'reviewing', 'accepted', 'declined'}:
            raise ValueError(f'Unsupported appeal status: {status}')
        result = await self.collection.update_one(
            {'guild_id': int(guild_id), 'case_id': int(case_id), 'status': {'$in': ['pending', 'reviewing']}},
            {
                '$set': {
                    'status': status,
                    'reviewer_user_id': int(reviewer_user_id),
                    'review_reason': str(reason or '')[:2000],
                    'updated_at': datetime.now(UTC),
                }
            },
        )
        return result.modified_count > 0


class ModerationEvidenceModel(BaseModel):
    """Capability-protected evidence blobs with tenant scope and TTL retention."""

    def __init__(self):
        super().__init__('moderation_evidence')

    async def create_evidence(
        self,
        guild_id: int,
        uploader_user_id: int,
        content: bytes,
        mime_type: str,
        extension: str,
        retention_days: int,
    ) -> Dict[str, Any]:
        now = datetime.now(UTC)
        evidence = {
            'evidence_id': secrets.token_hex(16),
            'guild_id': int(guild_id),
            'uploader_user_id': int(uploader_user_id),
            'content': bytes(content),
            'mime_type': str(mime_type)[:100],
            'extension': str(extension)[:10],
            'size': len(content),
            'created_at': now,
            # Uploads live only briefly until a case references them; attach_to_case extends retention.
            'attached': False,
            'retention_days': self._retention_days(retention_days),
            'expires_at': now + self.UNATTACHED_TTL,
        }
        await self.collection.insert_one(evidence)
        return evidence

    UNATTACHED_TTL = timedelta(hours=24)

    @staticmethod
    def _retention_days(retention_days: int) -> int:
        return max(30, min(3650, int(retention_days)))

    async def guild_usage(self, guild_id: int) -> Dict[str, int]:
        """Live evidence count and bytes for a guild, overall and for not-yet-attached uploads."""
        pipeline = [
            {'$match': {'guild_id': int(guild_id), 'expires_at': {'$gt': datetime.now(UTC)}}},
            {
                '$group': {
                    '_id': None,
                    'count': {'$sum': 1},
                    'bytes': {'$sum': '$size'},
                    'unattached': {'$sum': {'$cond': [{'$eq': ['$attached', True]}, 0, 1]}},
                }
            },
        ]
        rows = await (await self.collection.aggregate(pipeline)).to_list(length=1)
        row = rows[0] if rows else {}
        return {
            'count': int(row.get('count', 0)),
            'bytes': int(row.get('bytes', 0)),
            'unattached': int(row.get('unattached', 0)),
        }

    async def attach_to_case(self, guild_id: int, evidence_ids: list[str], retention_days: int) -> int:
        """Mark evidence referenced by a case as attached and give it the case retention period."""
        ids = sorted({str(item) for item in evidence_ids if item})
        if not ids:
            return 0
        expires_at = datetime.now(UTC) + timedelta(days=self._retention_days(retention_days))
        result = await self.collection.update_many(
            {'guild_id': int(guild_id), 'evidence_id': {'$in': ids}, 'expires_at': {'$gt': datetime.now(UTC)}},
            {'$set': {'attached': True, 'expires_at': expires_at}},
        )
        return int(result.modified_count)

    async def get_evidence(self, guild_id: int, evidence_id: str) -> Optional[Dict[str, Any]]:
        return await self.collection.find_one(
            {
                'guild_id': int(guild_id),
                'evidence_id': str(evidence_id),
                'expires_at': {'$gt': datetime.now(UTC)},
            }
        )
