"""Validation helpers shared by HTTP route modules.

Keep request-independent parsing and SSRF checks here so route modules stay
focused on authorization and orchestration.
"""

import asyncio
from datetime import UTC, datetime
import ipaddress
import math
import re
import socket
from typing import Any, Optional
import urllib.parse

from fastapi import HTTPException

from utils.url_safety import has_parser_differential


def detect_image_extension(data: bytes) -> Optional[str]:
    """Return a safe image extension inferred from a file signature."""
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'gif'
    if len(data) >= 12 and data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'webp'
    return None


def detect_evidence_extension(data: bytes) -> Optional[str]:
    """Allow only supported image signatures and PDF evidence."""
    return detect_image_extension(data) or ('pdf' if data.startswith(b'%PDF-') else None)


def parse_integer(value: Any, field: str, *, minimum: int = 0, maximum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)) or (isinstance(value, str) and not re.fullmatch(r'[+-]?[0-9]+', value.strip())) or (isinstance(value, float) and (not math.isfinite(value) or not value.is_integer() or abs(value) > 2**53 - 1)):
        raise HTTPException(status_code=400, detail=f'{field} must be an integer.')
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=400, detail=f'{field} must be an integer.') from exc
    if parsed < minimum or (maximum is not None and parsed > maximum):
        allowed = f'{minimum} to {maximum}' if maximum is not None else f'at least {minimum}'
        raise HTTPException(status_code=400, detail=f'{field} must be {allowed}.')
    return parsed


def parse_iso_datetime(value: Optional[str], field: str) -> Optional[datetime]:
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f'{field} must be an ISO-8601 date or timestamp.')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(status_code=400, detail=f'{field} must be an ISO-8601 date or timestamp.') from exc


def split_url(value: str) -> Optional[urllib.parse.SplitResult]:
    """Return the parsed URL if *value* is an HTTP(S) URL."""
    try:
        parts = urllib.parse.urlsplit(value.strip())
        if parts.scheme in {'http', 'https'}:
            if not parts.hostname:
                raise ValueError('Missing host')
            _ = parts.port
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='Invalid URL.') from exc
    if parts.scheme in {'http', 'https'} and parts.hostname:
        return parts
    return None


async def assert_public_url_host(value: str) -> None:
    """Reject URLs which resolve to non-public network addresses.

    Non-URL values such as search queries and provider prefixes pass through.
    This guards URLs that NodeLink will fetch from the bot host's network.
    """
    parts = split_url(value)
    if parts is None:
        return
    if parts.username or parts.password:
        raise HTTPException(status_code=400, detail='URLs containing credentials are not allowed.')
    if has_parser_differential(value, parts):
        raise HTTPException(status_code=400, detail='Invalid URL host.')
    hostname = parts.hostname.lower().strip('.')
    if hostname == 'localhost' or hostname.endswith('.local'):
        raise HTTPException(status_code=400, detail='URLs pointing at private hosts are not allowed.')
    try:
        addr_infos = await asyncio.get_running_loop().getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HTTPException(status_code=400, detail='Could not resolve the URL host.') from exc
    if not addr_infos:
        raise HTTPException(status_code=400, detail='Could not resolve the URL host.')
    for info in addr_infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise HTTPException(status_code=400, detail='URLs pointing at private network addresses are not allowed.')
