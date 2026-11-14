import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / '.env')

logger = logging.getLogger(__name__)


def _csv(name: str) -> list[str]:
    return [value.strip() for value in os.getenv(name, '').split(',') if value.strip()]


def _integer(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f'{name} must be an integer, got {raw_value!r}') from exc


def _integer_set(name: str) -> set[int]:
    try:
        return {int(value) for value in _csv(name)}
    except ValueError as exc:
        raise ValueError(f'{name} must contain comma-separated integer IDs') from exc


class Config:
    PROJECT_ROOT = Path(__file__).resolve().parent

    DISCORD_TOKENS = _csv('DISCORD_TOKEN')
    MONGODB_URI = os.getenv('MONGODB_URI')

    COMMAND_PREFIX = os.getenv('PREFIX', '!')
    INTENTS_ALL = True

    # NodeLink Configuration
    NODELINK_URI = os.getenv('NODELINK_URI', 'http://127.0.0.1:3000')
    NODELINK_PASSWORD = os.getenv('NODELINK_PASSWORD', 'youshallnotpass')

    # Last.fm
    LASTFM_API_KEY = os.getenv('LASTFM_API_KEY', '')
    LASTFM_API_SECRET = os.getenv('LASTFM_API_SECRET', '')

    # Social monitoring credentials are deployment secrets, never guild settings.
    SOCIAL_ALERTS_ENABLED = os.getenv('SOCIAL_ALERTS_ENABLED', 'true').lower() in {'true', '1', 'yes'}
    TWITCH_CLIENT_ID = os.getenv('TWITCH_CLIENT_ID', '').strip()
    TWITCH_CLIENT_SECRET = os.getenv('TWITCH_CLIENT_SECRET', '').strip()
    YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY', '').strip()
    X_BEARER_TOKEN = os.getenv('X_BEARER_TOKEN', '').strip()
    SOCIAL_TWITCH_POLL_SECONDS = max(60, _integer('SOCIAL_TWITCH_POLL_SECONDS', 120))
    SOCIAL_YOUTUBE_POLL_SECONDS = max(120, _integer('SOCIAL_YOUTUBE_POLL_SECONDS', 300))
    SOCIAL_YOUTUBE_LIVE_DISCOVERY_SECONDS = max(300, _integer('SOCIAL_YOUTUBE_LIVE_DISCOVERY_SECONDS', 3600))
    SOCIAL_X_POLL_SECONDS = max(300, _integer('SOCIAL_X_POLL_SECONDS', 900))

    # GitHub
    GITHUB_URL = os.getenv('GITHUB_URL', '')

    # Customisation
    MUSIC_BANNER_URL = (os.getenv('MUSIC_BANNER_URL', '') or '').strip()
    if not MUSIC_BANNER_URL.lower().startswith(('http://', 'https://', '/', 'static/')):
        MUSIC_BANNER_URL = ''

    BAR_URL = (os.getenv('BAR_URL', '') or '').strip()
    if not BAR_URL.lower().startswith(('http://', 'https://', '/', 'static/')):
        BAR_URL = ''

    # Owner ID
    OWNER_IDS = _integer_set('OWNER_IDS')

    # Discord OAuth & Web Server
    DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID', '')
    DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET', '')
    DISCORD_REDIRECT_URI = os.getenv('DISCORD_REDIRECT_URI', 'http://localhost:8000/api/auth/callback')
    WEB_PORT = _integer('WEB_PORT', 8000)
    INTERNAL_API_HOST = os.getenv('AYLA_INTERNAL_HOST', '127.0.0.1').strip()
    INTERNAL_API_PORT = _integer('AYLA_INTERNAL_PORT', 9000)
    WEB_URL = (os.getenv('WEB_URL', 'http://localhost:8000') or 'http://localhost:8000').strip().rstrip('/')
    SESSION_SECRET_KEY = os.getenv('SESSION_SECRET_KEY', '')

    @classmethod
    def resolve_session_secret(cls) -> str:
        """Return a safe signing key, never an empty/guessable one.

        A missing or too-short SESSION_SECRET_KEY would let anyone forge
        session cookies (itsdangerous accepts an empty HMAC key). Instead of
        refusing to boot the whole bot, fall back to a random per-boot key:
        sessions simply do not survive restarts until a persistent key is
        configured.
        """
        if len(cls.SESSION_SECRET_KEY) >= 32:
            return cls.SESSION_SECRET_KEY
        if cls.SESSION_SECRET_KEY:
            logger.warning(
                '[CONFIG] SESSION_SECRET_KEY is shorter than 32 characters; '
                'using a random per-boot key instead. Set a persistent key with: '
                'python -c "import secrets; print(secrets.token_hex(32))"'
            )
        else:
            logger.warning(
                '[CONFIG] SESSION_SECRET_KEY is not set; using a random per-boot key. '
                'Web sessions will be invalidated on every restart. Set one with: '
                'python -c "import secrets; print(secrets.token_hex(32))"'
            )
        return secrets.token_hex(32)

    WEB_ALLOWED_ORIGINS = list(
        dict.fromkeys(
            [
                WEB_URL.rstrip('/'),
                *_csv('WEB_ALLOWED_ORIGINS'),
            ]
        )
    )
    SESSION_COOKIE_SECURE = WEB_URL.lower().startswith('https://')

    @classmethod
    def validate(cls):
        missing = []
        if not cls.DISCORD_TOKENS:
            missing.append('DISCORD_TOKEN')
        if not cls.MONGODB_URI:
            missing.append('MONGODB_URI')
        if missing:
            raise ValueError(f'Missing required configuration: {", ".join(missing)}')
        if cls.DISCORD_CLIENT_ID and not cls.DISCORD_CLIENT_SECRET:
            raise ValueError('DISCORD_CLIENT_SECRET is required when Discord OAuth is enabled')
        if cls.DISCORD_CLIENT_ID and len(cls.SESSION_SECRET_KEY) < 32:
            raise ValueError('SESSION_SECRET_KEY must be at least 32 characters when Discord OAuth is enabled')
        return True
