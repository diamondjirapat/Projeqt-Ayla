import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
    MONGODB_URI = os.getenv('MONGODB_URI')

    COMMAND_PREFIX = os.getenv('PREFIX', '!')
    INTENTS_ALL = True

    # LAVALINK_HOST = os.getenv('LAVALINK_HOST', 'localhost')
    # LAVALINK_PORT = int(os.getenv('LAVALINK_PORT', 2333))
    # LAVALINK_PASSWORD = os.getenv('LAVALINK_PASSWORD', 'youshallnotpass')
    # LAVALINK_SSL = os.getenv('LAVALINK_SSL', 'false').lower() == 'true'

    # LASTFM_API_KEY = os.getenv('LASTFM_API_KEY')
    # LASTFM_API_SECRET = os.getenv('LASTFM_API_SECRET')
    

    
    @classmethod
    def validate(cls):
        if not cls.DISCORD_TOKEN:
            raise ValueError("DISCORD_TOKEN is missing. Check .env file.")
        return True