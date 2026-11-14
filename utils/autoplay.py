"""Autoplay recommendation logic for the music cog."""

from collections import deque
import logging
import random
import re
from typing import Any, Optional

import lava_lyra

from utils.queue import CustomPlayer


logger = logging.getLogger(__name__)
YOUTUBE_CHANNEL_ID = re.compile(r'UC[A-Za-z0-9_-]{22}')

#: Preset identifiers for the per-guild autoplay preference.
AUTOPLAY_PRESET_DEFAULT = 'default'
AUTOPLAY_PRESET_CUSTOM = 'custom'
AUTOPLAY_PRESETS = (AUTOPLAY_PRESET_DEFAULT, AUTOPLAY_PRESET_CUSTOM)

#: Version of the built-in default recipe. Bump this whenever the values in
#: :data:`DEFAULT_AUTOPLAY_CONFIG` change so custom forks can record which
#: default they were copied from. The default itself is code-only and can
#: never be overwritten through settings - updates only change this constant,
#: and guilds on the ``default`` preset automatically follow the new values
#: while ``custom`` guilds keep their own.
AUTOPLAY_PRESET_VERSION = 2

#: Built-in autoplay recipe. This is the currently implemented behavior,
#: captured verbatim as the immutable ``default`` preset. Never mutate this
#: mapping at runtime - always work on copies via :func:`default_autoplay_config`.
DEFAULT_AUTOPLAY_CONFIG: dict[str, Any] = {
    # Prefer a different artist first; relax the restriction when nothing qualifies.
    'artist_variety': True,
    # Block an artist again after this many consecutive plays by them (1-5).
    'same_artist_streak_limit': 2,
    # How many played URIs / normalized titles are remembered to avoid repeats (0-100, 0 disables).
    'history_size': 50,
    # How many tracks are seeded from player.history into the dedup window (0-50).
    'history_seed_size': 20,
    # How many top-ranked candidates form the weighted sampling pool (1-10).
    'candidate_pool_size': 5,
    # Sample the pool with 1/(rank) weights instead of always taking the top hit.
    'weighted_random': True,
    # Stage 1: provider recommendations (Spotify / YouTube / Deezer via lava-lyra).
    'enable_recommendations': True,
    # Stage 2: cleaned artist name search fallback.
    'enable_artist_fallback': True,
    # Stage 3: "<artist> radio" / "<title> mix" search fallback.
    'enable_radio_fallback': True,
    # YouTube channel IDs (and legacy exact uploader names) excluded from autoplay.
    'channel_blacklist': [],
}

_AUTOPLAY_BOOL_KEYS = (
    'artist_variety',
    'weighted_random',
    'enable_recommendations',
    'enable_artist_fallback',
    'enable_radio_fallback',
)
_AUTOPLAY_INT_BOUNDS = {
    'same_artist_streak_limit': (1, 5),
    'history_size': (0, 100),
    'history_seed_size': (0, 50),
    'candidate_pool_size': (1, 10),
}


def default_autoplay_config() -> dict[str, Any]:
    """Return a copy of the immutable built-in ``default`` preset."""
    return {**DEFAULT_AUTOPLAY_CONFIG, 'channel_blacklist': list(DEFAULT_AUTOPLAY_CONFIG['channel_blacklist'])}


def normalize_channel_blacklist(raw: Any) -> list[str]:
    """Keep up to 100 IDs or legacy names; channel IDs are case-sensitive."""
    if not isinstance(raw, list):
        return []
    names = []
    seen = set()
    for entry in raw:
        if not isinstance(entry, str):
            continue
        name = entry.strip()[:200]
        key = name if YOUTUBE_CHANNEL_ID.fullmatch(name) else name.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(name)
        if len(names) == 100:
            break
    return names


