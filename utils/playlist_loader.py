import logging
import asyncio
from typing import Dict, List, Any, Optional, Callable
import pomice

logger = logging.getLogger(__name__)

class PlaylistLoader:
    @staticmethod
    async def load_playlist(playlist_data: Dict[str, Any], player: pomice.Player) -> List[pomice.Track]:
        """
        Load an imported playlist:
        1. Fetch tracks from source URL
        2. Apply modifications (reorder, removals)
        3. Return list of tracks ready for immediate playback (source tracks only)
        """
        source_url = playlist_data.get('source_url')
        if not source_url:
            raise ValueError("No source URL found for imported playlist")

        # 1. Fetch from source
        try:
            tracks = await player.get_tracks(source_url)
            if not tracks:
                return []
        except Exception as e:
            logger.error(f"Failed to fetch source playlist {source_url}: {e}")
            raise e

        # Convert to list if it's a Playlist container
        if isinstance(tracks, pomice.Playlist):
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
        player: pomice.Player, 
        progress_callback: Optional[Callable[[int, int], Any]] = None,
        check_cancel: Optional[Callable[[], bool]] = None
    ):
        """
        Load manual additions in background and add to queue
        """
        total = len(additions)
        loaded = 0

        for i, add_data in enumerate(additions):
            if check_cancel and check_cancel():
                logger.info("Background loading cancelled")
                break

            url = add_data.get('url')
            if not url:
                logger.warning(f"Skipping addition with no URL: {add_data.get('title', 'Unknown')}")
                continue

            try:
                if i > 0:
                    await asyncio.sleep(0.5) # Delay for Lavalink/Rate limits

                tracks = await player.get_tracks(url)
                if not tracks:
                    logger.warning(f"No tracks found for addition URL: {url}")
                    continue

                if isinstance(tracks, pomice.Playlist):
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
                logger.error(f"Failed to load addition {url}: {e}")

        if progress_callback:
            if asyncio.iscoroutinefunction(progress_callback):
                await progress_callback(loaded, total)
            else:
                progress_callback(loaded, total)

        return loaded
