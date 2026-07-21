# NOTE: This file is 100% AI generated.
import hashlib
import logging
import re

import aiohttp

from config import Config

logger = logging.getLogger(__name__)

class LastFMHandler:
    BASE_URL = "https://ws.audioscrobbler.com/2.0/"

    def __init__(self):
        self.enabled = False
        self.api_key = Config.LASTFM_API_KEY
        self.api_secret = Config.LASTFM_API_SECRET
        
        if self.api_key and self.api_secret:
            self.enabled = True
            logger.info("Last.fm module enabled")
        else:
            logger.warning("Last.fm module disabled (missing API keys)")

    @staticmethod
    def clean_track_info(artist, title):
        """Cleans up YouTube titles and extracts artist/title if possible."""
        # Common video tags to remove
        tags_to_remove = [
            r'\(official.*?\)', r'\[official.*?\]',
            r'\(lyric.*?\)', r'\[lyric.*?\]',
            r'\(music video\)', r'\[music video\]',
            r'\(video\)', r'\[video\]',
            r'\(audio\)', r'\[audio\]',
            r'\[mv\]', r'\(mv\)'
        ]
        
        clean_title = title
        for tag in tags_to_remove:
            clean_title = re.sub(tag, '', clean_title, flags=re.IGNORECASE)
        
        clean_title = clean_title.strip()
        
        # If the title is "Artist - Title", extract them
        if ' - ' in clean_title:
            parts = clean_title.split(' - ', 1)
            artist = parts[0].strip()
            clean_title = parts[1].strip()
        
        # Clean up artist channel names (e.g., "ArtistVEVO" or "Artist - Topic")
        if artist.endswith('VEVO'):
            artist = artist[:-4]
        elif artist.endswith(' - Topic'):
            artist = artist[:-8]
            
        return artist.strip(), clean_title.strip()

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

    async def get_auth_data(self, cb=None):
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
                    url = f"https://www.last.fm/api/auth/?api_key={self.api_key}&token={token}"
                    if cb:
                        url += f"&cb={cb}"
                    return url, token
                else:
                    logger.error(f"Failed to get token: {data}")
                    return None, None

    async def get_session_from_token(self, token, url=None):
        """Exchange web token for session key"""
        if not self.enabled or not token:
            return None
        params = {'token': token}
        data = await self._request('auth.getSession', params, post=True)
        
        if data and 'session' in data:
            logger.info(f"[LASTFM] Session obtained for user")
            return data['session']['key']
        logger.warning(f"[LASTFM] Failed to get session from token")
        return None

    async def update_now_playing(self, session_key, artist, title):
        if not self.enabled or not session_key: return
        logger.debug(f"[LASTFM] Updating now playing: {artist} - {title}")

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

    async def get_recent_tracks(self, username, limit=50):
        """Get recent tracks for a user from Last.fm"""
        if not self.enabled or not username:
            return None
        params = {
            'user': username,
            'limit': str(limit)
        }
        return await self._request('user.getRecentTracks', params)

    async def get_top_tracks(self, username, limit=20, period='overall'):
        """Get top tracks for a user from Last.fm"""
        if not self.enabled or not username:
            return None
        params = {
            'user': username,
            'limit': str(limit),
            'period': period
        }
        return await self._request('user.getTopTracks', params)

    async def get_top_artists(self, username, limit=20, period='overall'):
        """Get top artists for a user from Last.fm"""
        if not self.enabled or not username:
            return None
        params = {
            'user': username,
            'limit': str(limit),
            'period': period
        }
        return await self._request('user.getTopArtists', params)

    async def get_similar_artists(self, artist, limit=10):
        """Get similar artists from Last.fm"""
        if not self.enabled or not artist:
            return None
        params = {
            'artist': artist,
            'limit': str(limit)
        }
        return await self._request('artist.getSimilar', params)

    async def get_chart_tracks(self, limit=20):
        """Get global top chart tracks from Last.fm"""
        if not self.enabled:
            return None
        params = {
            'limit': str(limit)
        }
        return await self._request('chart.getTopTracks', params)

    async def get_tag_top_tracks(self, tag, limit=20):
        """Get top tracks for a tag (genre) from Last.fm"""
        if not self.enabled or not tag:
            return None
        params = {
            'tag': tag,
            'limit': str(limit)
        }
        return await self._request('tag.getTopTracks', params)

lastfm_handler = LastFMHandler()