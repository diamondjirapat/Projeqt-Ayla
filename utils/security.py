"""Small security helpers shared by persistence and web features."""

from collections.abc import Mapping
from typing import Any


_SENSITIVE_KEY_PARTS = ('token', 'secret', 'password', 'session_key', 'oauth', 'cookie')


def redact_sensitive(value: Any, key: str | None = None) -> Any:
    """Recursively redact values whose keys commonly contain credentials."""
    if key and any(part in key.lower() for part in _SENSITIVE_KEY_PARTS):
        return '[REDACTED]'
    if isinstance(value, Mapping):
        return {str(item_key): redact_sensitive(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    return value
