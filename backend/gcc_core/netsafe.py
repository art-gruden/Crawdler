"""SSRF-safe HTTP GET for fetching third-party websites.

Every hop (including redirects) is checked before connecting:

* only http/https and allowed ports;
* the host name is resolved once, and *every* resolved address must be public
  (no loopback, private, link-local, CGNAT, multicast, reserved, cloud metadata …);
* the connection is then pinned to the checked IP, while the original host name is
  used for the Host header, TLS SNI and certificate verification. A DNS answer that
  changes between check and connect (rebinding) therefore cannot reach an internal
  address;
* the response body is read up to a byte limit.

The client deliberately ignores HTTP(S)_PROXY variables (``trust_env=False``): a
proxy would make the IP check meaningless.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .urls import UrlRejected, normalize_host

REDIRECT_CODES = {301, 302, 303, 307, 308}
_EXTRA_HEADERS = {"if-none-match", "if-modified-since", "accept", "accept-language"}

Resolver = Callable[[str, int], Awaitable[list[str]]]


class FetchError(Exception):
    """Base class. ``kind`` is a short machine code."""

    kind = "fetch_error"

    def __init__(self, message: str, url: str = ""):
        super().__init__(message)
        self.url = url


class BlockedAddress(FetchError):
    kind = "blocked_address"


class DnsError(FetchError):
    kind = "dns_error"


class ConnectError(FetchError):
    kind = "unreachable"


class TlsError(FetchError):
    kind = "tls_error"


class TooManyRedirects(FetchError):
    kind = "too_many_redirects"


class InvalidUrl(FetchError):
    kind = "invalid_url"


@dataclass
class NetPolicy:
    allowed_ports: frozenset[int] = frozenset({80, 443})
    allowed_private: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    @classmethod
    def from_settings(cls, settings) -> NetPolicy:
        return cls(
            allowed_ports=frozenset(settings.allowed_ports),
            allowed_private=tuple(ipaddress.ip_network(c, strict=False) for c in settings.allowed_private_cidrs),
        )

    def address_allowed(self, ip: str) -> bool:
        addr = ipaddress.ip_address(ip)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if any(addr in net for net in self.allowed_private):
            return True
        return addr.is_global and not addr.is_multicast


@dataclass
class FetchResult:
    url: str  # requested URL
    final_url: str
    status: int
    headers: httpx.Headers
    body: bytes
    truncated: bool
    ip: str
    redirects: list[str] = field(default_factory=list)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def text(self) -> str:
        charset = "utf-8"
        ct = self.headers.get("content-type", "")
        if "charset=" in ct:
            charset = ct.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DnsError(f"cannot resolve {host}: {exc}") from exc
    return list(dict.fromkeys(info[4][0] for info in infos))


def _check_url(url: str, policy: NetPolicy) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise InvalidUrl(f"unsupported scheme {scheme!r}", url)
    if parts.username or parts.password:
        raise InvalidUrl("credentials in URL", url)
    try:
        host = normalize_host(parts.hostname or "")
        port = parts.port or (443 if scheme == "https" else 80)
    except (UrlRejected, ValueError) as exc:
        raise InvalidUrl(str(exc), url) from exc
    if port not in policy.allowed_ports:
        raise BlockedAddress(f"port {port} not allowed", url)
    return scheme, host, port


async def safe_get(
    url: str,
    *,
    policy: NetPolicy,
    user_agent: str,
    timeout: float = 15.0,
    max_bytes: int = 512 * 1024,
    max_redirects: int = 5,
    accept: str = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    resolver: Resolver = system_resolver,
    before_request: Callable[[str], Awaitable[None]] | None = None,
    client: httpx.AsyncClient | None = None,
    extra_headers: dict[str, str] | None = None,
) -> FetchResult:
    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=0),
        )
    redirects: list[str] = []
    current = url
    try:
        for _ in range(max_redirects + 1):
            scheme, host, port = _check_url(current, policy)
            if before_request is not None:  # e.g. robots.txt and redirect-target checks
                await before_request(current)
            addresses = await resolver(host, port)
            if not addresses:
                raise DnsError(f"no address for {host}", current)
            blocked = [a for a in addresses if not policy.address_allowed(a)]
            if blocked:
                raise BlockedAddress(f"{host} resolves to non-public address {blocked[0]}", current)
            ip = addresses[0]
            parts = urlsplit(current)
            ip_netloc = f"[{ip}]" if ":" in ip else ip
            default_port = 443 if scheme == "https" else 80
            if port != default_port:
                ip_netloc += f":{port}"
            pinned = urlunsplit((scheme, ip_netloc, parts.path or "/", parts.query, ""))
            host_header = host if port == default_port else f"{host}:{port}"
            headers = {
                "Host": host_header,
                "User-Agent": user_agent,
                "Accept": accept,
                "Accept-Language": "de,en;q=0.8",
            }
            if extra_headers:  # only safe, request-shaping headers (conditional GET, Accept)
                headers.update({k: v for k, v in extra_headers.items() if k.lower() in _EXTRA_HEADERS})
            extensions = {"sni_hostname": host} if scheme == "https" else {}
            request = client.build_request("GET", pinned, headers=headers, extensions=extensions)
            try:
                response = await client.send(request, stream=True)
            except httpx.ConnectError as exc:
                if "CERTIFICATE" in str(exc).upper() or "SSL" in str(exc).upper():
                    raise TlsError(str(exc), current) from exc
                raise ConnectError(str(exc) or "connection failed", current) from exc
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                raise ConnectError(f"{type(exc).__name__}: {exc}", current) from exc
            try:
                if response.status_code in REDIRECT_CODES and "location" in response.headers:
                    nxt = urljoin(current, response.headers["location"].strip())
                    redirects.append(nxt)
                    current = nxt.split("#")[0]
                    continue
                body = bytearray()
                truncated = False
                try:
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) >= max_bytes:
                            truncated = True
                            break
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                    if not body:
                        raise ConnectError(f"{type(exc).__name__}: {exc}", current) from exc
                    truncated = True
                raw = bytes(body[:max_bytes])
                return FetchResult(
                    url=url,
                    final_url=current,
                    status=response.status_code,
                    headers=response.headers,
                    body=raw,
                    truncated=truncated,
                    ip=ip,
                    redirects=redirects,
                )
            finally:
                await response.aclose()
        raise TooManyRedirects(f"more than {max_redirects} redirects", url)
    finally:
        if own_client:
            await client.aclose()