def track_channel_id(track: Any) -> str | None:
    """Read the stable YouTube uploader ID preserved in NodeLink track info."""
    for source in (getattr(track, 'original', None), track):
        info = getattr(source, 'info', None)
        if not isinstance(info, dict):
            continue
        channel_id = info.get('channelId')
        if isinstance(channel_id, str) and YOUTUBE_CHANNEL_ID.fullmatch(channel_id):
            return channel_id
    return None


def is_channel_blocked(track: Any, config: Any) -> bool:
    """Match IDs exactly, with name matching only for pre-existing legacy entries."""
    entries = config.get('channel_blacklist', []) if isinstance(config, dict) else []
    channel_id = track_channel_id(track)
    author = getattr(track, 'author', '')
    author = author.strip().casefold() if isinstance(author, str) else ''
    return any(entry == channel_id if YOUTUBE_CHANNEL_ID.fullmatch(entry) else entry.casefold() == author for entry in entries if isinstance(entry, str))


def normalize_autoplay_config(raw: Any) -> dict[str, Any]:
    """Validate and normalize a user-supplied autoplay config.

    Unknown keys are dropped, missing keys fall back to the current default
    (so new defaults stay forward-compatible), and out-of-range values are
    clamped. Always returns a fresh dict; the default preset is never mutated.
    """
    normalized = default_autoplay_config()
    if not isinstance(raw, dict):
        return normalized
    for key in _AUTOPLAY_BOOL_KEYS:
        if key in raw and raw[key] is not None:
            normalized[key] = bool(raw[key])
    for key, (minimum, maximum) in _AUTOPLAY_INT_BOUNDS.items():
        if key in raw and raw[key] is not None:
            try:
                if isinstance(raw[key], bool):
                    raise ValueError(key)
                value = int(raw[key])
            except (TypeError, ValueError):
                continue
            normalized[key] = max(minimum, min(maximum, value))
    normalized['channel_blacklist'] = normalize_channel_blacklist(raw.get('channel_blacklist'))
    return normalized


def effective_autoplay_config(custom: Any) -> dict[str, Any]:
    """Resolve the config the player should use: custom values over the default."""
    if not isinstance(custom, dict) or not custom:
        return default_autoplay_config()
    return normalize_autoplay_config({**default_autoplay_config(), **custom})


def clean_artist_name(author: str) -> str:
    """Strip feature tags, topic channel suffixes, and extra punctuation from artist name."""
    if not author:
        return ''
    cleaned = re.sub(r'(?i)\s*-\s*topic$', '', author).strip()
    cleaned = re.sub(r'(?i)\s*(feat\.|ft\.|featuring|,|&|\bx\b|\s-\s).*$', '', cleaned).strip()
    return cleaned or author.strip()


