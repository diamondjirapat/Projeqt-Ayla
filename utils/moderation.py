"""Normalization helpers for moderation warning escalation policies."""

from typing import Any


def normalize_escalation_rules(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rules: dict[int, dict[str, Any]] = {}
    for raw in value[:20]:
        if not isinstance(raw, dict) or raw.get('action') not in {'timeout', 'kick', 'ban'}:
            continue
        try:
            points = int(raw.get('points'))
        except (TypeError, ValueError):
            continue
        if points < 1 or points > 10_000:
            continue
        rule: dict[str, Any] = {'points': points, 'action': raw['action']}
        if raw['action'] == 'timeout':
            try:
                duration = int(raw.get('duration_seconds', 3600))
            except (TypeError, ValueError):
                continue
            rule['duration_seconds'] = max(60, min(28 * 24 * 60 * 60, duration))
        rules[points] = rule
    return [rules[points] for points in sorted(rules)]


def select_crossed_escalation_rule(config: Any, previous_points: int, current_points: int) -> dict[str, Any] | None:
    """Return the highest configured threshold crossed by the newest warning."""
    rules = normalize_escalation_rules(config.get('escalation_rules') if isinstance(config, dict) else None)
    crossed = [rule for rule in rules if previous_points < rule['points'] <= current_points]
    return crossed[-1] if crossed else None


def normalize_casebook_config(value: Any) -> dict[str, Any]:
    config = value if isinstance(value, dict) else {}
    try:
        decay_days = int(config.get('warning_decay_days', 90))
    except (TypeError, ValueError):
        decay_days = 90
    try:
        retention_days = int(config.get('case_retention_days', 3650))
    except (TypeError, ValueError):
        retention_days = 3650
    return {
        'warning_decay_days': max(1, min(3650, decay_days)),
        'case_retention_days': max(30, min(3650, retention_days)),
        'appeals_enabled': bool(config.get('appeals_enabled', True)),
        'escalation_rules': normalize_escalation_rules(config.get('escalation_rules')),
    }
