"""Tickets persistence and domain rules."""

from datetime import UTC, datetime, timedelta
from typing import Optional, Dict, Any
import logging
import secrets
import weakref
from utils.cache import AsyncTTLCache
from pymongo import ReturnDocument
from database.connection import db_manager
from utils.security import redact_sensitive

from database.base import BaseModel

logger = logging.getLogger(__name__)
_ticket_channel_caches = weakref.WeakSet()


class TicketPanelModel(BaseModel):
    def __init__(self):
        super().__init__('ticket_panels')

    async def create_panel(self, guild_id: int, actor_user_id: int, config: Dict[str, Any]) -> Dict[str, Any]:
        from utils.tickets import normalize_ticket_panel

        now = datetime.now(UTC)
        panel = {
            'panel_id': secrets.token_hex(10),
            'guild_id': int(guild_id),
            **normalize_ticket_panel(config),
            'channel_id': None,
            'message_id': None,
            'created_by': int(actor_user_id),
            'created_at': now,
            'updated_at': now,
        }
        await self.collection.insert_one(panel)
        return panel

    async def get_panel(self, guild_id: int, panel_id: str) -> Optional[Dict[str, Any]]:
        return await self.collection.find_one({'guild_id': int(guild_id), 'panel_id': str(panel_id)})

    async def get_by_message(self, guild_id: int, message_id: int) -> Optional[Dict[str, Any]]:
        return await self.collection.find_one(
            {
                'guild_id': int(guild_id),
                'message_id': int(message_id),
                'enabled': True,
            }
        )

    async def list_panels(self, guild_id: int) -> list[Dict[str, Any]]:
        return [
            item
            async for item in self.collection.find({'guild_id': int(guild_id), 'deleted': {'$ne': True}}).sort(
                'created_at', 1
            )
        ]

    async def update_panel(self, guild_id: int, panel_id: str, config: Dict[str, Any]) -> bool:
        from utils.tickets import normalize_ticket_panel

        existing = await self.get_panel(guild_id, panel_id)
        if not existing or existing.get('deleted'):
            return False
        normalized = normalize_ticket_panel({**existing, **config})
        result = await self.collection.update_one(
            {'guild_id': int(guild_id), 'panel_id': str(panel_id)},
            {'$set': {**normalized, 'updated_at': datetime.now(UTC)}},
        )
        return result.matched_count > 0

    async def bind_message(self, guild_id: int, panel_id: str, channel_id: int, message_id: int) -> bool:
        result = await self.collection.update_one(
            {'guild_id': int(guild_id), 'panel_id': str(panel_id)},
            {
                '$set': {
                    'channel_id': int(channel_id),
                    'message_id': int(message_id),
                    'updated_at': datetime.now(UTC),
                }
            },
        )
        return result.matched_count > 0

    async def delete_panel(self, guild_id: int, panel_id: str) -> bool:
        # Keep staff and retention settings for tickets already opened from this panel.
        result = await self.collection.update_one(
            {'guild_id': int(guild_id), 'panel_id': str(panel_id)},
            {'$set': {'deleted': True, 'enabled': False, 'updated_at': datetime.now(UTC)}},
        )
        return result.matched_count > 0


