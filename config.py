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
    LASTFM_API_KEY = os.getenv('LASTFM_API_KEY')
    LASTFM_API_SECRET = os.getenv('LASTFM_API_SECRET')

    @classmethod
    def validate(cls):
        if not cls.DISCORD_TOKEN:
            raise ValueError("DISCORD_TOKEN is missing. Check .env file.")
        return True