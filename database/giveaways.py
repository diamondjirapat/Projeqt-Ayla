"""Giveaways persistence and domain rules."""

from datetime import UTC, datetime
from typing import Optional, Dict, Any
import logging

from database.base import BaseModel

logger = logging.getLogger(__name__)


def is_giveaway_closed(giveaway: Dict[str, Any]) -> bool:
    deadline = giveaway.get('end_time')
    if isinstance(deadline, datetime):
        deadline = deadline.replace(tzinfo=UTC) if deadline.tzinfo is None else deadline.astimezone(UTC)
        return bool(giveaway.get('ended')) or deadline <= datetime.now(UTC)
    return bool(giveaway.get('ended'))


class GiveawayModel(BaseModel):
    def __init__(self):
        super().__init__('giveaways')

    async def create_giveaway(
        self,
        message_id: int,
        guild_id: int,
        channel_id: int,
        host_id: int,
        prize: str,
        winners_count: int,
        end_time: datetime,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create and store a new giveaway record."""
        now = datetime.now(UTC)
        giveaway_data = {
            'message_id': int(message_id),
            'guild_id': int(guild_id),
            'channel_id': int(channel_id),
            'host_id': int(host_id),
            'prize': prize,
            'winners_count': max(1, int(winners_count)),
            'end_time': end_time,
            'entries': [],
            'ended': False,
            'winners': [],
            'extra_data': extra_data or {},
            'created_at': now,
            'updated_at': now,
        }
        await self.collection.insert_one(giveaway_data)
        return giveaway_data

    async def get_giveaway(self, message_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a giveaway by its Discord message ID."""
        return await self.collection.find_one({'message_id': int(message_id)})

    async def add_entry(self, message_id: int, user_id: int) -> bool:
        """Add a user to the giveaway entries list if not already entered."""
        result = await self.collection.update_one(
            {
                'message_id': int(message_id),
                'ended': False,
                'end_time': {'$gt': datetime.now(UTC)},
                'entries': {'$ne': int(user_id)},
            },
            {
                '$push': {'entries': int(user_id)},
                '$set': {'updated_at': datetime.now(UTC)},
            },
        )
        return result.modified_count > 0

    async def remove_entry(self, message_id: int, user_id: int) -> bool:
        """Remove a user from the giveaway entries list."""
        result = await self.collection.update_one(
            {
                'message_id': int(message_id),
                'ended': False,
                'entries': int(user_id),
                'end_time': {'$gt': datetime.now(UTC)},
            },
            {
                '$pull': {'entries': int(user_id)},
                '$set': {'updated_at': datetime.now(UTC)},
            },
        )
        return result.modified_count > 0

    async def get_active_giveaways(self) -> list[Dict[str, Any]]:
        """Retrieve all ongoing active giveaways."""
        cursor = self.collection.find({'ended': False})
        return await cursor.to_list(length=None)

    async def get_expired_active_giveaways(self) -> list[Dict[str, Any]]:
        """Retrieve all active giveaways whose end_time has passed."""
        now = datetime.now(UTC)
        cursor = self.collection.find({'ended': False, 'end_time': {'$lte': now}})
        return await cursor.to_list(length=None)

    async def get_guild_giveaways(self, guild_id: int, include_ended: bool = False) -> list[Dict[str, Any]]:
        """Get all giveaways for a specific guild."""
        query: Dict[str, Any] = {'guild_id': int(guild_id)}
        if not include_ended:
            query['ended'] = False
        cursor = self.collection.find(query).sort('end_time', 1)
        return await cursor.to_list(length=None)

    async def end_giveaway(self, message_id: int, winners: list[int]) -> bool:
        """Mark a giveaway as ended and record winners.

        The transition is atomic: only an active giveaway is ended, so
        concurrent finalizers cannot both succeed. Returns False when the
        giveaway was already ended (or does not exist).
        """
        result = await self.collection.update_one(
            {'message_id': int(message_id), 'ended': {'$ne': True}},
            {
                '$set': {
                    'ended': True,
                    'winners': [int(w) for w in winners],
                    'ended_at': datetime.now(UTC),
                    'updated_at': datetime.now(UTC),
                }
            },
        )
        return result.modified_count > 0

    async def set_winners(self, message_id: int, winners: list[int]) -> bool:
        """Update or reroll winners for an ended giveaway."""
        result = await self.collection.update_one(
            {'message_id': int(message_id), 'ended': True},
            {
                '$set': {
                    'winners': [int(w) for w in winners],
                    'updated_at': datetime.now(UTC),
                }
            },
        )
        return result.modified_count > 0

    async def delete_giveaway(self, message_id: int) -> bool:
        """Delete a giveaway document."""
        result = await self.collection.delete_one({'message_id': int(message_id)})
        return result.deleted_count > 0
