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
        """Create indexes on the hot lookup fields and clean up legacy schema indexes.

        Unique indexes enforce one document per entity/pair. If pre-existing duplicates
        prevent a unique index from being created, we fall back to a non-unique index.
        """
        # Clean up legacy incorrect unique indexes
        try:
            user_levels_col = self.db['user_levels']
            index_info = await user_levels_col.index_information()
            for idx_name, idx_details in index_info.items():
                keys = idx_details.get('key', [])
                if idx_details.get('unique') and len(keys) == 1 and keys[0][0] == 'user_id':
                    logger.warning(
                        "Dropping legacy single-field unique index %r from user_levels "
                        "in favor of compound (guild_id, user_id) index.",
                        idx_name,
                    )
                    await user_levels_col.drop_index(idx_name)
        except Exception:
            logger.exception("Could not check/drop legacy index from user_levels")

        index_specs = [
            # Collection, Index Keys, Unique Flag
            ('users', [('user_id', 1)], True),
            ('guilds', [('guild_id', 1)], True),
            ('user_prefixes', [('user_id', 1)], True),
            ('guild_prefixes', [('guild_id', 1)], True),
            # Leveling: compound unique per guild and user
            ('user_levels', [('guild_id', 1), ('user_id', 1)], True),
            # Leveling: query indexes for aggregations and leaderboards
            ('user_levels', [('user_id', 1)], False),
            ('user_levels', [('guild_id', 1), ('xp', -1)], False),
            # Custom commands: unique command name per guild
            ('custom_commands', [('guild_id', 1), ('name', 1)], True),
            ('custom_commands', [('guild_id', 1)], False),
        ]
        for collection_name, keys, unique in index_specs:
            collection = self.db[collection_name]
            try:
                await collection.create_index(keys, unique=unique)
            except DuplicateKeyError as exc:
                if unique:
                    logger.warning(
                        "Unique index on %s.%s could not be created because duplicate "
                        "records already exist (%s); creating a lookup index instead.",
                        collection_name,
                        keys,
                        exc,
                    )
                    try:
                        await collection.create_index(keys, unique=False)
                    except Exception:
                        logger.exception("Could not create index on %s.%s", collection_name, keys)
                else:
                    logger.exception("DuplicateKeyError creating index on %s.%s", collection_name, keys)
            except Exception:
                logger.exception("Could not ensure index on %s.%s", collection_name, keys)

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
