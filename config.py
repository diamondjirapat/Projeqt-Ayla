import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
    MONGODB_URI = os.getenv('MONGODB_URI')

    COMMAND_PREFIX = os.getenv('PREFIX', '!')
    INTENTS_ALL = True

    # Lavalink Configuration
    LAVALINK_URI = os.getenv('LAVALINK_URI', 'http://localhost:8090')
    LAVALINK_PASSWORD = os.getenv('LAVALINK_PASSWORD', 'youshallnotpass')

    # Last.fm
    LASTFM_API_KEY = os.getenv('LASTFM_API_KEY', '')
    LASTFM_API_SECRET = os.getenv('LASTFM_API_SECRET', '')
    
    # Customisation
    MUSIC_BANNER_URL = os.getenv('MUSIC_BANNER_URL', '')
    BAR_URL = os.getenv('BAR_URL', '')

    # Owner ID
    _owner_ids_raw = os.getenv('OWNER_IDS', '368581475660201984')
    OWNER_IDS = set(int(id.strip()) for id in _owner_ids_raw.split(',') if id.strip())
    
    @classmethod
    def validate(cls):
        if not cls.DISCORD_TOKEN:
            raise ValueError("DISCORD_TOKEN is missing. Check .env file.")
        return True