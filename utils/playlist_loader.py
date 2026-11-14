import logging
import asyncio
from typing import Dict, List, Any, Optional, Callable
import lava_lyra

from utils.queue import assert_safe_track_query

logger = logging.getLogger(__name__)


class PlaylistLoader:
    @staticmethod
    def first_track(results: Any) -> Optional[lava_lyra.Track]:
        """Return the first playable track from a get_tracks result, if any.

        Handles every shape the node can return: a track list, a Playlist
        container, a single track, or an empty/falsy result.
        """
        if not results:
            return None
        if isinstance(results, list):
            return results[0] if results else None
        inner = getattr(results, 'tracks', None)
        if inner is not None:
            return inner[0] if inner else None
        return results

    @staticmethod
    async def resolve_saved_tracks(
        tracks_list: Any,
        player: lava_lyra.Player,
        requester: Optional[str] = None,
        concurrency: int = 10,
    ) -> tuple[List[lava_lyra.Track], int]:
        """Resolve saved track entries to playable tracks, skipping failures.

        Each entry is loaded in isolation: when one track fails to load (bad
        URL, malformed entry, node error), only that track is skipped and the
        rest of the playlist keeps loading. Returns the resolved tracks in
        playlist order plus the number of skipped entries.
        """
        if not isinstance(tracks_list, list):
            return [], 0
        sem = asyncio.Semaphore(concurrency)

        async def _resolve(track_data: Any) -> Optional[lava_lyra.Track]:
            try:
                url = track_data.get('url', '') if isinstance(track_data, dict) else ''
                if not url:
                    return None
                async with sem:
                    url = await assert_safe_track_query(url)
                    track = PlaylistLoader.first_track(await player.get_tracks(url))
                if track is None:
                    return None
                if requester is not None:
                    try:
                        track.requester = requester
                    except Exception:
                        pass
                return track
            except Exception as e:
                logger.warning(f'Skipping saved track that failed to load: {e}')
                return None

        tasks = [_resolve(track_data) for track_data in tracks_list]
        resolved = await asyncio.gather(*tasks, return_exceptions=True)

        ok_tracks: List[lava_lyra.Track] = []
        failed = 0
        for result in resolved:
            if result is None or isinstance(result, BaseException):
                if isinstance(result, BaseException):
                    logger.warning(f'Skipping saved track that failed to load: {result}')
                failed += 1
            else:
                ok_tracks.append(result)
        return ok_tracks, failed

    @staticmethod
    async def load_playlist(playlist_data: Dict[str, Any], player: lava_lyra.Player) -> List[lava_lyra.Track]:
        """
        Load an imported playlist:
        1. Fetch tracks from source URL
        2. Apply modifications (reorder, removals)
        3. Return list of tracks ready for immediate playback (source tracks only)
        """
        source_url = playlist_data.get('source_url')
        if not source_url:
            raise ValueError('No source URL found for imported playlist')

        await assert_safe_track_query(str(source_url))

        # 1. Fetch from source
        try:
            tracks = await player.get_tracks(source_url)
            if not tracks:
                return []
        except Exception as e:
            logger.error(f'Failed to fetch source playlist {source_url}: {e}')
            raise e

        # Convert to list if it's a Playlist container
        if isinstance(tracks, lava_lyra.Playlist):
            source_tracks = list(tracks.tracks)
        elif isinstance(tracks, list):
            source_tracks = tracks
        else:
            source_tracks = [tracks]

        # Apply modifications
        modifications = playlist_data.get('modifications', {})

        removal_list = modifications.get('removals', [])
        if removal_list:
            removals = set()
            for r in removal_list:
                if isinstance(r, dict):
                    removals.add(r.get('url', ''))
                else:
                    removals.add(r)
            source_tracks = [t for t in source_tracks if t.uri not in removals]

        reorder_ids = modifications.get('reorder', [])
        if reorder_ids:
            from collections import defaultdict

            uri_to_tracks = defaultdict(list)
            for t in source_tracks:
                uri_to_tracks[t.uri].append(t)

            reordered_tracks = []

            for uri in reorder_ids:
                if uri_to_tracks[uri]:
                    reordered_tracks.append(uri_to_tracks[uri].pop(0))

            for uri, tracks in uri_to_tracks.items():
                reordered_tracks.extend(tracks)

            source_tracks = reordered_tracks

        return source_tracks

    @staticmethod
    async def load_additions_background(
        additions: List[Dict[str, Any]],
        player: lava_lyra.Player,
        progress_callback: Optional[Callable[[int, int], Any]] = None,
        check_cancel: Optional[Callable[[], bool]] = None,
    ):
        """
        Load manual additions in background and add to queue
        """
        total = len(additions)
        loaded = 0

        for i, add_data in enumerate(additions):
            if check_cancel and check_cancel():
                logger.info('Background loading cancelled')
                break

            url = add_data.get('url')
            if not url:
                logger.warning(f'Skipping addition with no URL: {add_data.get("title", "Unknown")}')
                continue

            try:
                await assert_safe_track_query(str(url))
            except Exception as e:
                logger.error(f'Skipped unsafe addition URL {url}: {e}')
                continue

            try:
                if i > 0:
                    await asyncio.sleep(0.5)  # Delay for NodeLink/Rate limits

                tracks = await player.get_tracks(url)
                if not tracks:
                    logger.warning(f'No tracks found for addition URL: {url}')
                    continue

                if isinstance(tracks, lava_lyra.Playlist):
                    track = tracks.tracks[0]
                elif isinstance(tracks, list):
                    track = tracks[0]
                else:
                    track = tracks

                player.queue.put(track)

                loaded += 1

                if progress_callback and loaded % 5 == 0:
                    if asyncio.iscoroutinefunction(progress_callback):
                        await progress_callback(loaded, total)
                    else:
                        progress_callback(loaded, total)

            except Exception as e:
                logger.error(f'Failed to load addition {url}: {e}')

        if progress_callback:
            if asyncio.iscoroutinefunction(progress_callback):
                await progress_callback(loaded, total)
            else:
                progress_callback(loaded, total)

        return loaded
