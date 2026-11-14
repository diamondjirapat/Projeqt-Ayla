"""Scheduled announcements and events storage.

Items live in the ``scheduled_items`` collection. The scheduler cog claims
due items atomically with expiring leases. Durable attempts and history reconciliation
prevent automatic resends after uncertain delivery. Unconfirmed attempts need manual review.
"""

import calendar
import logging
import re
import secrets
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional

from bson import ObjectId
from pymongo import ReturnDocument

from database.models import BaseModel

logger = logging.getLogger(__name__)

SCHEDULER_KINDS = ('announcement', 'event')
SCHEDULER_RECURRENCES = ('once', 'daily', 'weekly', 'monthly')
SCHEDULER_STATUSES = ('scheduled', 'sending', 'done', 'cancelled', 'failed')

SCHEDULER_TITLE_LIMIT = 256
SCHEDULER_CONTENT_LIMIT = 2000
SCHEDULER_LOCATION_LIMIT = 128

SCHEDULER_EMBED_TEXT_LIMITS = {'title': 256, 'description': 3500, 'footer': 512}
SCHEDULER_EMBED_URL_FIELDS = ('image_url', 'thumbnail_url', 'footer_icon_url')
SCHEDULER_MEDIA_VARIABLES = {'{avatar}', '{server_icon}', '{server_banner}'}

STALE_AFTER = timedelta(hours=24)
MAX_CLAIM_PER_TICK = 25
CLAIM_LEASE = timedelta(minutes=5)


