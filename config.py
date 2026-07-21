import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    _raw_token = os.getenv('DISCORD_TOKEN', '')
    DISCORD_TOKENS = [t.strip() for t in _raw_token.split(',') if t.strip()]
    DISCORD_TOKEN = DISCORD_TOKENS[0] if DISCORD_TOKENS else None
    MONGODB_URI = os.getenv('MONGODB_URI')

    COMMAND_PREFIX = os.getenv('PREFIX', '!')
    INTENTS_ALL = True

    # Lavalink Configuration
    LAVALINK_URI = os.getenv('LAVALINK_URI', 'http://localhost:8090')
    LAVALINK_PASSWORD = os.getenv('LAVALINK_PASSWORD', 'youshallnotpass')

    # Last.fm
    LASTFM_API_KEY = os.getenv('LASTFM_API_KEY', '')
    LASTFM_API_SECRET = os.getenv('LASTFM_API_SECRET', '')

    # GitHub
    GITHUB_URL = os.getenv('GITHUB_URL', '')

    # Customisation
    MUSIC_BANNER_URL = os.getenv('MUSIC_BANNER_URL', '')
    BAR_URL = os.getenv('BAR_URL', '')

    # Owner ID
    _owner_ids_raw = os.getenv('OWNER_IDS', '368581475660201984')
    OWNER_IDS = set(int(id.strip()) for id in _owner_ids_raw.split(',') if id.strip())

    # Discord OAuth & Web Server
    DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID', '')
    DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET', '')
    DISCORD_REDIRECT_URI = os.getenv('DISCORD_REDIRECT_URI', 'http://localhost:8000/api/auth/callback')
    WEB_PORT = int(os.getenv('WEB_PORT', '8000'))
    SESSION_SECRET_KEY = os.getenv('SESSION_SECRET_KEY', 'secret-session-key')
    WEB_URL = os.getenv('WEB_URL', 'http://localhost:8000')


    @classmethod
    def validate(cls):
        if not cls.DISCORD_TOKENS:
            raise ValueError("DISCORD_TOKEN is missing. Check .env file.")
        return True
