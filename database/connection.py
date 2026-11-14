from pymongo import AsyncMongoClient

from database.indexes import INDEX_SPECS, IndexRequirementError, ensure_required_index, is_legacy_level_index

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
            logger.error('MONGODB_URI is not configured')
            return False

        try:
            self.client = AsyncMongoClient(
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
            logger.info(f'Connected to MongoDB: {db_name}')

            await self._ensure_indexes()
            return True
        except Exception as error:
            if isinstance(error, IndexRequirementError):
                logger.error('Database integrity check failed: %s', error)
            else:
                logger.error('Failed to connect to MongoDB (%s)', type(error).__name__)
            if self.client is not None:
                await self.client.close()
            self.client = None
            self.db = None
            return False

    async def _ensure_indexes(self):
        """Require integrity/retention indexes before allowing application writes."""
        failures = []
        for spec in INDEX_SPECS:
            if not spec.required:
                continue
            try:
                await ensure_required_index(self.db[spec.collection], spec)
            except IndexRequirementError as error:
                failures.append(str(error))
        if failures:
            raise IndexRequirementError('; '.join(failures))

        # Only remove the old cross-guild restriction after the replacement
        # compound unique index has been created and verified successfully.
        levels = self.db['user_levels']
        try:
            indexes = await levels.index_information()
            for name, metadata in indexes.items():
                if is_legacy_level_index(metadata):
                    logger.warning('Removing obsolete cross-guild user_levels index %s', name)
                    await levels.drop_index(name)
        except Exception as error:
            raise IndexRequirementError('Could not remove the obsolete user_levels uniqueness rule') from error

        for spec in INDEX_SPECS:
            if spec.required:
                continue
            try:
                await self.db[spec.collection].create_index(list(spec.keys), unique=False)
            except Exception:
                logger.warning('Optional lookup index unavailable: %s %s', spec.collection, spec.keys)

    async def disconnect(self):
        from database.guild_cache import clear_guild_caches

        clear_guild_caches()
        if self.client is not None:
            await self.client.close()
            self.client = None
            self.db = None
            logger.info('Disconnected from MongoDB')

    def get_collection(self, collection_name: str):
        if self.db is None:
            raise RuntimeError('Database not connected')
        return self.db[collection_name]


db_manager = DatabaseManager()