def ensure_utc(value: datetime) -> datetime:
    """Return *value* as an aware UTC datetime (naive values are assumed UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_scheduler_time(raw: Any, field: str = 'starts_at') -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f'{field} is required.')
    try:
        parsed = datetime.fromisoformat(raw.strip().replace('Z', '+00:00'))
        return ensure_utc(parsed)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f'{field} must be an ISO-8601 date or timestamp.') from exc


def advance_recurrence(after: datetime, recurrence: str) -> datetime:
    """Return the next occurrence after *after* for a recurrence rule."""
    moment = ensure_utc(after)
    if recurrence == 'daily':
        return moment + timedelta(days=1)
    if recurrence == 'weekly':
        return moment + timedelta(weeks=1)
    if recurrence == 'monthly':
        year, month = moment.year, moment.month + 1
        if month > 12:
            year += 1
            month = 1
        last_day = calendar.monthrange(year, month)[1]
        return moment.replace(year=year, month=month, day=min(moment.day, last_day))
    raise ValueError(f'Unknown recurrence: {recurrence}.')


def normalize_scheduler_embed(raw_config: Any, *, strict: bool = False) -> Dict[str, str]:
    """Return a Discord-safe scheduler embed configuration."""
    if raw_config is None:
        return {}
    if not isinstance(raw_config, dict):
        if strict:
            raise ValueError('embed must be an object.')
        return {}

    normalized: Dict[str, str] = {}
    for field, maximum in SCHEDULER_EMBED_TEXT_LIMITS.items():
        value = str(raw_config.get(field, '') or '').strip()
        if len(value) > maximum:
            if strict:
                raise ValueError(f'embed.{field} must be {maximum} characters or fewer.')
            value = value[:maximum]
        if value:
            normalized[field] = value

    raw_color = str(raw_config.get('color', '') or '').strip()
    if raw_color:
        color = raw_color if raw_color.startswith('#') else f'#{raw_color}'
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
            if strict:
                raise ValueError('embed.color must be a six-digit hex color.')
        else:
            normalized['color'] = color.lower()

    for field in SCHEDULER_EMBED_URL_FIELDS:
        value = str(raw_config.get(field, '') or '').strip()
        if not value:
            continue
        if len(value) > 2048:
            if strict:
                raise ValueError(f'embed.{field} must be 2048 characters or fewer.')
            continue
        if value in SCHEDULER_MEDIA_VARIABLES:
            normalized[field] = value
            continue
        try:
            parsed = urllib.parse.urlsplit(value)
            valid_url = parsed.scheme in {'http', 'https'} and bool(parsed.hostname)
        except ValueError:
            valid_url = False
            parsed = None
        if not valid_url or (parsed and (parsed.username or parsed.password)):
            if strict:
                raise ValueError(f'embed.{field} must be a valid HTTP or HTTPS URL.')
            continue
        normalized[field] = value

    return normalized


def validate_scheduler_payload(raw: Any, *, partial: bool = False) -> Dict[str, Any]:
    """Validate a create/update payload, returning normalized values.

    Raises ``ValueError`` with a human-readable message for invalid input.
    With ``partial=True`` every field is optional (PATCH semantics).
    """
    if not isinstance(raw, dict):
        raise ValueError('Payload must be an object.')
    now = datetime.now(UTC)
    normalized: Dict[str, Any] = {}

    if 'kind' in raw or not partial:
        kind = str(raw.get('kind', '') or '').strip().lower()
        if kind not in SCHEDULER_KINDS:
            raise ValueError(f'kind must be one of: {", ".join(SCHEDULER_KINDS)}.')
        normalized['kind'] = kind

    if 'title' in raw:
        title = str(raw.get('title', '') or '').strip()
        if len(title) > SCHEDULER_TITLE_LIMIT:
            raise ValueError(f'title must be {SCHEDULER_TITLE_LIMIT} characters or fewer.')
        normalized['title'] = title

    if 'content' in raw or not partial:
        content = str(raw.get('content', '') or '').strip()
        if not content:
            raise ValueError('content is required.')
        if len(content) > SCHEDULER_CONTENT_LIMIT:
            raise ValueError(f'content must be {SCHEDULER_CONTENT_LIMIT} characters or fewer.')
        normalized['content'] = content

    if 'embed' in raw:
        try:
            normalized['embed'] = normalize_scheduler_embed(raw.get('embed'), strict=True)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    if 'starts_at' in raw or not partial:
        starts_at = parse_scheduler_time(raw.get('starts_at'), 'starts_at')
        if starts_at <= now:
            raise ValueError('starts_at must be in the future.')
        normalized['starts_at'] = starts_at

    if 'recurrence' in raw:
        recurrence = str(raw.get('recurrence', '') or '').strip().lower() or 'once'
        if recurrence not in SCHEDULER_RECURRENCES:
            raise ValueError(f'recurrence must be one of: {", ".join(SCHEDULER_RECURRENCES)}.')
        normalized['recurrence'] = recurrence

    if 'event_end_at' in raw and raw.get('event_end_at') not in (None, ''):
        event_end_at = parse_scheduler_time(raw.get('event_end_at'), 'event_end_at')
        starts_at = normalized.get('starts_at')
        if starts_at is not None and event_end_at <= starts_at:
            raise ValueError('event_end_at must be after starts_at.')
        normalized['event_end_at'] = event_end_at
    elif 'event_end_at' in raw:
        normalized['event_end_at'] = None

    if 'location' in raw:
        location = str(raw.get('location', '') or '').strip()
        if len(location) > SCHEDULER_LOCATION_LIMIT:
            raise ValueError(f'location must be {SCHEDULER_LOCATION_LIMIT} characters or fewer.')
        normalized['location'] = location

    return normalized


def serialize_scheduler_item(item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Convert a scheduler document into a JSON-safe mapping."""
    if not item:
        return {}
    res = dict(item)
    if '_id' in res:
        res['_id'] = str(res['_id'])
        res['item_id'] = str(res['_id'])
    res.pop('claim_token', None)
    res.pop('claim_owner', None)
    for key in ('starts_at', 'next_run_at', 'event_end_at', 'created_at', 'updated_at', 'sent_at', 'lease_until'):
        if hasattr(res.get(key), 'isoformat'):
            res[key] = res[key].isoformat()
    return res


def _coerce_object_id(item_id: Any) -> Optional[ObjectId]:
    try:
        return ObjectId(str(item_id))
    except Exception:
        return None