class TicketModel(BaseModel):
    def __init__(self):
        super().__init__('tickets')
        self._channel_misses = AsyncTTLCache(ttl=5, max_size=2000, name='tickets.channel_misses')
        _ticket_channel_caches.add(self._channel_misses)

    @property
    def counter_collection(self):
        return db_manager.get_collection('ticket_counters')

    async def can_open(self, guild_id: int, member_user_id: int, panel: Dict[str, Any]) -> tuple[bool, str]:
        query = {
            'guild_id': int(guild_id),
            'member_user_id': int(member_user_id),
            'panel_id': panel['panel_id'],
            'status': 'open',
        }
        count = await self.collection.count_documents(query)
        if count >= int(panel.get('max_open_per_member', 1)):
            return False, 'You already have the maximum number of open tickets for this category.'
        cooldown = int(panel.get('cooldown_seconds', 0))
        if cooldown:
            recent = await self.collection.find_one(
                {
                    'guild_id': int(guild_id),
                    'member_user_id': int(member_user_id),
                    'panel_id': panel['panel_id'],
                    'created_at': {'$gte': datetime.now(UTC) - timedelta(seconds=cooldown)},
                },
                sort=[('created_at', -1)],
            )
            if recent:
                return False, 'Please wait before opening another ticket in this category.'
        return True, ''

    async def create_ticket(
        self,
        guild_id: int,
        panel: Dict[str, Any],
        member_user_id: int,
        channel_id: int,
        *,
        answers: Optional[list[Dict[str, str]]] = None,
        evidence: Optional[list[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        counter = await self.counter_collection.find_one_and_update(
            {'guild_id': int(guild_id)},
            {'$inc': {'next_ticket_id': 1}, '$setOnInsert': {'created_at': datetime.now(UTC)}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        now = datetime.now(UTC)
        ticket = {
            'guild_id': int(guild_id),
            'ticket_id': int(counter['next_ticket_id']),
            'panel_id': panel['panel_id'],
            'category_name': panel.get('category_name', 'Support'),
            'channel_id': int(channel_id),
            'member_user_id': int(member_user_id),
            'anonymous': bool(panel.get('anonymous')),
            'status': 'open',
            'priority': 'normal',
            'assigned_staff_id': None,
            'participant_user_ids': [],
            'answers': answers or [],
            'evidence': evidence or [],
            'first_staff_response_at': None,
            'last_activity_at': now,
            'reminder_sent_at': None,
            'closed_at': None,
            'closed_by': None,
            'resolution_seconds': None,
            'rating': None,
            'rating_message_id': None,
            'transcript': None,
            'retention_days': max(1, min(3650, int(panel.get('retention_days', 365)))),
            'created_at': now,
            'updated_at': now,
        }
        await self.collection.insert_one(ticket)
        for cache in tuple(_ticket_channel_caches):
            cache.pop((int(guild_id), int(channel_id)))
        return ticket

    async def get_ticket(self, guild_id: int, ticket_id: int) -> Optional[Dict[str, Any]]:
        return await self.collection.find_one({'guild_id': int(guild_id), 'ticket_id': int(ticket_id)})

    async def get_by_channel(self, guild_id: int, channel_id: int) -> Optional[Dict[str, Any]]:
        return await self.collection.find_one({'guild_id': int(guild_id), 'channel_id': int(channel_id)})

    async def get_message_ticket(self, guild_id: int, channel_id: int) -> Optional[Dict[str, Any]]:
        """Cache channel misses briefly; existing tickets always receive fresh reads."""
        key = (int(guild_id), int(channel_id))
        if self._channel_misses.get(key) is None:
            return None
        ticket = await self._channel_misses.get_or_load(key, lambda: self.get_by_channel(guild_id, channel_id))
        if ticket is not None:
            self._channel_misses.pop(key)
        return ticket

    async def get_by_rating_message(self, message_id: int) -> Optional[Dict[str, Any]]:
        return await self.collection.find_one({'rating_message_id': int(message_id), 'status': 'closed'})

    async def list_tickets(
        self,
        guild_id: int,
        *,
        status: Optional[str] = None,
        category: Optional[str] = None,
        assignee: Optional[int] = None,
        priority: Optional[str] = None,
        member: Optional[int] = None,
        older_than: Optional[datetime] = None,
        page: int = 1,
        limit: int = 50,
    ) -> Dict[str, Any]:
        query: Dict[str, Any] = {'guild_id': int(guild_id)}
        for key, value in (('status', status), ('category_name', category), ('priority', priority)):
            if value:
                query[key] = value
        if assignee is not None:
            query['assigned_staff_id'] = int(assignee)
        if member is not None:
            query['member_user_id'] = int(member)
        if older_than:
            query['created_at'] = {'$lte': older_than}
        page, limit = max(1, int(page)), max(1, min(100, int(limit)))
        total = await self.collection.count_documents(query)
        cursor = self.collection.find(query).sort('updated_at', -1).skip((page - 1) * limit).limit(limit)
        return {'items': [item async for item in cursor], 'page': page, 'limit': limit, 'total': total}

    async def update_ticket(self, guild_id: int, ticket_id: int, values: Dict[str, Any]) -> bool:
        allowed = {
            'category_name',
            'priority',
            'assigned_staff_id',
            'participant_user_ids',
            'channel_id',
            'status',
            'closed_at',
            'closed_by',
            'resolution_seconds',
            'rating',
            'rating_message_id',
            'transcript',
        }
        update = {key: value for key, value in values.items() if key in allowed}
        if not update:
            return False
        update['updated_at'] = datetime.now(UTC)
        result = await self.collection.update_one(
            {'guild_id': int(guild_id), 'ticket_id': int(ticket_id)},
            {'$set': update},
        )
        return result.matched_count > 0

    async def record_activity(
        self,
        guild_id: int,
        channel_id: int,
        actor_user_id: int,
        *,
        is_staff: bool = False,
        evidence: Optional[list[Dict[str, Any]]] = None,
    ) -> bool:
        now = datetime.now(UTC)
        update: Dict[str, Any] = {'$set': {'last_activity_at': now, 'reminder_sent_at': None, 'updated_at': now}}
        if evidence:
            update['$push'] = {'evidence': {'$each': evidence[:10]}}
        result = await self.collection.update_one(
            {'guild_id': int(guild_id), 'channel_id': int(channel_id), 'status': 'open'},
            update,
        )
        if is_staff:
            await self.collection.update_one(
                {
                    'guild_id': int(guild_id),
                    'channel_id': int(channel_id),
                    'status': 'open',
                    'first_staff_response_at': None,
                },
                {'$set': {'first_staff_response_at': now, 'first_staff_user_id': int(actor_user_id)}},
            )
        return result.matched_count > 0

    async def close_ticket(self, guild_id: int, ticket_id: int, actor_user_id: int) -> Optional[Dict[str, Any]]:
        ticket = await self.get_ticket(guild_id, ticket_id)
        if not ticket or ticket.get('status') != 'open':
            return None
        now = datetime.now(UTC)
        expires_at = now + timedelta(days=max(1, min(3650, int(ticket.get('retention_days', 365)))))
        result = await self.collection.find_one_and_update(
            {'guild_id': int(guild_id), 'ticket_id': int(ticket_id), 'status': 'open'},
            {
                '$set': {
                    'status': 'closed',
                    'closed_at': now,
                    'closed_by': int(actor_user_id),
                    'resolution_seconds': max(0, int((now - ticket['created_at']).total_seconds())),
                    'expires_at': expires_at,
                    'updated_at': now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if result:
            for collection_name in ('ticket_events', 'ticket_transcripts', 'ticket_transcript_messages'):
                await db_manager.get_collection(collection_name).update_many(
                    {'guild_id': int(guild_id), 'ticket_id': int(ticket_id)},
                    {'$set': {'expires_at': expires_at}},
                )
        return result

    async def reopen_ticket(self, guild_id: int, ticket_id: int, actor_user_id: int) -> Optional[Dict[str, Any]]:
        result = await self.collection.find_one_and_update(
            {'guild_id': int(guild_id), 'ticket_id': int(ticket_id), 'status': 'closed'},
            {
                '$set': {
                    'status': 'open',
                    'closed_at': None,
                    'closed_by': None,
                    'resolution_seconds': None,
                    'last_activity_at': datetime.now(UTC),
                    'updated_at': datetime.now(UTC),
                    'reopened_by': int(actor_user_id),
                },
                '$unset': {'expires_at': ''},
            },
            return_document=ReturnDocument.AFTER,
        )
        if result:
            for collection_name in ('ticket_events', 'ticket_transcripts', 'ticket_transcript_messages'):
                await db_manager.get_collection(collection_name).update_many(
                    {'guild_id': int(guild_id), 'ticket_id': int(ticket_id)},
                    {'$unset': {'expires_at': ''}},
                )
        return result

    async def add_note(self, guild_id: int, ticket_id: int, actor_user_id: int, content: str) -> Dict[str, Any]:
        ticket = await self.get_ticket(guild_id, ticket_id)
        note = {
            'note_id': secrets.token_hex(8),
            'guild_id': int(guild_id),
            'ticket_id': int(ticket_id),
            'actor_user_id': int(actor_user_id),
            'content': str(content).strip()[:4000],
            'created_at': datetime.now(UTC),
        }
        if ticket and ticket.get('expires_at'):
            note['expires_at'] = ticket['expires_at']
        await db_manager.get_collection('ticket_events').insert_one({**note, 'event_type': 'private_note'})
        return note

    async def record_event(
        self,
        guild_id: int,
        ticket_id: int,
        event_type: str,
        actor_user_id: Optional[int],
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ticket = await self.get_ticket(guild_id, ticket_id)
        event = {
            'event_id': secrets.token_hex(10),
            'guild_id': int(guild_id),
            'ticket_id': int(ticket_id),
            'event_type': str(event_type)[:80],
            'actor_user_id': int(actor_user_id) if actor_user_id is not None else None,
            'data': redact_sensitive(data or {}),
            'created_at': datetime.now(UTC),
        }
        if ticket and ticket.get('expires_at'):
            event['expires_at'] = ticket['expires_at']
        await db_manager.get_collection('ticket_events').insert_one(event)
        return event

    async def list_events(self, guild_id: int, ticket_id: int) -> list[Dict[str, Any]]:
        cursor = (
            db_manager.get_collection('ticket_events')
            .find(
                {
                    'guild_id': int(guild_id),
                    'ticket_id': int(ticket_id),
                }
            )
            .sort('created_at', 1)
        )
        return [event async for event in cursor]

    async def rate_ticket(self, message_id: int, member_user_id: int, rating: int) -> bool:
        result = await self.collection.update_one(
            {
                'rating_message_id': int(message_id),
                'member_user_id': int(member_user_id),
                'status': 'closed',
                'rating': None,
            },
            {'$set': {'rating': max(1, min(5, int(rating))), 'rated_at': datetime.now(UTC)}},
        )
        return result.modified_count > 0

    async def statistics(self, guild_id: int) -> Dict[str, Any]:
        items = [
            item async for item in self.collection.find({'guild_id': int(guild_id)}).sort('created_at', -1).limit(5000)
        ]
        response_times = [
            (item['first_staff_response_at'] - item['created_at']).total_seconds()
            for item in items
            if item.get('first_staff_response_at') and item.get('created_at')
        ]
        resolution_times = [item['resolution_seconds'] for item in items if item.get('resolution_seconds') is not None]
        ratings = [item['rating'] for item in items if item.get('rating') is not None]
        workload: Dict[str, int] = {}
        for item in items:
            if item.get('status') == 'open' and item.get('assigned_staff_id'):
                key = str(item['assigned_staff_id'])
                workload[key] = workload.get(key, 0) + 1

        def average(values: list[Any]) -> Optional[float]:
            return round(sum(values) / len(values), 1) if values else None

        return {
            'open': sum(item.get('status') == 'open' for item in items),
            'closed': sum(item.get('status') == 'closed' for item in items),
            'average_response_seconds': average(response_times),
            'average_resolution_seconds': average(resolution_times),
            'average_rating': average(ratings),
            'staff_workload': workload,
        }
