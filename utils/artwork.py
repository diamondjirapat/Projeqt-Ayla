"""Canonical artwork resolution for the web player and Discord embeds."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import re
from typing import Any, Literal, Mapping, Optional
from urllib.parse import quote, urlparse

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_ARTWORK = "/default_artwork.jpg"
LASTFM_MISSING_IMAGE_ID = "2a96cbd8b46e442fc41c2b86b821562f"

ArtworkSource = Literal["provider", "youtube", "itunes", "deezer", "lastfm", "custom", "default"]
MediaKind = Literal["track", "artist", "album", "playlist", "vibe"]


def _value(item: Any, name: str, default: Any = "") -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _clean_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or value == DEFAULT_ARTWORK or LASTFM_MISSING_IMAGE_ID in value:
        return ""
    if value.startswith("/"):
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return value


def normalize_artwork_url(value: Any) -> str:
    """Validate an artwork URL and normalize unreliable YouTube sizes."""
    url = _clean_url(value)
    if not url:
        return ""
    return url.replace("maxresdefault.jpg", "hqdefault.jpg")


def extract_youtube_id(item_or_url: Any) -> str:
    """Extract 11-character YouTube video ID from a track object or URL string."""
    identifier = str(_value(item_or_url, "identifier", "") or "").strip()
    source = str(_value(item_or_url, "source", "") or "").lower()
    uri = str(_value(item_or_url, "uri", "") or _value(item_or_url, "url", "") or "")
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", identifier) and (
        source in {"youtube", "youtubemusic"} or "youtu" in uri.lower() or not uri
    ):
        return identifier
    if not uri and isinstance(item_or_url, str):
        uri = item_or_url
    match = re.search(
        r"(?:youtu\.be/|youtube(?:-nocookie)?\.com/(?:embed/|v/|shorts/|watch\?(?:[^#]*&)?v=))"
        r"([0-9A-Za-z_-]{11})(?:[^0-9A-Za-z_-]|$)",
        uri,
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


@dataclass(frozen=True)
class ArtworkResult:
    artwork: str
    artwork_fallbacks: tuple[str, ...]
    artwork_source: ArtworkSource
    media_kind: MediaKind

    def as_dict(self) -> dict[str, Any]:
        return {
            "artwork": self.artwork,
            "artwork_fallbacks": list(self.artwork_fallbacks),
            "artwork_source": self.artwork_source,
            "media_kind": self.media_kind,
        }


def _provider_artwork(item: Any) -> str:
    # custom_artwork is intentionally excluded to prevent unverified client override
    for field in ("_artwork", "artwork", "artwork_url", "thumbnail"):
        value = normalize_artwork_url(_value(item, field, ""))
        if value:
            return value
    return ""


class ArtworkResolver:
    """Artwork resolver for tracks, artists, and collections with async provider lookups."""

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict[str, ArtworkResult] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "ProjeqtAyla/1.0 (Artwork Resolver)"},
                timeout=aiohttp.ClientTimeout(total=3.0),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def resolve_track(self, track: Any) -> ArtworkResult:
        """Synchronous in-memory track artwork resolution."""
        author = str(_value(track, "author", "") or _value(track, "artist", "") or "").strip()
        title = str(_value(track, "title", "") or _value(track, "name", "") or "").strip()
        cache_key = f"track:{author.lower()}:{title.lower()}"
        if author and title and cache_key in self._cache:
            return self._cache[cache_key]

        provider = _provider_artwork(track)
        if provider:
            source: ArtworkSource = "lastfm" if "last.fm" in provider or "lastfm" in provider else "provider"
            res = ArtworkResult(provider, (), source, "track")
            if author and title:
                self._cache[cache_key] = res
            return res

        yt_id = extract_youtube_id(track)
        if yt_id:
            yt_art = f"https://i.ytimg.com/vi/{yt_id}/hqdefault.jpg"
            res = ArtworkResult(yt_art, (), "youtube", "track")
            if author and title:
                self._cache[cache_key] = res
            return res

        return ArtworkResult(DEFAULT_ARTWORK, (), "default", "track")

    def resolve_track_sync(self, track: Any) -> ArtworkResult:
        return self.resolve_track(track)

    async def resolve_track_async(self, track: Any) -> ArtworkResult:
        """Asynchronous track artwork resolution with iTunes and Deezer fallback."""
        sync_res = self.resolve_track(track)
        if sync_res.artwork != DEFAULT_ARTWORK:
            return sync_res

        author = str(_value(track, "author", "") or _value(track, "artist", "") or "").strip()
        title = str(_value(track, "title", "") or _value(track, "name", "") or "").strip()
        if not title or title.lower() in {"unknown", "unknown track"}:
            return sync_res

        cache_key = f"track:{author.lower()}:{title.lower()}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        session = await self._get_session()

        # 1. Try iTunes Search API
        try:
            query = quote(f"{author} {title}".strip())
            url = f"https://itunes.apple.com/search?term={query}&entity=song&limit=1"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data.get("resultCount", 0) > 0:
                        art = data["results"][0].get("artworkUrl100", "")
                        if art:
                            res = ArtworkResult(art.replace("100x100bb", "600x600bb"), (), "itunes", "track")
                            self._cache[cache_key] = res
                            return res
        except Exception as e:
            logger.debug(f"[ARTWORK] iTunes lookup failed for {author} - {title}: {e}")

        # 2. Try Deezer API
        try:
            query = quote(f"{author} {title}".strip())
            url = f"https://api.deezer.com/search?q={query}&limit=1"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data.get("data"):
                        album_obj = data["data"][0].get("album", {})
                        cover = album_obj.get("cover_big") or album_obj.get("cover_medium") or album_obj.get("cover_xl")
                        if cover:
                            res = ArtworkResult(cover, (), "deezer", "track")
                            self._cache[cache_key] = res
                            return res
        except Exception as e:
            logger.debug(f"[ARTWORK] Deezer lookup failed for {author} - {title}: {e}")

        self._cache[cache_key] = sync_res
        return sync_res

    def resolve_artist(self, artist: Any) -> ArtworkResult:
        """Synchronous artist artwork resolution."""
        name = str(_value(artist, "name", "") or _value(artist, "author", "") or "").strip()
        cache_key = f"artist:{name.lower()}"
        if name and cache_key in self._cache:
            return self._cache[cache_key]

        provider = _provider_artwork(artist)
        if provider:
            source: ArtworkSource = "lastfm" if "last.fm" in provider or "lastfm" in provider else "provider"
            res = ArtworkResult(provider, (), source, "artist")
            if name:
                self._cache[cache_key] = res
            return res

        return ArtworkResult(DEFAULT_ARTWORK, (), "default", "artist")

    async def resolve_artist_async(self, artist: Any) -> ArtworkResult:
        """Asynchronous artist artwork resolution with Deezer lookup."""
        sync_res = self.resolve_artist(artist)
        if sync_res.artwork != DEFAULT_ARTWORK:
            return sync_res

        name = str(_value(artist, "name", "") or _value(artist, "author", "") or "").strip()
        if not name or name.lower() in {"unknown", "unknown artist"}:
            return sync_res

        cache_key = f"artist:{name.lower()}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        session = await self._get_session()

        try:
            query = quote(name)
            url = f"https://api.deezer.com/search/artist?q={query}&limit=1"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data.get("data"):
                        pic = data["data"][0].get("picture_big") or data["data"][0].get("picture_medium")
                        if pic:
                            res = ArtworkResult(pic, (), "deezer", "artist")
                            self._cache[cache_key] = res
                            return res
        except Exception as e:
            logger.debug(f"[ARTWORK] Deezer artist lookup failed for {name}: {e}")

        self._cache[cache_key] = sync_res
        return sync_res

    async def resolve_tracks_batch(self, tracks: list[Any]) -> list[ArtworkResult]:
        """Resolve a batch of tracks concurrently."""
        sem = asyncio.Semaphore(15)

        async def _bound_resolve(t: Any) -> ArtworkResult:
            async with sem:
                return await self.resolve_track_async(t)

        return await asyncio.gather(*[_bound_resolve(t) for t in tracks])

    async def resolve_artists_batch(self, artists: list[Any]) -> list[ArtworkResult]:
        """Resolve a batch of artists concurrently."""
        sem = asyncio.Semaphore(15)

        async def _bound_resolve(a: Any) -> ArtworkResult:
            async with sem:
                return await self.resolve_artist_async(a)

        return await asyncio.gather(*[_bound_resolve(a) for a in artists])

    def resolve_collection(
        self,
        collection: Any,
        *,
        kind: Literal["album", "playlist"] = "playlist",
        tracks: list[Any] | None = None,
    ) -> ArtworkResult:
        custom = normalize_artwork_url(_value(collection, "cover", "") or _value(collection, "custom_cover", ""))
        if custom:
            return ArtworkResult(custom, (), "custom", kind)

        provider = _provider_artwork(collection)
        if provider:
            return ArtworkResult(provider, (), "provider", kind)

        for track in tracks or _value(collection, "tracks", []) or []:
            candidate = self.resolve_track(track).artwork
            if candidate != DEFAULT_ARTWORK:
                return ArtworkResult(candidate, (), "provider", kind)

        return ArtworkResult(DEFAULT_ARTWORK, (), "default", kind)


artwork_resolver = ArtworkResolver()


def artwork_contract(item: Any, *, kind: MediaKind = "track") -> dict[str, Any]:
    if kind == "artist":
        return artwork_resolver.resolve_artist(item).as_dict()
    if kind in {"album", "playlist"}:
        return artwork_resolver.resolve_collection(item, kind=kind).as_dict()
    if kind == "vibe":
        return ArtworkResult(DEFAULT_ARTWORK, (), "default", "vibe").as_dict()
    return artwork_resolver.resolve_track(item).as_dict()


def normalize_saved_track(track: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize saved track dictionary representation."""
    source = dict(track or {})
    canonical_url = str(source.get("canonical_url") or source.get("uri") or source.get("url") or "").strip()
    normalized = {
        "title": source.get("title") or "Unknown",
        "author": source.get("author") or "Unknown",
        "url": canonical_url,
        "uri": canonical_url,
        "canonical_url": canonical_url,
        "length": source.get("length") or 0,
    }
    if source.get("source"):
        normalized["source"] = source["source"]
    candidate = {**source, **normalized}
    normalized.update(artwork_contract(candidate, kind="track"))
    return normalized
