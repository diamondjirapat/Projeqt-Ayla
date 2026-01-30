import logging
import asyncio
from typing import Dict, List, Any, Optional, Callable
import wavelink
from datetime import datetime

logger = logging.getLogger(__name__)

class PlaylistLoader:
    @staticmethod
    async def load_playlist(playlist_data: Dict[str, Any], player: wavelink.Player) -> List[wavelink.Playable]:
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
            tracks: wavelink.Search = await wavelink.Playable.search(source_url)
            if not tracks:
                return []
        except Exception as e:
            logger.error(f"Failed to fetch source playlist {source_url}: {e}")
            raise e

        # Convert to list if it's a Playlist container
        if isinstance(tracks, wavelink.Playlist):
            source_tracks = list(tracks.tracks)
        elif isinstance(tracks, list):
            source_tracks = tracks
        else:
            source_tracks = [tracks]

        # Apply modifications
        modifications = playlist_data.get('modifications', {})
        
        # Apply Removals (by track ID/URI)
        removals = set(modifications.get('removals', []))
        if removals:
            source_tracks = [t for t in source_tracks if t.uri not in removals]

        reorder_ids = modifications.get('reorder', [])
        if reorder_ids:
            uri_to_track = {t.uri: t for t in source_tracks}
            
            reordered_tracks = []
            seen_uris = set()
            
            for uri in reorder_ids:
                if uri in uri_to_track:
                    reordered_tracks.append(uri_to_track[uri])
                    seen_uris.add(uri)

            for t in source_tracks:
                if t.uri not in seen_uris:
                    reordered_tracks.append(t)
            
            source_tracks = reordered_tracks

        return source_tracks

    @staticmethod
    async def load_additions_background(
        additions: List[Dict[str, Any]], 
        player: wavelink.Player, 
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
                continue

            try:
                if i > 0:
                    await asyncio.sleep(0.5) # Delay for Lavalink/Rate limits

                tracks = await wavelink.Playable.search(url)
                if not tracks:
                    continue

                track = tracks[0] if isinstance(tracks, list) else tracks
                
                player.queue.put(track)
                
                loaded += 1
                
                if progress_callback:
                    if loaded % 10 == 0 or i == total - 1:
                        if asyncio.iscoroutinefunction(progress_callback):
                            await progress_callback(loaded, total)
                        else:
                            progress_callback(loaded, total)

            except Exception as e:
                logger.error(f"Failed to load addition {url}: {e}")

        return loaded
