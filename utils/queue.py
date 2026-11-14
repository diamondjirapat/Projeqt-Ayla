from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
import socket
import time
from enum import Enum
from typing import Any, Iterable, List, Optional
from urllib.parse import urlsplit

from utils.url_safety import has_parser_differential

import discord
from discord.utils import MISSING
import lava_lyra

logger = logging.getLogger(__name__)


class UnsafeTrackQuery(Exception):
    """Raised when a track query targets a private host or a local file."""


async def assert_safe_track_query(query: str) -> str:
    """Reject URL queries aimed at private hosts and local filesystem paths.

    NodeLink fetches URL queries from the bot host's own network position and
    resolves absolute paths against the bot filesystem, so an attacker-supplied
    query must never target internal addresses or local files. Non-URL values
    (search queries, provider prefixes like `ytsearch:`) pass through.
    """
    query = query.strip()
    if not query:
        return query
    lowered = query.lower()
    if lowered.startswith(('file:', 'ftp:', 'ftps:', 'sftp:')):
        raise UnsafeTrackQuery('Local file queries are not allowed.')
    scheme, sep, rest = query.partition(':')
    if sep and len(scheme) == 1 and scheme.isalpha() and rest.startswith(('\\', '/')):
        # Windows drive-letter path (e.g. C:\Users\...), not a URL scheme.
        raise UnsafeTrackQuery('Local file queries are not allowed.')
    # POSIX absolute paths, Windows UNC shares, and relative traversal never
    # name a playable media source; protocol-relative URLs are unsupported.
    if query.startswith(('/', '\\')) or query.startswith(('../', '..\\')):
        raise UnsafeTrackQuery('Local file queries are not allowed.')
    if sep and rest.startswith('//'):
        try:
            parts = urlsplit(query)
            if parts.scheme not in {'http', 'https'} or not parts.hostname:
                raise ValueError('Unsupported media URL')
            _ = parts.port
        except ValueError as exc:
            raise UnsafeTrackQuery('Invalid media URL.') from exc
        if parts.username or parts.password:
            raise UnsafeTrackQuery('URLs containing credentials are not allowed.')
        if has_parser_differential(query, parts):
            raise UnsafeTrackQuery('Invalid media URL host.')
        hostname = (parts.hostname or '').lower().strip('.')
        if hostname == 'localhost' or hostname.endswith('.local'):
            raise UnsafeTrackQuery('URLs pointing at private hosts are not allowed.')
        try:
            addr_infos = await asyncio.get_running_loop().getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeTrackQuery('Could not resolve the URL host.') from exc
        if not addr_infos:
            raise UnsafeTrackQuery('Could not resolve the URL host.')
        for info in addr_infos:
            address = ipaddress.ip_address(info[4][0])
            if not address.is_global:
                raise UnsafeTrackQuery('URLs pointing at private network addresses are not allowed.')
    return query


class LoopMode(Enum):
    OFF = 0
    NONE = 0
    TRACK = 1
    QUEUE = 2


class QueueException(Exception):
    """Base exception for queue operations."""

    pass


class QueueEmpty(QueueException):
    """Exception raised when getting an item from an empty queue."""

    pass


class QueueFull(QueueException):
    """Exception raised when putting an item into a full queue."""

    pass