def normalize_core_title(title: str, artists: Optional[list[str]] = None) -> str:
    """Normalize a song title to its core essence to detect and prevent duplicate uploads.

    Strips:
    - Quoted subtitles/lyrics (e.g. "One summer night...")
    - Known artist prefixes/names
    - Text in brackets/parentheses: (Slowed + Reverb), [Official Video], (Bass Boosted), etc.
    - Audio/video format tags: 'slowed', 'nightcore', 'sped up', 'tiktok version', '1 hour loop', etc.
    - Non-alphanumeric punctuation and excess whitespace.
    """
    if not title:
        return ''
    t = title.lower()

    # 1. Remove text inside quotes (often lyrics or subtitles)
    t = re.sub(r'[\"“”][^\"“”]*[\"“”]', ' ', t)
    t = re.sub(r'[\'‘’][^\'‘’]*[\'‘’]', ' ', t)
    # Remove standalone apostrophes/quotes so words like "I'll" become "ill"
    t = re.sub(r'[\"\'“”‘’`]', '', t)

    # 2. Strip known artist names from the title
    if artists:
        for artist in artists:
            ca = clean_artist_name(artist).lower()
            if ca and len(ca) >= 2:
                t = re.sub(rf'(?i)\b{re.escape(ca)}\b', ' ', t)

    # 3. If formatted as "Artist - Title", keep the right side
    if ' - ' in t:
        parts = t.split(' - ', 1)
        if parts[1].strip():
            t = parts[1]

    # 4. Remove text in brackets/parentheses: (...), [...], {...}
    t = re.sub(r'[\(\[\{].*?[\)\]\}]', ' ', t)
    # 5. Remove everything after trailing separators |, /, ~
    t = re.sub(r'[\|/~].*$', ' ', t)

    # 6. Remove common video/audio/remix tag keywords
    keywords = [
        r'\bfeat\b.*$',
        r'\bft\b.*$',
        r'\bfeaturing\b.*$',
        r'\bofficial\s+music\s+video\b',
        r'\bofficial\s+video\b',
        r'\bofficial\s+audio\b',
        r'\blyric\s+video\b',
        r'\blyrics\b',
        r'\baudio\b',
        r'\bvideo\b',
        r'\bslowed\s*\+\s*reverb\b',
        r'\bslowed\b',
        r'\breverb\b',
        r'\bsped\s*up\b',
        r'\bspeed\s*up\b',
        r'\bultra\s+slowed\b',
        r'\bbass\s+boosted\b',
        r'\bnightcore\b',
        r'\btik\s*tok\s*(song)?\b',
        r'\b1\s*hour\b',
        r'\bloop\b',
        r'\blooped\b',
        r'\bextended\b',
        r'\bvisualizer\b',
        r'\bmusic\s+video\b',
        r'\bversion\b',
        r'\bcover\b',
    ]
    for kw in keywords:
        t = re.sub(kw, ' ', t, flags=re.IGNORECASE)

    # 7. Strip non-alphanumeric and extra whitespace
    t = re.sub(r'[^\w\s]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


class AutoplayMixin:
    """Autoplay track recommendations backed by the guild playback history."""

    async def _resolve_autoplay_uploader(self, player, track) -> bool:
        """Resolve YouTube identity before selection, including Spotify/Apple search matches."""
        if not isinstance(track, lava_lyra.Track) or track_channel_id(track):
            return True
        original = getattr(track, 'original', None)
        try:
            if original is None and track._search_type:
                # Match lava-lyra's ISRC-first resolution, but inspect the uploader before playing.
                results = None
                if track.isrc:
                    try:
                        results = await player.get_tracks(f'{track._search_type}:{track.isrc}')
                    except Exception:
                        pass
                if not isinstance(results, list) or not results:
                    results = await player.get_tracks(f'{track._search_type}:{track.title} - {track.author}')
                if not isinstance(results, list) or not results:
                    return False
                track.original = results[0]
                track.track_id = results[0].track_id
                original = track.original
            source = original or track
            if source.track_type in (lava_lyra.TrackType.YOUTUBE, lava_lyra.TrackType.YOUTUBE_MUSIC):
                if not track_channel_id(source):
                    loaded = await player.get_tracks(source.uri)
                    tracks = loaded.tracks if isinstance(loaded, lava_lyra.Playlist) else loaded
                    match = next((item for item in tracks or [] if item.identifier == source.identifier), None)
                    channel_id = track_channel_id(match)
                    if channel_id:
                        source.info['channelId'] = channel_id
                return track_channel_id(source) is not None
            return True
        except Exception:
            logger.debug('Could not resolve autoplay uploader identity', exc_info=True)
            return False

    async def _load_autoplay_config(self, guild_id) -> dict[str, Any]:
        """Load the effective autoplay recipe, falling back to the built-in default."""
        try:
            settings = await self.guild_model.get_autoplay_settings(guild_id)
        except Exception:
            settings = None
        if isinstance(settings, dict) and isinstance(settings.get('config'), dict):
            return settings['config']
        return default_autoplay_config()

    async def apply_autoplay_settings(self, player, guild_id=None) -> dict[str, Any]:
        """Refresh a live player's autoplay recipe from guild settings."""
        if guild_id is None:
            guild_id = getattr(getattr(player, 'guild', None), 'id', None)
        config = await self._load_autoplay_config(guild_id)
        try:
            player.autoplay_config = config
        except Exception:
            pass
        return config

    async def _fetch_autoplay_track(self, player: CustomPlayer, last_track: lava_lyra.Track, config: Optional[dict[str, Any]] = None) -> lava_lyra.Track | None:
        """Fetch a recommended track based on the last played track with multi-stage fallback and anti-repetition."""
        guild_id = player.guild.id
        cfg = effective_autoplay_config(config if config is not None else getattr(player, 'autoplay_config', None))

        history_size = cfg['history_size']
        recent_window = max(history_size, cfg['same_artist_streak_limit'], 2)
        seed_size = cfg['history_seed_size'] if history_size > 0 else 0

        played_uris = self.autoplay_played_uris.get(guild_id)
        if played_uris is None or played_uris.maxlen != history_size:
            played_uris = deque(played_uris or [], maxlen=history_size)
            self.autoplay_played_uris[guild_id] = played_uris
        played_titles = self.autoplay_played_titles.get(guild_id)
        if played_titles is None or played_titles.maxlen != history_size:
            played_titles = deque(played_titles or [], maxlen=history_size)
            self.autoplay_played_titles[guild_id] = played_titles
        recent_artists = self.autoplay_recent_artists.get(guild_id)
        if recent_artists is None or recent_artists.maxlen != recent_window:
            recent_artists = deque(recent_artists or [], maxlen=recent_window)
            self.autoplay_recent_artists[guild_id] = recent_artists

        # Seed from player.history (last N tracks) to avoid repeating songs played earlier in session
        if hasattr(player, 'history') and player.history and seed_size > 0:
            for hist_track in list(player.history)[-seed_size:]:
                if hist_track.uri and hist_track.uri not in played_uris:
                    played_uris.append(hist_track.uri)
                hist_norm = normalize_core_title(hist_track.title, [hist_track.author])
                if hist_norm and hist_norm not in played_titles:
                    played_titles.append(hist_norm)

        if last_track.uri and last_track.uri not in played_uris:
            played_uris.append(last_track.uri)
        last_norm = normalize_core_title(last_track.title, [last_track.author])
        if last_norm and last_norm not in played_titles:
            played_titles.append(last_norm)
        last_clean_artist = clean_artist_name(last_track.author)
        if last_clean_artist:
            recent_artists.append(last_clean_artist.lower())

        logger.info(f"[AUTOPLAY RECOMMENDATION] Fetching recommendations: '{last_track.title}' by {last_track.author}")

        def is_valid_candidate(t: lava_lyra.Track, allow_same_artist: bool = True) -> bool:
            if not t or not getattr(t, 'title', None):
                return False
            # Consult the live config too: a command can add a block while a provider call is in flight.
            live_config = getattr(player, 'autoplay_config', None)
            if is_channel_blocked(t, cfg) or is_channel_blocked(t, live_config):
                return False
            if t.uri and t.uri in played_uris:
                return False
            if hasattr(t, 'identifier') and t.identifier:
                if any(t.identifier in u for u in played_uris if u):
                    return False
            if t.title.lower() == last_track.title.lower():
                return False
            t_norm = normalize_core_title(t.title, [t.author, last_track.author])
            if t_norm and t_norm in played_titles:
                return False
            if last_norm and t_norm == last_norm:
                return False
            if not allow_same_artist:
                cand_artist = clean_artist_name(t.author).lower()
                streak = cfg['same_artist_streak_limit']
                recent = list(recent_artists)[-streak:]
                if cand_artist and len(recent) >= streak and recent.count(cand_artist) >= streak:
                    return False
            return True

        async def select_best_candidate(raw_tracks: list[lava_lyra.Track]) -> Optional[lava_lyra.Track]:
            """Select a candidate, prioritizing artist variety and sampling from top results."""
            diverse, relaxed = [], []
            for candidate in raw_tracks:
                if not is_valid_candidate(candidate):
                    continue
                live = getattr(player, 'autoplay_config', None)
                entries = [
                    *cfg['channel_blacklist'],
                    *(live.get('channel_blacklist', []) if isinstance(live, dict) else []),
                ]
                if any(YOUTUBE_CHANNEL_ID.fullmatch(entry) for entry in entries):
                    if not await self._resolve_autoplay_uploader(player, candidate):
                        continue
                    if not is_valid_candidate(candidate):
                        continue
                relaxed.append(candidate)
                if is_valid_candidate(candidate, allow_same_artist=not cfg['artist_variety']):
                    diverse.append(candidate)
                if len(diverse) >= cfg['candidate_pool_size']:
                    break
            candidates = diverse or relaxed
            if not candidates:
                return None
            # Pick from top candidates with weights towards top rankings to avoid deterministic loop
            pool = candidates[: cfg['candidate_pool_size']]
            if not cfg['weighted_random']:
                selected = pool[0]
            if cfg['weighted_random']:
                weights = [1.0 / (i + 1) for i in range(len(pool))]
                selected = random.choices(pool, weights=weights, k=1)[0]
            selected.requester = 'AutoPlay 🎵'

            # Record chosen track in history
            if selected.uri:
                played_uris.append(selected.uri)
            sel_norm = normalize_core_title(selected.title, [selected.author])
            if sel_norm:
                played_titles.append(sel_norm)
            sel_artist = clean_artist_name(selected.author)
            if sel_artist:
                recent_artists.append(sel_artist.lower())

            return selected

        # Stage 1: Built-in recommendations via lava-lyra (Spotify, YouTube, Deezer, etc.)

        try:
            recommended = (await player.get_recommendations(track=last_track)) if cfg['enable_recommendations'] else None
            if recommended:
                tracks = recommended.tracks if isinstance(recommended, lava_lyra.Playlist) else recommended
                selected = await select_best_candidate(tracks)
                if selected:
                    logger.info(f"[AUTOPLAY STAGE 1 - RECOMMENDATION] Selected: '{selected.title}' by {selected.author}")
                    return selected
        except Exception as e:
            logger.warning(f'[AUTOPLAY STAGE 1] Recommendations failed: {e}')

        # Stage 2: Cleaned artist search
        clean_artist = clean_artist_name(last_track.author)
        if clean_artist and cfg['enable_artist_fallback']:
            try:
                logger.info(f"[AUTOPLAY STAGE 2] Falling back to artist search: '{clean_artist}'")
                tracks = await player.get_tracks(clean_artist)
                if tracks:
                    track_list = tracks.tracks if isinstance(tracks, lava_lyra.Playlist) else tracks
                    selected = await select_best_candidate(track_list)
                    if selected:
                        logger.info(f"[AUTOPLAY STAGE 2 - ARTIST] Selected: '{selected.title}' by {selected.author}")
                        return selected
            except Exception as e:
                logger.warning(f'[AUTOPLAY STAGE 2] Search failed: {e}')

        # Stage 3: Title keyword/Radio search fallback to branch out
        try:
            clean_title = re.sub(r'[\(\[\{].*?[\)\]\}]', '', last_track.title).strip()
            query = f'{clean_artist} radio' if clean_artist else f'{clean_title} mix'
            logger.info(f"[AUTOPLAY STAGE 3] Falling back to mix/radio search: '{query}'")
            tracks = (await player.get_tracks(query)) if cfg['enable_radio_fallback'] else None
            if tracks:
                track_list = tracks.tracks if isinstance(tracks, lava_lyra.Playlist) else tracks
                selected = await select_best_candidate(track_list)
                if selected:
                    logger.info(f"[AUTOPLAY STAGE 3 - RADIO] Selected: '{selected.title}' by {selected.author}")
                    return selected
        except Exception as e:
            logger.warning(f'[AUTOPLAY STAGE 3] Search failed: {e}')

        return None
