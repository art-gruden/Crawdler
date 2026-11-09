"""Polite, SSRF-safe fetching for the career crawler.

Every request goes through :func:`gcc_core.netsafe.safe_get` (public IPs only, IP pinning,
redirect re-checks, size limits). On top of that this module adds:

* robots.txt per host (Protego), including ``Crawl-delay`` (capped);
* a per-domain throttle (registrable domain; also shared ATS hosts) so a domain sees at
  most one request every ``per_domain_delay_s`` seconds across all worker processes;
* a global concurrency limit owned by the resource controller;
* ``Retry-After`` / 429 / 503 handling: the domain is cooled down, the scan stops;
* CAPTCHA/bot-challenge and login-wall detection: never bypassed, the scan stops.
"""

from __future__ import annotations

import asyncio
import re
import time
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
from protego import Protego

from gcc_core.netsafe import FetchError, FetchResult, NetPolicy, Resolver, safe_get, system_resolver
from gcc_core.urls import domain_key

from .config import CrawlerSettings
from .urls import host_of

CHALLENGE = re.compile(
    # interstitial challenge pages only: a reCAPTCHA inside a contact form is not an access wall
    r"(cf-chl-|cf_chl_opt|/cdn-cgi/challenge-platform|ddos-guard\.net|captcha-delivery\.com|px-captcha|"
    r"attention required! \| cloudflare|just a moment\.\.\.|checking your browser|"
    r"verify you are (a )?human|bitte bestätigen sie, dass sie kein roboter sind)",
    re.I,
)
MAX_CRAWL_DELAY_S = 30.0
MAX_RETRY_AFTER_S = 24 * 3600


class CrawlStop(Exception):
    """Stop crawling this site now (not an error of our code)."""

    kind = "stop"

    def __init__(self, message: str, url: str = "", *, retry_after_s: float | None = None, status: int | None = None):
        super().__init__(message)
        self.url = url
        self.retry_after_s = retry_after_s
        self.status = status


class RateLimited(CrawlStop):
    kind = "rate_limited"


class AccessRestricted(CrawlStop):
    kind = "access_restricted"


class RobotsUnavailable(CrawlStop):
    kind = "robots_unavailable"


class RobotsDisallowed(Exception):
    kind = "robots_disallowed"

    def __init__(self, url: str):
        super().__init__(f"robots.txt disallows {url}")
        self.url = url


@dataclass
class Page:
    url: str  # requested
    final_url: str
    status: int
    content_type: str
    body: bytes
    truncated: bool
    elapsed_ms: int
    headers: httpx.Headers
    redirects: list[str] = field(default_factory=list)

    @classmethod
    def from_result(cls, res: FetchResult, elapsed_ms: int) -> Page:
        return cls(
            url=res.url,
            final_url=res.final_url,
            status=res.status,
            content_type=res.content_type,
            body=res.body,
            truncated=res.truncated,
            elapsed_ms=elapsed_ms,
            headers=res.headers,
            redirects=res.redirects,
        )

    def text(self) -> str:
        charset = None
        ct = self.headers.get("content-type", "")
        if "charset=" in ct:
            charset = ct.split("charset=")[-1].split(";")[0].strip().strip('"') or None
        if charset is None:
            m = re.search(rb"<meta[^>]+charset=[\"']?([a-zA-Z0-9_-]+)", self.body[:4096], re.I)
            charset = m.group(1).decode("ascii") if m else "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    @property
    def is_html(self) -> bool:
        return self.content_type in ("text/html", "application/xhtml+xml") or (
            not self.content_type and self.body[:200].lstrip().lower().startswith((b"<!doctype html", b"<html"))
        )

    @property
    def is_pdf(self) -> bool:
        return self.content_type == "application/pdf" or self.body[:5] == b"%PDF-"


def parse_retry_after(value: str | None, default: float) -> float:
    if not value:
        return default
    value = value.strip()
    if value.isdigit():
        return min(float(value), MAX_RETRY_AFTER_S)
    try:
        when = parsedate_to_datetime(value)
        return max(0.0, min(when.timestamp() - time.time(), MAX_RETRY_AFTER_S))
    except (TypeError, ValueError):
        return default


# --- per-domain throttle --------------------------------------------------------------


class Throttle:
    """Spacing between requests to one key (registrable domain)."""

    async def wait(self, key: str, delay_s: float) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def cool_down(self, key: str, seconds: float) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class MemoryThrottle(Throttle):
    def __init__(self, clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep):
        self._next: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._clock = clock
        self._sleep = sleep

    async def wait(self, key: str, delay_s: float) -> None:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = self._clock()
            start = max(now, self._next.get(key, 0.0))
            self._next[key] = start + delay_s
            if len(self._next) > 50_000:  # bounded: forget the oldest entries
                for k in sorted(self._next, key=self._next.get)[:10_000]:
                    self._next.pop(k, None)
                    self._locks.pop(k, None)
        if start > now:
            await self._sleep(start - now)

    async def cool_down(self, key: str, seconds: float) -> None:
        self._next[key] = max(self._next.get(key, 0.0), self._clock() + seconds)