class CustomQueue:
    """
    Standalone custom Queue system.
    Maintains intuitive behavior for LoopMode.OFF, LoopMode.TRACK, and LoopMode.QUEUE.
    """

    def __init__(self, max_size: Optional[int] = None, *, overflow: bool = True):
        self.max_size: Optional[int] = max_size
        self._overflow: bool = overflow
        self._queue: List[Any] = []
        self._current: Optional[Any] = None
        self._loop_mode: LoopMode = LoopMode.OFF

    @property
    def current(self) -> Optional[Any]:
        """Return the currently playing track."""
        return self._current

    @property
    def _current_item(self) -> Optional[Any]:
        """Backward compatibility property for internal access to current item."""
        return self._current

    @_current_item.setter
    def _current_item(self, item: Optional[Any]) -> None:
        self._current = item

    @property
    def loop_mode(self) -> LoopMode:
        """Return the current loop mode."""
        return self._loop_mode

    @property
    def is_looping(self) -> bool:
        """Check if any loop mode is enabled."""
        return self._loop_mode != LoopMode.OFF and self._loop_mode != LoopMode.NONE

    @property
    def count(self) -> int:
        """Return the number of upcoming items in the queue."""
        return len(self._queue)

    @property
    def is_empty(self) -> bool:
        """Check if the queue has no upcoming items."""
        if self._loop_mode == LoopMode.QUEUE:
            return len(self._queue) == 0 and self._current is None
        return len(self._queue) == 0

    @property
    def is_full(self) -> bool:
        """Check if the queue is full."""
        if self.max_size is None:
            return False
        return len(self._queue) >= self.max_size

    def _ensure_room(self) -> None:
        if not self.is_full:
            return

        if not self._overflow:
            raise QueueFull(f'Queue max_size of {self.max_size} has been reached.')

        if self._queue:
            self._queue.pop()

    def set_current(self, item: Any) -> None:
        """Set the currently playing track."""
        self._current = item

    def get(self, force_next: bool = False) -> Any:
        """
        Retrieve and return the next track in queue according to current LoopMode.
        If force_next is True (e.g. manual skip), advances to the next song even if LoopMode.TRACK is active.
        """
        if self._loop_mode == LoopMode.TRACK and not force_next:
            if self._current is not None:
                return self._current

        if self._loop_mode == LoopMode.QUEUE:
            if self._current is not None:
                self._queue.append(self._current)

        if not self._queue:
            if self._loop_mode == LoopMode.QUEUE and self._current is not None:
                return self._current
            self._current = None
            raise QueueEmpty('The queue is empty.')

        next_track = self._queue.pop(0)
        self._current = next_track
        return next_track

    def put(self, item: Any) -> None:
        """Add an item to the end of the queue."""
        self._ensure_room()
        self._queue.append(item)

    def put_at_index(self, index: int, item: Any) -> None:
        """Insert an item at a specific index in the upcoming queue."""
        if not 0 <= index <= len(self._queue):
            raise IndexError('Index out of bounds')

        self._ensure_room()
        self._queue.insert(index, item)

    def put_at(self, index: int, item: Any) -> None:
        """Insert an item at a specific index in the upcoming queue."""
        self.put_at_index(index, item)

    def put_at_front(self, item: Any) -> None:
        """Insert an item at the front of the upcoming queue (index 0)."""
        self.put_at_index(0, item)

    def extend(self, items: Iterable[Any]) -> None:
        """Add multiple items to the end of the queue."""
        for item in items:
            self.put(item)

    def move(self, index_from: int, index_to: int) -> None:
        """Move an item from index_from to index_to in the queue."""
        size = len(self._queue)

        if not 0 <= index_from < size:
            raise IndexError('Source index out of bounds')

        if not 0 <= index_to < size:
            raise IndexError('Destination index out of bounds')

        if index_from == index_to:
            return

        item = self._queue.pop(index_from)
        self._queue.insert(index_to, item)

    def remove_at(self, index: int) -> Any:
        """Remove and return the item at index in the queue."""
        if not 0 <= index < len(self._queue):
            raise IndexError('Index out of bounds')

        return self._queue.pop(index)

    def remove(self, item: Any) -> None:
        """Remove the first occurrence of item from the queue."""
        self._queue.remove(item)

    def shuffle(self) -> None:
        """Shuffle the upcoming queue in place."""
        random.shuffle(self._queue)

    def clear(self) -> None:
        """Clear all upcoming tracks from the queue."""
        self._queue.clear()

    def to_list(self) -> List[Any]:
        """Return a copy list of upcoming tracks in queue."""
        return list(self._queue)

    def set_loop_mode(self, mode: LoopMode) -> None:
        """Set the queue loop mode."""
        self._loop_mode = mode

    def disable_loop(self) -> None:
        """Disable loop mode."""
        self._loop_mode = LoopMode.OFF

    def copy(self) -> CustomQueue:
        """Return a copy of the CustomQueue."""
        new_q = CustomQueue(max_size=self.max_size, overflow=self._overflow)
        new_q._queue = list(self._queue)
        new_q._current = self._current
        new_q._loop_mode = self._loop_mode
        return new_q

    def __len__(self) -> int:
        return len(self._queue)

    def __iter__(self):
        return iter(self._queue)

    def __getitem__(self, index: int) -> Any:
        return self._queue[index]

    def __bool__(self) -> bool:
        return not self.is_empty


