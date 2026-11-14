"""URL shape checks shared by the guards that vet media URLs NodeLink will fetch."""

import ipaddress
import re
from urllib.parse import SplitResult

_LDH_HOST = re.compile(r'(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*')


def has_parser_differential(raw_url: str, parts: SplitResult) -> bool:
    """True when Python's urlsplit and a WHATWG parser (NodeLink's ``new URL``) could disagree on the host.

    Backslashes act as path separators for WHATWG but not for urlsplit (``http://127.0.0.1\\x.example/``),
    and percent-encoded or non-LDH host bytes are normalised differently, so such URLs are rejected rather
    than vetted under one interpretation and fetched under another.
    """
    if '\\' in raw_url or any(ord(char) < 0x21 or ord(char) == 0x7F for char in raw_url.strip()):
        return True
    netloc = parts.netloc.rsplit('@', 1)[-1]
    if '%' in netloc:
        return True
    hostname = (parts.hostname or '').lower().rstrip('.')
    if not hostname:
        return True
    if ':' in hostname:  # bracketed IPv6 literal
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError:
            return True
        return False
    return _LDH_HOST.fullmatch(hostname) is None
