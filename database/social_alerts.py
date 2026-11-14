"""Durable monitoring cursors, leases and delivery attempts. Native _id uniqueness protects each subscription."""

from datetime import UTC, datetime, timedelta
import hashlib
import json
import secrets

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database.base import BaseModel
from services.social_providers import account_handle

LEASE_SECONDS = 180


def utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)).isoformat()


def subscription_key(guild_id: int, entry: dict) -> str:
    identity = [
        str(guild_id),
        entry['id'],
        entry['platform'],
        account_handle(entry['platform'], entry['handle']),
        str(entry.get('channelId')),
        sorted(entry['events']),
    ]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


class SocialAlertState(BaseModel):
    def __init__(self):
        super().__init__('social_alert_state')

    async def claim(self, guild_id: int, entry: dict, now: datetime) -> dict | None:
        key = subscription_key(guild_id, entry)
        try:
            await self.collection.update_one(
                {'_id': key},
                {
                    '$setOnInsert': {
                        'guild_id': guild_id,
                        'alert_id': entry['id'],
                        'initialized': False,
                        'seen': [],
                        'pending': [],
                        'metadata': {},
                        'created_at': now,
                        'next_poll_at': now,
                        'lease_until': now,
                        'expires_at': now + timedelta(days=90),
                    }
                },
                upsert=True,
            )
        except DuplicateKeyError:
            pass
        return await self.collection.find_one_and_update(
            {'_id': key, 'next_poll_at': {'$lte': now}, 'lease_until': {'$lte': now}},
            {'$set': {'claim_token': secrets.token_hex(16), 'lease_until': now + timedelta(seconds=LEASE_SECONDS)}},
            return_document=ReturnDocument.AFTER,
        )

    async def update(self, state: dict, **values) -> bool:
        result = await self.collection.update_one(
            {'_id': state['_id'], 'claim_token': state['claim_token'], 'lease_until': {'$gt': datetime.now(UTC)}},
            {'$set': values},
        )
        if result.matched_count:
            state.update(values)
        return bool(result.matched_count)

    async def finish(self, state: dict, now: datetime, interval: int, code: str = '') -> None:
        await self.update(
            state,
            next_poll_at=now + timedelta(seconds=interval),
            lease_until=now,
            checked_at=now,
            code=code,
            expires_at=now + timedelta(days=90),
        )

    async def deactivate(self, guild_id: int, active_keys: list[str]) -> None:
        # A paused account gets a fresh baseline next time; missed posts are not replayed.
        await self.collection.update_many(
            {'guild_id': guild_id, '_id': {'$nin': active_keys}, 'initialized': True},
            {
                '$set': {
                    'initialized': False,
                    'seen': [],
                    'pending': [],
                    'code': 'paused',
                    'next_poll_at': datetime.now(UTC),
                }
            },
        )

    async def statuses(self, guild_id: int) -> dict[str, dict]:
        result = {}
        async for item in self.collection.find({'guild_id': guild_id}, {'alert_id': 1, 'code': 1, 'checked_at': 1, 'last_sent_at': 1}):
            result[item['_id']] = {
                'code': item.get('code', ''),
                'checked_at': utc_iso(item.get('checked_at')),
                'last_sent_at': utc_iso(item.get('last_sent_at')),
            }
        return result