def _player_history(player: Any) -> List[Any]:
    """Return the player's history list, creating it when missing."""
    history = getattr(player, 'history', None)
    if not isinstance(history, list):
        history = []
        try:
            player.history = history
        except Exception:
            pass
    return history


def move_current_to_queue(player: Any, queue_index: int) -> None:
    """Reposition the playing track toward the upcoming side without interrupting it.

    ``queue_index`` counts how many upcoming tracks should end up behind the
    playing track: those tracks move from the front of the upcoming queue to
    the end of history. The playing track object itself (``player.current``
    and ``player.queue`` current) is never touched, so playback continues.
    ``queue_index=0`` is a no-op (the track already heads the upcoming queue).
    """
    history = _player_history(player)
    queue_list = player.queue._queue
    index = max(0, min(int(queue_index), len(queue_list)))
    if index:
        moving = queue_list[:index]
        del queue_list[:index]
        history.extend(moving)


def move_current_to_history(player: Any, history_index: int) -> None:
    """Reposition the playing track toward the history side without interrupting it.

    ``history_index`` counts how many history tracks should stay behind the
    playing track: history tracks at/after that position move to the front of
    the upcoming queue in order. Playback of ``player.current`` continues.
    ``history_index=len(history)`` is a no-op (the track already follows history).
    """
    history = _player_history(player)
    queue_list = player.queue._queue
    index = max(0, min(int(history_index), len(history)))
    if index < len(history):
        moving = history[index:]
        del history[index:]
        queue_list[0:0] = moving


def get_session_volume(player: Any) -> Optional[int]:
    """Return the volume the user picked for this voice session, if any.

    The value lives on the player so it naturally lasts for the whole
    session (one voice connection) and is forgotten on disconnect, when a
    fresh player is created. Non-integer values (e.g. auto-mocked attrs in
    tests) count as "no session volume".
    """
    vol = getattr(player, 'session_volume', None)
    if isinstance(vol, bool):
        return None
    if isinstance(vol, int) and 0 <= vol <= 150:
        return vol
    return None


def remember_session_volume(player: Any, volume: int) -> None:
    """Record a user-chosen volume so later tracks keep it for this session."""
    try:
        player.session_volume = int(volume)
    except Exception:
        pass


async def apply_session_volume(player: Any, default_volume: int) -> int:
    """Apply the session volume before playback, falling back to the default.

    Returns the volume that was applied. When the user already picked a
    volume this session it is re-applied; otherwise the guild default is
    applied and remembered as the session volume.
    """
    vol = get_session_volume(player)
    if vol is None:
        vol = max(0, min(150, int(default_volume)))
        remember_session_volume(player, vol)
    await player.set_volume(vol)
    return vol


