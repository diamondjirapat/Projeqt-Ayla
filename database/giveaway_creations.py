"""Durable giveaway posting intents. Ambiguous attempts never expire or resend."""

from datetime import UTC, datetime, timedelta

from pymongo.errors import DuplicateKeyError

from database.base import BaseModel


class GiveawayCreationModel(BaseModel):
    def __init__(self):
        super().__init__('giveaway_creations')

    async def reserve(self, operation_id: str, fingerprint: str, payload: dict, bot_id: int) -> tuple[bool, dict]:
        document = {
            '_id': operation_id,
            'fingerprint': fingerprint,
            'payload': payload,
            'status': 'pending',
            'created_at': datetime.now(UTC),
            'bot_id': bot_id,
        }
        try:
            await self.collection.insert_one(document)
            return True, document
        except DuplicateKeyError:
            return False, await self.collection.find_one({'_id': operation_id})

    async def record(self, operation_id: str, status: str, **fields) -> None:
        values = {'status': status, 'updated_at': datetime.now(UTC), **fields}
        if status in ('completed', 'compensated'):
            values['expires_at'] = datetime.now(UTC) + timedelta(days=7)
        update = {'$set': values}
        if status not in ('completed', 'compensated'):
            update['$unset'] = {'expires_at': ''}
        result = await self.collection.update_one({'_id': operation_id}, update)
        if not result.acknowledged or not result.matched_count:
            raise RuntimeError('Giveaway creation record is unavailable')
