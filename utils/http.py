"""Bounded HTTP fetching with DNS pinning and private-network blocking."""

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp


MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _blocked_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


async def resolve_public_url(value: str, allowed_domains: list[str]) -> tuple[str, list[str]]:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('URLs must be bare HTTP or HTTPS URLs without credentials.')
    hostname = parsed.hostname.casefold().strip('.')
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError('IP-literal URLs are not allowed; use a public domain.')
    domains = [str(item).casefold().strip('.') for item in allowed_domains if str(item).strip('.')]
    if domains and not any(hostname == item or hostname.endswith(f'.{item}') for item in domains):
        raise ValueError('The URL host is not in the domain allowlist.')
    if hostname == 'localhost' or hostname.endswith('.local'):
        raise ValueError('Private network hosts are not allowed.')
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(hostname, parsed.port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError('Could not resolve the URL host.') from exc
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not addresses or any(_blocked_ip(ipaddress.ip_address(address)) for address in addresses):
        raise ValueError('URLs cannot resolve to private or non-public network addresses.')
    return hostname, addresses


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, hostname: str, addresses: list[str]):
        self.hostname = hostname
        self.addresses = addresses

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC):
        if host.casefold().strip('.') != self.hostname:
            raise OSError('Unexpected host passed to the pinned resolver.')
        return [
            {
                'hostname': host,
                'host': address,
                'port': port,
                'family': socket.AF_INET6 if ':' in address else socket.AF_INET,
                'proto': 0,
                'flags': 0,
            }
            for address in self.addresses
        ]

    async def close(self) -> None:
        return None


async def safe_http_request(
    method: str,
    url: str,
    *,
    allowed_domains: list[str],
    headers: dict[str, str] | None = None,
    json_body: object | None = None,
    body: bytes | None = None,
    timeout_seconds: float = 10,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
) -> tuple[int, dict[str, str], bytes, str]:
    """Fetch a public URL with redirects revalidated and response size bounded."""
    current = url
    if not 1 <= max_response_bytes <= 8 * 1024 * 1024:
        raise ValueError('HTTP response limit must be between 1 byte and 8 MB.')
    original_scheme = urlsplit(url).scheme
    current_method = method.upper()
    current_headers = dict(headers or {})
    for _ in range(4):
        hostname, addresses = await resolve_public_url(current, allowed_domains)
        connector = aiohttp.TCPConnector(
            resolver=_PinnedResolver(hostname, addresses),
            ttl_dns_cache=0,
        )
        timeout = aiohttp.ClientTimeout(total=max(1, min(30, timeout_seconds)))
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.request(
                current_method,
                current,
                headers=current_headers,
                json=json_body if body is None else None,
                data=body,
                allow_redirects=False,
            ) as response:
                if response.status in {301, 302, 303, 307, 308} and response.headers.get('Location'):
                    destination = urljoin(current, response.headers['Location'])
                    previous = urlsplit(current)
                    following = urlsplit(destination)
                    if (previous.scheme, previous.hostname, previous.port) != (
                        following.scheme,
                        following.hostname,
                        following.port,
                    ):
                        current_headers = {
                            key: value
                            for key, value in current_headers.items()
                            if key.lower() not in {'authorization', 'cookie', 'proxy-authorization', 'host'}
                        }
                    if (
                        response.status == 303
                        and current_method != 'HEAD'
                        or (response.status in {301, 302} and current_method == 'POST')
                    ):
                        current_method = 'GET'
                        json_body = None
                        body = None
                        current_headers = {
                            key: value
                            for key, value in current_headers.items()
                            if key.lower() not in {'content-type', 'content-length', 'transfer-encoding'}
                        }
                    current = destination
                    if original_scheme == 'https' and urlsplit(current).scheme != 'https':
                        raise ValueError('HTTPS requests cannot redirect to insecure HTTP.')
                    continue
                payload = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    if len(payload) + len(chunk) > max_response_bytes:
                        raise ValueError(f'HTTP response exceeded the {max_response_bytes / (1024 * 1024):g} MB limit.')
                    payload.extend(chunk)
                return response.status, dict(response.headers), bytes(payload), current
    raise ValueError('HTTP request exceeded the redirect limit.')
