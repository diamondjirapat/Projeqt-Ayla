"""Shared JSON contracts for internal API and Discord feature data."""

from datetime import datetime
from typing import Optional

from fastapi.responses import JSONResponse
from utils.artwork import artwork_resolver


def exact_json_numbers(value):
    """Preserve snowflakes that JavaScript cannot represent as integer numbers."""
    if isinstance(value, int) and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, dict):
        return {key: exact_json_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [exact_json_numbers(item) for item in value]
    return value


class PreciseJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return super().render(exact_json_numbers(content))


def serialize_track(track, **extra):
    res = artwork_resolver.resolve_track(track)
    payload = {
        'title': getattr(track, 'title', 'Unknown Track'),
        'author': getattr(track, 'author', 'Unknown Artist'),
        'uri': getattr(track, 'uri', ''),
        'length': getattr(track, 'length', 0),
        'requester': getattr(track, 'requester', 'Unknown'),
    }
    payload.update(res.as_dict())
    payload.update(extra)
    return payload


def _serialize_api_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _serialize_api_value(item) for key, item in value.items() if key != '_id'}
    if isinstance(value, list):
        return [_serialize_api_value(item) for item in value]
    return value


def _serialize_ticket(ticket: dict, reveal_identity: bool) -> dict:
    serialized = _serialize_api_value(ticket)
    if ticket.get('anonymous') and not reveal_identity:
        serialized['member_user_id'] = None
        serialized['identity_restricted'] = True
    return serialized


def _serialize_custom_command(cmd: Optional[dict]) -> dict:
    if not cmd:
        return {}
    res = dict(cmd)
    if '_id' in res:
        res['_id'] = str(res['_id'])
    if hasattr(res.get('created_at'), 'isoformat'):
        res['created_at'] = res['created_at'].isoformat()
    if hasattr(res.get('updated_at'), 'isoformat'):
        res['updated_at'] = res['updated_at'].isoformat()
    return res


def _serialize_giveaway(g: dict) -> dict:
    if not g:
        return {}
    res = dict(g)
    if '_id' in res:
        res['_id'] = str(res['_id'])
    for dt_key in ('end_time', 'created_at', 'updated_at', 'ended_at'):
        if hasattr(res.get(dt_key), 'isoformat'):
            res[dt_key] = res[dt_key].isoformat()
    # Discord snowflakes exceed the JS safe-integer range, so emit every ID
    # as a string. Otherwise a client-side JSON.parse silently rounds the
    # value and end/enter/reroll/delete echo back a message_id that matches
    # nothing (404 'Giveaway not found').
    for id_key in ('message_id', 'guild_id', 'channel_id', 'host_id'):
        if res.get(id_key) is not None:
            res[id_key] = str(res[id_key])
    for list_key in ('entries', 'winners'):
        if isinstance(res.get(list_key), list):
            res[list_key] = [str(v) for v in res[list_key]]
    return res