class CustomPlayer(lava_lyra.Player):
    """
    Custom player using our standalone CustomQueue with lava-lyra.
    """

    def __init__(
        self,
        client: discord.Client = MISSING,
        channel: discord.abc.Connectable = MISSING,
        *,
        node: lava_lyra.Node | None = None,
    ):
        super().__init__(client, channel, node=node)

        self.queue: CustomQueue = CustomQueue()
        self.twenty_four_seven: bool = True
        self.autoplay_enabled: bool = True
        self.history: List[lava_lyra.Track] = []
        self.home_channel = None
        self.current_track_start_time = None
        # User-chosen volume for this voice session. None until the first
        # track (guild default) or an explicit volume change sets it.
        self.session_volume: Optional[int] = None
        self._destroying = False
        self._destroyed = False

    @property
    def gapless_enabled(self) -> bool:
        """Gapless playback is always enabled, independent of saved guild settings."""
        return True

    @property
    def is_playing(self) -> bool:
        # NB: gate on the voice connection so a stopped/destroyed player
        # (which keeps a stale _current until cleanup) never reports True.
        return self._current is not None and not self._paused and self._is_connected

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def position(self) -> int:
        # Base Player.position returns 0 whenever is_playing is False, and
        # our is_playing override is False while paused. Report the frozen
        # position instead so pause holds the current time (matching the
        # base-class paused branch) rather than resetting it to 0:00.
        if getattr(self, '_paused', False) and getattr(self, '_current', None) is not None:
            current = self._current
            original = getattr(current, 'original', None)
            if original:
                current = original
            last = getattr(self, '_last_position', 0) or 0
            length = getattr(current, 'length', 0) or 0
            return min(int(last), length) if length else int(last)
        return lava_lyra.Player.position.fget(self)

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    async def set_pause(self, pause: bool) -> None:
        await self._send_player_request({'paused': pause})
        if pause != self._paused:
            # Read using the old pause state, then rebase before switching it.
            # Otherwise pausing loses time since the last node update, and
            # resuming adds the paused interval until the next update arrives.
            self._last_position = self.position
            self._last_update_local = time.time() * 1000
            self._paused = pause

    async def disconnect(self, *, force: bool = False) -> None:
        # Every leave path (idle timeout, recovery, API, or a gateway kick) must
        # delete the NodeLink player as well as closing the Discord connection.
        await self.destroy()

    async def destroy(self) -> None:
        if self._destroying or self._destroyed:
            return
        self._destroying = True
        # Reset local playback state first so late NodeLink events (e.g. a
        # TrackStuck for the stopped track) are recognised as stale. Then
        # close Discord and DELETE the NodeLink player independently: a failed
        # voice-state request must not leave the audio player behind.
        try:
            self.queue.clear()
        except Exception:
            pass
        try:
            self.queue.set_current(None)
        except Exception:
            pass
        self._current = None
        self._next_track = None
        self._paused = False
        try:
            try:
                await super().disconnect(force=True)
            except Exception as e:
                logger.debug(f'Error disconnecting voice during player destroy: {e}')
            finally:
                self._is_connected = False
                self.channel = None
            # A delayed teardown must never remove a newer player from this node.
            if self._node._players.get(self.guild.id) is self:
                self._node._players.pop(self.guild.id, None)
                if self.node.is_connected:
                    try:
                        await self._node.send(method='DELETE', path=self._player_endpoint_uri, guild_id=self.guild.id)
                    except Exception:
                        logger.exception('Failed to delete NodeLink player for guild %s', self.guild.id)
        finally:
            self._destroyed = True
            self._destroying = False
            cog = self.client.get_cog('Music')
            if cog:
                try:
                    await cog.player_disconnected(self)
                except Exception:
                    logger.exception('Failed to clean up voice music player for guild %s', self.guild.id)

    async def get_tracks(self, query: str | dict[str, Any]) -> list[lava_lyra.Track] | lava_lyra.Playlist:
        if isinstance(query, dict):
            query = query.get('uri') or query.get('url') or query.get('query') or (f'ytsearch:{query.get("title", "")} {query.get("author", "")}'.strip() if query.get('title') or query.get('author') else '')
        if not isinstance(query, str):
            query = str(query or '').strip()
        query = await assert_safe_track_query(query)

        # lava-lyra's Node.get_tracks handles URL detection and search prefix
        # application internally, so we just pass the query through.
        return await self.node.get_tracks(query)

    async def play(
        self,
        track: lava_lyra.Track,
        *,
        start: int = 0,
        end: int = 0,
        ignore_if_playing: bool = False,
        gapless: bool = False,
        **kwargs: Any,
    ) -> lava_lyra.Track | None:
        if gapless and self._current is None:
            # gapless=True only preloads nextTrack server-side: it needs a
            # still-playing track to transition from. With nothing loaded it
            # would stage the song without ever starting it and stall the
            # queue/autoplay, so fall back to a normal play.
            gapless = False
        custom_art = getattr(track, 'custom_artwork', None)
        requester = getattr(track, 'requester', None)
        played = await super().play(
            track,
            start=start,
            end=end,
            ignore_if_playing=ignore_if_playing,
            gapless=gapless,
        )
        if custom_art and played:
            played.custom_artwork = custom_art
        if requester and played:
            played.requester = requester
        # A gapless call only stages the next track server-side; what is
        # actually playing is unchanged, so leave the queue current alone.
        if played and not gapless:
            self.queue.set_current(played)
        return played