_RESERVE_LUA = """
local now = tonumber(ARGV[1])
local delay = tonumber(ARGV[2])
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
local start = math.max(now, nxt)
redis.call('SET', KEYS[1], tostring(start + delay), 'PX', math.ceil((start + delay - now) * 1000) + 60000)
return tostring(start - now)
"""


class RedisThrottle(Throttle):
    """Cross-process throttle (HTML worker, browser worker, verifier share one view of a domain)."""

    def __init__(self, url: str, prefix: str = "gcc:throttle:"):
        import redis.asyncio as aioredis

        self._redis = aioredis.Redis.from_url(url, socket_timeout=5)
        self._prefix = prefix
        self._script = self._redis.register_script(_RESERVE_LUA)
        self._fallback = MemoryThrottle()

    async def wait(self, key: str, delay_s: float) -> None:
        try:
            wait_s = float(await self._script(keys=[self._prefix + key], args=[time.time(), delay_s]))
        except Exception:  # Redis hiccup: stay polite within this process
            await self._fallback.wait(key, delay_s)
            return
        if wait_s > 0:
            await asyncio.sleep(wait_s)

    async def cool_down(self, key: str, seconds: float) -> None:
        await self._fallback.cool_down(key, seconds)
        try:
            k = self._prefix + key
            until = time.time() + seconds
            cur = await self._redis.get(k)
            if cur is None or float(cur) < until:
                await self._redis.set(k, str(until), px=int(seconds * 1000) + 60000)
        except Exception:
            pass


# --- concurrency limiter adjustable at runtime -------------------------------------------


class AdjustableLimiter:
    """Semaphore whose limit the resource controller can raise or lower while in use."""

    def __init__(self, limit: int):
        self._limit = max(1, limit)
        self._active = 0
        self._cond = asyncio.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active(self) -> int:
        return self._active

    async def set_limit(self, limit: int) -> None:
        async with self._cond:
            self._limit = max(1, limit)
            self._cond.notify_all()

    async def __aenter__(self):
        async with self._cond:
            await self._cond.wait_for(lambda: self._active < self._limit)
            self._active += 1
        return self

    async def __aexit__(self, *exc):
        async with self._cond:
            self._active -= 1
            self._cond.notify_all()


# --- robots ----------------------------------------------------------------------------


