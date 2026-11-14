"""Playlists persistence and domain rules."""

from typing import Optional, Dict, Any
import logging
import re
from utils.artwork import normalize_saved_track


logger = logging.getLogger(__name__)


def _normalize_playlist(playlist: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not playlist:
        return playlist
    normalized = dict(playlist)
    normalized['tracks'] = [
        normalize_saved_track(track) for track in playlist.get('tracks', []) if isinstance(track, dict)
    ]
    modifications = playlist.get('modifications')
    if isinstance(modifications, dict):
        normalized_modifications = dict(modifications)
        normalized_modifications['additions'] = [
            normalize_saved_track(track) for track in modifications.get('additions', []) if isinstance(track, dict)
        ]
        normalized['modifications'] = normalized_modifications
    return normalized


def _playlist_key(name: str) -> str:
    """Sanitize a playlist name into a MongoDB-safe field key.

    MongoDB interprets dots in field names as nested-path separators and a
    leading ``$`` as an operator, so a playlist named ``"My.Mix"`` would
    otherwise be stored as ``playlists.my.mix`` and corrupt the document.
    Whitespace is collapsed to underscores and any ``.``/``$`` is replaced.
    """
    if not isinstance(name, str):
        raise TypeError('Playlist name must be a string')

    key = re.sub(r'\s+', '_', name.strip().lower())
    key = re.sub(r'[.$]', '_', key)
    if not key:
        raise ValueError('Playlist name cannot be empty')
    return key
