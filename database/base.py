"""Base persistence and domain rules."""

from datetime import UTC, datetime
from typing import Dict, Any
import logging
from database.connection import db_manager


logger = logging.getLogger(__name__)


class BaseModel:
    def __init__(self, collection_name: str):
        self.collection_name = collection_name

    @property
    def collection(self):
        injected = getattr(self, '_collection', None)
        if injected is not None:
            return injected
        return db_manager.get_collection(self.collection_name)

    async def _upsert(self, identity: Dict[str, int], update_data: Dict[str, Any]) -> bool:
        """Update a document without mutating the caller's mapping."""
        now = datetime.now(UTC)
        values = {**update_data, 'updated_at': now}
        values.pop('created_at', None)
        result = await self.collection.update_one(
            identity,
            {
                '$set': values,
                '$setOnInsert': {'created_at': now},
            },
            upsert=True,
        )
        return result.modified_count > 0 or result.upserted_id is not None
