import motor.motor_asyncio
from pymongo.errors import DuplicateKeyError

from config import Config
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class DatabaseManager:
    def __init__(self):
        self.client = None
        self.db = None

    async def connect(self) -> bool:
        """Connect to MongoDB"""
        if not Config.MONGODB_URI:
            logger.error("MONGODB_URI is not configured")
            return False

        try:
            self.client = motor.motor_asyncio.AsyncIOMotorClient(
                Config.MONGODB_URI,
                serverSelectionTimeoutMS=10_000,
            )

            try:
                parsed = urlparse(Config.MONGODB_URI)
                db_name = parsed.path.lstrip('/')
                if not db_name:
                    db_name = 'discord_bot'
            except Exception:
                db_name = 'discord_bot'

            self.db = self.client[db_name]

            await self.client.admin.command('ping')
            logger.info(f"Connected to MongoDB: {db_name}")

            await self._ensure_indexes()
            return True
        except Exception:
            logger.exception("Failed to connect to MongoDB")
            if self.client is not None:
                self.client.close()
            self.client = None
            self.db = None
            return False

    async def _ensure_indexes(self):
        """Create indexes on the hot lookup fields.

        Unique indexes enforce one document per user/guild (and one prefix per
        entity). If pre-existing duplicates prevent a unique index from being
        created, we fall back to a non-unique index so lookups are still fast.
        """
        index_specs = [
            ('users', 'user_id'),
            ('guilds', 'guild_id'),
            ('user_prefixes', 'user_id'),
            ('guild_prefixes', 'guild_id'),
        ]
        for collection_name, field in index_specs:
            collection = self.db[collection_name]
            try:
                await collection.create_index(field, unique=True)
            except DuplicateKeyError as exc:
                logger.warning(
                    "Unique index on %s.%s could not be created because duplicate "
                    "records already exist (%s); creating a lookup index instead.",
                    collection_name,
                    field,
                    exc,
                )
                try:
                    await collection.create_index(field, unique=False)
                except Exception:
                    logger.exception("Could not create index on %s.%s", collection_name, field)
            except Exception:
                # An existing non-unique index, permissions, or a transient database
                # problem should not prevent the bot from starting.
                logger.exception("Could not ensure unique index on %s.%s", collection_name, field)

    async def disconnect(self):
        if self.client is not None:
            self.client.close()
            self.client = None
            self.db = None
            logger.info("Disconnected from MongoDB")

    def get_collection(self, collection_name: str):
        if self.db is None:
            raise RuntimeError("Database not connected")
        return self.db[collection_name]


db_manager = DatabaseManager()