class ScheduledItemModel(BaseModel):
    """Persist scheduled announcements and Discord events."""

    def __init__(self):
        super().__init__('scheduled_items')

    async def create_item(
        self,
        guild_id: int,
        channel_id: int,
        creator_id: int,
        payload: Dict[str, Any],
        *,
        discord_event_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Store a validated payload as a new scheduled item."""
        now = datetime.now(UTC)
        doc = {
            'guild_id': int(guild_id),
            'channel_id': int(channel_id),
            'creator_id': int(creator_id),
            # Whose authority governs mention pings at dispatch; refreshed whenever content changes.
            'content_author_id': int(creator_id),
            'kind': payload['kind'],
            'title': payload.get('title', ''),
            'content': payload['content'],
            'embed': payload.get('embed', {}),
            'starts_at': payload['starts_at'],
            'next_run_at': payload['starts_at'],
            'recurrence': payload.get('recurrence', 'once'),
            'event_end_at': payload.get('event_end_at'),
            'location': payload.get('location', ''),
            'discord_event_id': int(discord_event_id) if discord_event_id else None,
            'status': 'scheduled',
            'last_error': '',
            'sent_at': None,
            'created_at': now,
            'updated_at': now,
        }
        await self.collection.insert_one(doc)
        return doc

    async def get_item(self, item_id: Any) -> Optional[Dict[str, Any]]:
        """Fetch a single item by its ID."""
        object_id = _coerce_object_id(item_id)
        if object_id is None:
            return None
        return await self.collection.find_one({'_id': object_id})

    async def list_guild_items(
        self,
        guild_id: int,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        include_done: bool = True,
        include_cancelled: bool = False,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """List a guild's items, optionally bounded to a time window."""
        query: Dict[str, Any] = {'guild_id': int(guild_id)}
        if start is not None or end is not None:
            window: Dict[str, Any] = {}
            if start is not None:
                window['$gte'] = ensure_utc(start)
            if end is not None:
                window['$lte'] = ensure_utc(end)
            query['$or'] = [
                {'next_run_at': window},
                {
                    'recurrence': {'$in': ['daily', 'weekly', 'monthly']},
                    'status': {'$in': ['scheduled', 'sending', 'failed']},
                    'next_run_at': {'$lte': ensure_utc(end)} if end is not None else {'$exists': True},
                },
            ]
        excluded = []
        if not include_done:
            excluded.append('done')
        if not include_cancelled:
            excluded.append('cancelled')
        if excluded:
            query['status'] = {'$nin': excluded}
        cursor = self.collection.find(query).sort('next_run_at', 1).limit(limit)
        return await cursor.to_list(length=limit)

    async def update_item(
        self, item_id: Any, guild_id: int, changes: Dict[str, Any], *, editor_id: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """Apply validated changes to a scheduled item; returns the updated doc."""
        allowed = {'kind', 'title', 'content', 'embed', 'recurrence', 'channel_id', 'event_end_at', 'location'}
        values = {key: changes[key] for key in allowed if key in changes}
        if editor_id is not None and any(key in values for key in ('content', 'embed', 'channel_id', 'kind')):
            values['content_author_id'] = int(editor_id)
        if 'starts_at' in changes:
            values['starts_at'] = changes['starts_at']
            values['next_run_at'] = changes['starts_at']
        if not values:
            return None
        values['updated_at'] = datetime.now(UTC)
        object_id = _coerce_object_id(item_id)
        if object_id is None:
            return None
        query = {'_id': object_id, 'guild_id': int(guild_id), 'status': 'scheduled'}
        if 'starts_at' in changes or 'event_end_at' in changes:
            existing = await self.collection.find_one(query)
            if existing is None:
                return None
            starts_at = changes.get('starts_at', existing.get('next_run_at') or existing.get('starts_at'))
            event_end_at = changes.get('event_end_at', existing.get('event_end_at'))
            if event_end_at is not None and ensure_utc(event_end_at) <= ensure_utc(starts_at):
                raise ValueError('event_end_at must be after starts_at.')
            # Guard both halves of the validated pair against concurrent edits.
            query.update({field: existing.get(field) for field in ('starts_at', 'next_run_at', 'event_end_at')})
        return await self.collection.find_one_and_update(
            query,
            {'$set': values},
            return_document=ReturnDocument.AFTER,
        )

    async def cancel_item(self, item_id: Any, guild_id: int) -> Optional[Dict[str, Any]]:
        """Mark an item cancelled; returns the doc for native-event cleanup."""
        object_id = _coerce_object_id(item_id)
        if object_id is None:
            return None
        return await self.collection.find_one_and_update(
            {'_id': object_id, 'guild_id': int(guild_id), 'status': {'$in': ['scheduled', 'failed']}},
            {'$set': {'status': 'cancelled', 'updated_at': datetime.now(UTC)}},
            return_document=ReturnDocument.AFTER,
        )

    async def claim_due_items(
        self, now: datetime, *, guild_ids: list[int], channel_ids: list[int], owner: str, limit: int = 1
    ) -> List[Dict[str, Any]]:
        """Claim reachable jobs, including expired leases left by a crashed worker."""
        if not guild_ids or not channel_ids:
            return []
        now = ensure_utc(now)
        # Pre-upgrade sending records have no durable attempt. Their outcome is
        # unknowable, so quarantine instead of replaying a potentially accepted send.
        await self.collection.update_many(
            {
                'guild_id': {'$in': [int(guild_id) for guild_id in guild_ids]},
                'channel_id': {'$in': [int(channel_id) for channel_id in channel_ids]},
                'status': 'sending',
                'delivery_protocol': {'$ne': 1},
                '$or': [
                    {'lease_until': {'$lte': now}},
                    {'lease_until': {'$exists': False}, 'updated_at': {'$lte': now - CLAIM_LEASE}},
                ],
            },
            {
                '$set': {
                    'status': 'failed',
                    'last_error': 'Legacy interrupted delivery requires manual channel-history review before retry.',
                    'updated_at': now,
                }
            },
        )
        query = {
            'guild_id': {'$in': [int(guild_id) for guild_id in guild_ids]},
            'channel_id': {'$in': [int(channel_id) for channel_id in channel_ids]},
            'next_run_at': {'$lte': now},
            '$or': [
                {'status': 'scheduled'},
                {'status': 'sending', 'delivery_protocol': 1, 'lease_until': {'$lte': now}},
                {
                    'status': 'sending',
                    'delivery_protocol': 1,
                    'lease_until': {'$exists': False},
                    'updated_at': {'$lte': now - CLAIM_LEASE},
                },
            ],
        }
        claimed: List[Dict[str, Any]] = []
        for _ in range(limit):
            doc = await self.collection.find_one_and_update(
                query,
                {
                    '$set': {
                        'status': 'sending',
                        'delivery_protocol': 1,
                        'updated_at': now,
                        'lease_until': now + CLAIM_LEASE,
                        'claim_owner': owner,
                        'claim_token': secrets.token_hex(16),
                    }
                },
                sort=[('next_run_at', 1)],
                return_document=ReturnDocument.AFTER,
            )
            if not doc:
                break
            claimed.append(doc)
        return claimed

    async def begin_delivery(self, item_id: Any, now: datetime, *, claim_token: str, marker: str, bot_id: int) -> bool:
        """Reserve one external send before contacting Discord; never reset on lease recovery."""
        object_id = _coerce_object_id(item_id)
        if object_id is None:
            return False
        result = await self.collection.update_one(
            {
                '_id': object_id,
                'status': 'sending',
                'claim_token': claim_token,
                'delivery_attempt': {'$exists': False},
                'lease_until': {'$gt': now},
            },
            {'$set': {'delivery_attempt': {'marker': marker, 'bot_id': bot_id, 'started_at': now}}},
        )
        return result.modified_count > 0

    async def resolve_item(self, item_id: Any, now: datetime, *, claim_token: str) -> Optional[Dict[str, Any]]:
        """Mark a dispatched item done, or advance it when it recurs."""
        object_id = _coerce_object_id(item_id)
        if object_id is None:
            return None
        identity = {'_id': object_id, 'status': 'sending', 'claim_token': claim_token}
        doc = await self.collection.find_one(identity)
        if not doc:
            return None
        recurrence = doc.get('recurrence', 'once')
        if recurrence in SCHEDULER_RECURRENCES and recurrence != 'once':
            base = ensure_utc(doc.get('next_run_at') or now)
            upcoming = advance_recurrence(base, recurrence)
            while upcoming <= now:
                base = upcoming
                upcoming = advance_recurrence(base, recurrence)
            values = {'status': 'scheduled', 'next_run_at': upcoming, 'sent_at': now, 'updated_at': now}
        else:
            values = {'status': 'done', 'sent_at': now, 'updated_at': now}
        return await self.collection.find_one_and_update(
            identity,
            {
                '$set': values,
                '$unset': {'claim_token': '', 'claim_owner': '', 'lease_until': '', 'delivery_attempt': ''},
            },
            return_document=ReturnDocument.AFTER,
        )

    async def release_claim(self, item_id: Any, *, claim_token: str) -> bool:
        """Return a claimed item to scheduled (used when dispatch is retried later)."""
        object_id = _coerce_object_id(item_id)
        if object_id is None:
            return False
        result = await self.collection.update_one(
            {'_id': object_id, 'status': 'sending', 'claim_token': claim_token},
            {
                '$set': {'status': 'scheduled', 'updated_at': datetime.now(UTC)},
                '$unset': {'claim_token': '', 'claim_owner': '', 'lease_until': ''},
            },
        )
        return result.modified_count > 0

    async def mark_failed(self, item_id: Any, error: str, *, claim_token: str) -> bool:
        """Record a dispatch failure on an item."""
        object_id = _coerce_object_id(item_id)
        if object_id is None:
            return False
        result = await self.collection.update_one(
            {'_id': object_id, 'status': 'sending', 'claim_token': claim_token},
            {
                '$set': {'status': 'failed', 'last_error': str(error)[:500], 'updated_at': datetime.now(UTC)},
                '$unset': {'claim_token': '', 'claim_owner': '', 'lease_until': ''},
            },
        )
        return result.modified_count > 0
