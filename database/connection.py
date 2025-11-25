import motor.motor_asyncio
from config import Config
import logging

logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self):
        self.client = None
        self.db = None
    
    async def connect(self):
        """Connect to MongoDB"""
        try:
            self.client = motor.motor_asyncio.AsyncIOMotorClient(Config.MONGODB_URI)
            db_name = Config.MONGODB_URI.split('/')[-1] if '/' in Config.MONGODB_URI else 'discord_bot'
            self.db = self.client[db_name]

            await self.client.admin.command('ping')
            logger.info(f"Connected to MongoDB: {db_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            return False
    
    async def disconnect(self):
        """Disconnect from MongoDB"""
        if self.client:
            self.client.close()
            logger.info("Disconnected from MongoDB")
    
    def get_collection(self, collection_name: str):
        """Get a collection from the database"""
        if self.db is None:
            raise RuntimeError("Database not connected")
        return self.db[collection_name]


db_manager = DatabaseManager()