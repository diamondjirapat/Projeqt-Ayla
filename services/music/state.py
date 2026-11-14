"""Cheap session-specific revisions for the player's queue and history."""

import hashlib
import secrets


def timeline_revision(player) -> str:
    session = getattr(player, '_timeline_session', None)
    if session is None:
        session = secrets.token_hex(16)
        player._timeline_session = session
    digest = hashlib.blake2b(digest_size=16)
    digest.update(session.encode())
    # Direct list edits are supported throughout playback and controls. Inspect
    # identities instead of relying on every caller to bump a revision counter.
    for tracks in (player.queue._queue, getattr(player, 'history', [])):
        digest.update(len(tracks).to_bytes(8, 'big'))
        for track in tracks:
            digest.update(id(track).to_bytes(8, 'big'))
            metadata = tuple(
                getattr(track, field, None) for field in ('title', 'author', 'uri', 'length', 'requester', 'artwork')
            )
            digest.update(repr(metadata).encode())
    return digest.hexdigest()
