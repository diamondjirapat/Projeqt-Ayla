# NOTE: This file is 100% AI generated.
import hashlib
import logging

import aiohttp

from config import Config

logger = logging.getLogger(__name__)

class LastFMHandler:
    BASE_URL = "http://ws.audioscrobbler.com/2.0/"

    def __init__(self):
        self.enabled = False
        self.api_key = Config.LASTFM_API_KEY
        self.api_secret = Config.LASTFM_API_SECRET
        
        if self.api_key and self.api_secret:
            self.enabled = True
            logger.info("Last.fm module enabled")
        else:
            logger.warning("Last.fm module disabled (missing API keys)")

    def _sign_call(self, params):
        """
        Sign a method call by generating an api_sig.
        The signature is the MD5 of all parameters (excluding format and callback)
        sorted alphabetically by name, followed by the secret.
        """
        keys = list(params.keys())
        keys.sort()
        sig_str = ""
        for key in keys:
            if key in ('format', 'callback'):
                continue
            sig_str += f"{key}{params[key]}"
        sig_str += self.api_secret
        return hashlib.md5(sig_str.encode('utf-8')).hexdigest()

    async def _request(self, method, params, session_key=None, post=False):
        if not self.enabled:
            return None

        params['api_key'] = self.api_key
        params['method'] = method
        
        if session_key:
            params['sk'] = session_key

        # Signature is required for write methods and auth
        if post or 'sk' in params or method == 'auth.getSession':
            params['api_sig'] = self._sign_call(params)

        params['format'] = 'json'

        async with aiohttp.ClientSession() as session:
            try:
                if post:
                    async with session.post(self.BASE_URL, data=params) as resp:
                        if resp.status != 200:
                            content = await resp.text()
                            logger.error(f"Last.fm API Error ({method}): {resp.status} - {content}")
                            return None
                        return await resp.json()
                else:
                    async with session.get(self.BASE_URL, params=params) as resp:
                        if resp.status != 200:
                            content = await resp.text()
                            logger.error(f"Last.fm API Error ({method}): {resp.status} - {content}")
                            return None
                        return await resp.json()
            except Exception as e:
                logger.error(f"Last.fm Request Failed ({method}): {e}")
                return None

    async def get_auth_data(self):
        """Get auth URL and token"""
        if not self.enabled: return None, None
        params = {}
        params['api_key'] = self.api_key
        params['method'] = 'auth.getToken'
        params['api_sig'] = self._sign_call(params)
        params['format'] = 'json'

        async with aiohttp.ClientSession() as session:
             async with session.get(self.BASE_URL, params=params) as resp:
                data = await resp.json()
                if 'token' in data:
                    token = data['token']
                    url = f"http://www.last.fm/api/auth/?api_key={self.api_key}&token={token}"
                    return url, token
                else:
                    logger.error(f"Failed to get token: {data}")
                    return None, None

    async def get_session_from_token(self, token, url=None):
        """Exchange web token for session key"""
        if not self.enabled or not token:
            return None
        params = {'token': token}
        data = await self._request('auth.getSession', params)
        
        if data and 'session' in data:
            return data['session']['key']
        return None

    async def update_now_playing(self, session_key, artist, title):
        if not self.enabled or not session_key: return

        params = {
            'artist': artist,
            'track': title
        }
        await self._request('track.updateNowPlaying', params, session_key=session_key, post=True)

    async def scrobble(self, session_key, artist, title, timestamp):
        if not self.enabled or not session_key: return

        params = {
            'artist': artist,
            'track': title,
            'timestamp': str(timestamp)
        }
        logger.info(f"Sending scrobble request: {params}")
        result = await self._request('track.scrobble', params, session_key=session_key, post=True)
        logger.info(f"Scrobble result: {result}")

    async def get_username_from_session(self, session_key):
        """Get username from session key"""
        if not self.enabled or not session_key: return None
        data = await self._request('user.getInfo', {}, session_key=session_key)
        if data and 'user' in data:
            return data['user']['name']
        return None

lastfm_handler = LastFMHandler()