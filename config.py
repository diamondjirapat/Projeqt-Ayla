import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _csv(name: str) -> list[str]:
    return [value.strip() for value in os.getenv(name, '').split(',') if value.strip()]


def _integer(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw_value!r}") from exc


def _integer_set(name: str) -> set[int]:
    try:
        return {int(value) for value in _csv(name)}
    except ValueError as exc:
        raise ValueError(f"{name} must contain comma-separated integer IDs") from exc


class Config:
    PROJECT_ROOT = Path(__file__).resolve().parent

    DISCORD_TOKENS = _csv('DISCORD_TOKEN')
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
    OWNER_IDS = _integer_set('OWNER_IDS')

    # Discord OAuth & Web Server
    DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID', '')
    DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET', '')
    DISCORD_REDIRECT_URI = os.getenv('DISCORD_REDIRECT_URI', 'http://localhost:8000/api/auth/callback')
    WEB_PORT = _integer('WEB_PORT', 8000)
    SESSION_SECRET_KEY = os.getenv('SESSION_SECRET_KEY', '')
    WEB_URL = os.getenv('WEB_URL', 'http://localhost:8000')
    WEB_ALLOWED_ORIGINS = list(dict.fromkeys([
        WEB_URL.rstrip('/'),
        *_csv('WEB_ALLOWED_ORIGINS'),
    ]))
    SESSION_COOKIE_SECURE = WEB_URL.lower().startswith('https://')

    @classmethod
    def validate(cls):
        missing = []
        if not cls.DISCORD_TOKENS:
            missing.append('DISCORD_TOKEN')
        if not cls.MONGODB_URI:
            missing.append('MONGODB_URI')
        if missing:
            raise ValueError(f"Missing required configuration: {', '.join(missing)}")
        if cls.DISCORD_CLIENT_ID and not cls.DISCORD_CLIENT_SECRET:
            raise ValueError("DISCORD_CLIENT_SECRET is required when Discord OAuth is enabled")
        if cls.DISCORD_CLIENT_ID and len(cls.SESSION_SECRET_KEY) < 32:
            raise ValueError("SESSION_SECRET_KEY must be at least 32 characters when Discord OAuth is enabled")
        return True