class Robots:
    def __init__(self, fetch_raw: Callable[[str], Awaitable[Page]], user_agent: str):
        self._fetch_raw = fetch_raw
        self._ua = user_agent
        self._cache: dict[str, Protego | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def rules(self, url: str) -> Protego | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin not in self._cache:
                self._cache[origin] = await self._load(origin)
        return self._cache[origin]

    async def _load(self, origin: str) -> Protego | None:
        page = await self._fetch_raw(origin + "/robots.txt")
        if page.status in (429,) or page.status >= 500:
            # RFC 9309 §2.3.1.3: unreachable robots.txt means complete disallow.
            raise RobotsUnavailable(f"robots.txt answered {page.status}", origin, status=page.status)
        if page.status >= 400:
            return None
        return Protego.parse(page.text()[:500_000])

    async def check(self, url: str) -> None:
        rules = await self.rules(url)
        if rules is not None and not rules.can_fetch(url, self._ua):
            raise RobotsDisallowed(url)

    async def crawl_delay(self, url: str) -> float | None:
        rules = await self.rules(url)
        if rules is None:
            return None
        delay = rules.crawl_delay(self._ua)
        return min(float(delay), MAX_CRAWL_DELAY_S) if delay else None

    async def sitemaps(self, url: str) -> list[str]:
        rules = await self.rules(url)
        return list(rules.sitemaps) if rules is not None else []


# --- fetcher -----------------------------------------------------------------------------


@dataclass
class FetchStat:
    url: str
    status: int | None
    elapsed_ms: int
    bytes: int
    error: str | None = None


class Fetcher:
    """One per worker process. Thread-unsafe; use from one event loop."""

    def __init__(
        self,
        settings: CrawlerSettings,
        *,
        policy: NetPolicy,
        user_agent: str,
        throttle: Throttle | None = None,
        limiter: AdjustableLimiter | None = None,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
        on_stat: Callable[[FetchStat], None] | None = None,
    ):
        self.settings = settings
        self.policy = policy
        self.user_agent = user_agent
        self.throttle = throttle or MemoryThrottle()
        self.limiter = limiter or AdjustableLimiter(settings.http_concurrency)
        self.resolver = resolver
        self.on_stat = on_stat
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            timeout=httpx.Timeout(settings.http_timeout_s, connect=min(settings.http_timeout_s, 10.0)),
            limits=httpx.Limits(max_connections=max(4, settings.http_concurrency * 2), max_keepalive_connections=0),
        )
        self.robots = Robots(self._fetch_robots, user_agent)
        self._domain_delay: dict[str, float] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _before_hop(self, url: str) -> None:
        """Runs before every hop including redirects: robots.txt + throttle of the target host."""
        await self.robots.check(url)
        await self._throttle(url)

    async def _throttle(self, url: str) -> None:
        host = host_of(url)
        key = domain_key(host) or host
        delay = self._domain_delay.get(key)
        if delay is None:
            crawl_delay = await self.robots.crawl_delay(url)
            delay = max(self.settings.per_domain_delay_s, crawl_delay or 0.0)
            self._domain_delay[key] = delay
        await self.throttle.wait(key, delay)

    async def _fetch_robots(self, robots_url: str) -> Page:
        async def throttle_only(url: str) -> None:
            await self.throttle.wait(domain_key(host_of(url)) or host_of(url), self.settings.per_domain_delay_s)

        return await self._get(robots_url, max_bytes=500_000, before=throttle_only, accept="text/plain,*/*;q=0.5")

    async def _get(
        self,
        url: str,
        *,
        max_bytes: int,
        before: Callable[[str], Awaitable[None]],
        accept: str,
        extra_headers: dict[str, str] | None = None,
    ) -> Page:
        t0 = time.monotonic()
        status = None
        size = 0
        err = None
        try:
            async with self.limiter:
                res = await safe_get(
                    url,
                    policy=self.policy,
                    user_agent=self.user_agent,
                    timeout=self.settings.http_timeout_s,
                    max_bytes=max_bytes,
                    max_redirects=self.settings.max_redirects,
                    accept=accept,
                    resolver=self.resolver,
                    before_request=before,
                    client=self._client,
                    extra_headers=extra_headers,
                )
            status, size = res.status, len(res.body)
            return Page.from_result(res, int((time.monotonic() - t0) * 1000))
        except FetchError as exc:
            err = exc.kind
            raise
        finally:
            if self.on_stat is not None:
                self.on_stat(FetchStat(url, status, int((time.monotonic() - t0) * 1000), size, err))

    async def get(
        self,
        url: str,
        *,
        kind: str = "html",
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> Page:
        """GET with robots, throttle, size limits and stop conditions. Raises CrawlStop / FetchError."""
        max_bytes = {
            "html": self.settings.max_html_bytes,
            "feed": self.settings.max_feed_bytes,
            "sitemap": self.settings.max_feed_bytes,
            "pdf": self.settings.max_pdf_bytes,
        }.get(kind, self.settings.max_html_bytes)
        accept = {
            "html": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.3",
            "feed": "application/json,application/xml,text/xml;q=0.9,*/*;q=0.3",
            "sitemap": "application/xml,text/xml;q=0.9,*/*;q=0.3",
            "pdf": "application/pdf,*/*;q=0.3",
        }.get(kind, "*/*")
        extra = {}
        if etag:
            extra["If-None-Match"] = etag
        if last_modified:
            extra["If-Modified-Since"] = last_modified
        page = await self._get(url, max_bytes=max_bytes, before=self._before_hop, accept=accept, extra_headers=extra)
        await self._check_stop(page)
        if kind == "sitemap" and (page.final_url.endswith(".gz") or page.body[:2] == b"\x1f\x8b"):
            page.body = _gunzip_bounded(page.body, self.settings.max_feed_bytes)
        return page

    async def get_subresource(self, url: str, *, accept: str = "*/*", max_bytes: int = 3 * 1024 * 1024) -> Page:
        """For the browser: scripts/styles/XHR needed to render an allowed page.

        Same SSRF protection (public IPs, pinning, redirect checks, size limit) and global
        concurrency, but no robots.txt lookup and no per-domain spacing — they are part of
        a page fetch that already passed both.
        """

        async def no_check(_u: str) -> None:
            return None

        return await self._get(url, max_bytes=max_bytes, before=no_check, accept=accept)

    async def _check_stop(self, page: Page) -> None:
        key = domain_key(host_of(page.final_url)) or host_of(page.final_url)
        if page.status in (429, 503):
            default = 600.0 if page.status == 429 else 300.0
            retry = parse_retry_after(page.headers.get("retry-after"), default)
            await self.throttle.cool_down(key, retry)
            raise RateLimited(f"HTTP {page.status}", page.final_url, retry_after_s=retry, status=page.status)
        if page.status in (401, 407):
            raise AccessRestricted(f"HTTP {page.status} (login required)", page.final_url, status=page.status)
        if page.status == 403:
            raise AccessRestricted("HTTP 403", page.final_url, status=403)
        if page.status == 200 and len(page.body) < 150_000:
            head = page.body[:150_000].decode("utf-8", errors="ignore")
            if CHALLENGE.search(head):
                raise AccessRestricted("bot protection challenge", page.final_url, status=200)


def _gunzip_bounded(data: bytes, limit: int) -> bytes:
    try:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        return d.decompress(data, limit)
    except zlib.error:
        return b""
