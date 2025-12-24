import pylast
import logging
import asyncio
import urllib.parse

from config import Config

logger = logging.getLogger(__name__)


class LastFMHandler:
    def __init__(self):
        self.enabled = False
        if Config.LASTFM_API_KEY and Config.LASTFM_API_SECRET:
            self.enabled = True
            logger.info("Last.fm module enabled")
        else:
            logger.warning("Last.fm module disabled (missing API keys)")

    def _get_network(self, session_key=None):
        """Helper to get a fresh network instance"""
        network = pylast.LastFMNetwork(
            api_key=Config.LASTFM_API_KEY,
            api_secret=Config.LASTFM_API_SECRET
        )
        if session_key:
            network.session_key = session_key
        return network

    async def get_auth_data(self):
        """Get auth URL and token"""
        if not self.enabled: return None, None
        
        def _task():
            network = self._get_network()
            sg = pylast.SessionKeyGenerator(network)
            url = sg.get_web_auth_url()
            
            # Extract token from URL
            parsed = urllib.parse.urlparse(url)
            token = urllib.parse.parse_qs(parsed.query).get('token', [None])[0]
            return url, token

        return await asyncio.to_thread(_task)

    async def get_session_from_token(self, token, url):
        """Exchange web token for session key"""
        if not self.enabled or not token:
            return None

        def _task():
            network = self._get_network()
            sg = pylast.SessionKeyGenerator(network)
            sg.token = token
            return sg.get_web_auth_session_key(url, token)

        return await asyncio.to_thread(_task)

    async def update_now_playing(self, session_key, artist, title):
        if not self.enabled or not session_key: return
        
        def _task():
            try:
                network = self._get_network(session_key)
                network.update_now_playing(artist=artist, title=title)
            except Exception as e:
                logger.warning(f"Last.fm NP error: {e}")

        await asyncio.to_thread(_task)

    async def scrobble(self, session_key, artist, title, timestamp):
        if not self.enabled or not session_key: return
        
        def _task():
            try:
                network = self._get_network(session_key)
                network.scrobble(artist=artist, title=title, timestamp=timestamp)
            except Exception as e:
                logger.warning(f"Last.fm Scrobble error: {e}")

        await asyncio.to_thread(_task)

    async def get_username_from_session(self, session_key):
        """Get username from session key"""
        if not self.enabled or not session_key: return None

        def _task():
            try:
                network = self._get_network(session_key)
                return network.get_authenticated_user().get_name()
            except Exception as e:
                logger.warning(f"Last.fm Get User error: {e}")
                return None

        return await asyncio.to_thread(_task)


lastfm_handler = LastFMHandler()