"""Normalize legacy and current guild social-alert configuration."""

from typing import Any, Dict
import hashlib

STREAM_ALERT_PLATFORMS = ('twitch', 'youtube')

SOCIAL_ALERT_PLATFORMS = ('twitch', 'youtube', 'x')

SOCIAL_ALERT_EVENTS: Dict[str, tuple] = {
    'twitch': ('live',),
    'youtube': ('video', 'live'),
    'x': ('post',),
}

SOCIAL_ALERT_MAX_ENTRIES = 25


def _normalize_stream_alerts(raw: Any) -> Dict[str, Dict[str, Any]]:
    """Coerce stored/posted stream alert configs to the web panel contract (camelCase)."""
    source = raw if isinstance(raw, dict) else {}
    normalized: Dict[str, Dict[str, Any]] = {}
    for platform in STREAM_ALERT_PLATFORMS:
        cfg = source.get(platform)
        cfg = cfg if isinstance(cfg, dict) else {}
        channel_raw = cfg.get('channelId', cfg.get('channel_id'))
        if channel_raw is None or str(channel_raw).lower() in ('null', 'none', ''):
            channel_id = None
        else:
            channel_id = str(channel_raw)
        normalized[platform] = {
            'enabled': bool(cfg.get('enabled', False)),
            'streamerName': str(cfg.get('streamerName', cfg.get('streamer_name', '')) or ''),
            'channelId': channel_id,
            'mentionRole': str(cfg.get('mentionRole', cfg.get('mention_role', 'none')) or 'none'),
            'customMessage': str(cfg.get('customMessage', cfg.get('custom_message', '')) or ''),
        }
    return normalized


def _normalize_social_alert_entry(raw: Any, index: int) -> Dict[str, Any] | None:
    """Coerce one social-alert entry to the panel contract, or None when unusable."""
    if not isinstance(raw, dict):
        return None
    platform = str(raw.get('platform', '') or '').strip().lower()
    if platform not in SOCIAL_ALERT_PLATFORMS:
        return None
    allowed_events = SOCIAL_ALERT_EVENTS[platform]
    raw_events = raw.get('events', raw.get('event_types', raw.get('eventTypes')))
    events: list[str] = []
    if isinstance(raw_events, (list, tuple)):
        for item in raw_events:
            name = str(item or '').strip().lower()
            if name in allowed_events and name not in events:
                events.append(name)
    if not events:
        events = list(allowed_events)
    handle = raw.get('handle', raw.get('streamerName', raw.get('streamer_name', raw.get('source', ''))))
    handle = str(handle or '').strip()[:150]
    channel_raw = raw.get('channelId', raw.get('channel_id'))
    if channel_raw is None or str(channel_raw).lower() in ('null', 'none', ''):
        channel_id = None
    else:
        channel_id = str(channel_raw)
    entry_id = raw.get('id')
    if not isinstance(entry_id, str) or not entry_id.strip() or len(entry_id) > 64:
        entry_id = hashlib.sha256(f'{index}:{platform}:{handle}'.encode()).hexdigest()[:24]
    else:
        entry_id = entry_id.strip()
    return {
        'id': entry_id,
        'platform': platform,
        'handle': handle,
        'events': events,
        'enabled': bool(raw.get('enabled', False)),
        'channelId': channel_id,
        'mentionRole': str(raw.get('mentionRole', raw.get('mention_role', 'none')) or 'none'),
        'customMessage': str(raw.get('customMessage', raw.get('custom_message', '')) or ''),
    }


def _normalize_social_alerts(raw: Any) -> list[Dict[str, Any]]:
    """Coerce stored/posted social alerts to a bounded list of entries."""
    if isinstance(raw, dict):
        # Tolerate the legacy keyed shape posted to the new key by mistake.
        raw = _social_alerts_from_legacy(raw)
    if not isinstance(raw, list):
        return []
    normalized: list[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        entry = _normalize_social_alert_entry(item, index)
        if entry is None:
            continue
        if entry['id'] in seen_ids:
            entry['id'] = hashlib.sha256(f'{index}:{entry["id"]}'.encode()).hexdigest()[:24]
        seen_ids.add(entry['id'])
        normalized.append(entry)
        if len(normalized) >= SOCIAL_ALERT_MAX_ENTRIES:
            break
    return normalized


def _social_alerts_from_legacy(raw: Any) -> list[Dict[str, Any]]:
    """Convert the legacy single-per-platform stream_alerts dict to social entries."""
    legacy = _normalize_stream_alerts(raw)
    entries: list[Dict[str, Any]] = []
    for platform, events in (('twitch', ['live']), ('youtube', ['video'])):
        cfg = legacy.get(platform, {})
        if not isinstance(cfg, dict):
            continue
        if not cfg.get('streamerName') and not cfg.get('enabled') and not cfg.get('channelId') and not cfg.get('customMessage'):
            continue
        entries.append(
            {
                'id': f'legacy-{platform}',
                'platform': platform,
                'handle': cfg.get('streamerName', ''),
                'events': events,
                'enabled': bool(cfg.get('enabled', False)),
                'channelId': cfg.get('channelId'),
                'mentionRole': cfg.get('mentionRole', 'none'),
                'customMessage': cfg.get('customMessage', ''),
            }
        )
    return entries


def _legacy_stream_alerts_from_social(entries: Any) -> Dict[str, Dict[str, Any]]:
    """Derive the legacy twitch/youtube dict from social entries for old clients."""
    legacy = _normalize_stream_alerts(None)
    if not isinstance(entries, list):
        return legacy
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        platform = entry.get('platform')
        if platform not in STREAM_ALERT_PLATFORMS or legacy[platform].get('streamerName'):
            continue
        if not entry.get('handle') and not entry.get('enabled') and not entry.get('channelId'):
            continue
        legacy[platform] = {
            'enabled': bool(entry.get('enabled', False)),
            'streamerName': str(entry.get('handle', '') or ''),
            'channelId': entry.get('channelId'),
            'mentionRole': str(entry.get('mentionRole', 'none') or 'none'),
            'customMessage': str(entry.get('customMessage', '') or ''),
        }
    return legacy


# ---------------------------------------------------------------------------
# Music embed appearance endpoints
# ---------------------------------------------------------------------------
