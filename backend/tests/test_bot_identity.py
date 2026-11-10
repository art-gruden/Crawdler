"""Crawler identity: one User-Agent from the environment, robots.txt product token, the public
/api/public/bot endpoint and the guarantee that everything else stays behind the login.

No real websites are contacted: local servers, httpx transports and fakes only.
"""

from __future__ import annotations

import asyncio
import ipaddress
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from career_crawler.browser import BrowserRenderer
from career_crawler.config import CrawlerSettings
from career_crawler.fetch import Fetcher, MemoryThrottle, RobotsDisallowed
from company_discovery.verify import RobotsCache, verify_many
from company_discovery.verify import RobotsDisallowed as VerifyRobotsDisallowed
from gcc_core.botinfo import contact_email, robots_token
from gcc_core.config import Settings, get_settings
from gcc_core.netsafe import NetPolicy

BACKEND = Path(__file__).parent.parent
MOZILLA_UA = "Mozilla/5.0 (compatible; GermanyCareerCrawler/0.1; +https://crawdler.example/bot)"
PLAIN_UA = "GermanyCareerCrawler/0.1 (+https://crawdler.example/bot)"
H = {"X-Requested-With": "gcc-ui"}


@pytest.fixture
def env_ua(monkeypatch):
    """Set CRAWLER_USER_AGENT (and optionally more) through the real settings mechanism."""

    def apply(ua: str, **extra: str) -> Settings:
        monkeypatch.setenv("CRAWLER_USER_AGENT", ua)
        for k, v in extra.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
        return get_settings()

    yield apply
    monkeypatch.undo()
    get_settings.cache_clear()


# --- 1. configuration -----------------------------------------------------------------------


def test_user_agent_is_loaded_from_environment(env_ua):
    assert env_ua(MOZILLA_UA).crawler_user_agent == MOZILLA_UA


def test_there_is_no_second_user_agent_setting():
    """CrawlerSettings (CRAWLER_ prefix) must not grow its own, possibly conflicting, identity."""
    assert not [f for f in CrawlerSettings.model_fields if "agent" in f]
    assert [f for f in Settings.model_fields if "agent" in f] == ["crawler_user_agent"]


def test_compose_forwards_env_file_and_never_overrides_the_user_agent():
    compose = BACKEND.parent / "docker-compose.yml"
    if not compose.exists():
        pytest.skip("docker-compose.yml not available")
    spec = yaml.safe_load(compose.read_text("utf-8"))
    assert spec["x-backend"]["env_file"] in (".env", [".env"])
    for name, service in spec["services"].items():
        env = service.get("environment") or {}
        env = env if isinstance(env, dict) else dict(e.split("=", 1) for e in env)
        assert "CRAWLER_USER_AGENT" not in env, f"{name} overrides the User-Agent"
        # api, workers and beat are built from ./backend (or merge the x-backend anchor): all load .env
        if service.get("build") or service.get("image", "").startswith("gcc-backend"):
            if name != "web":
                assert service.get("env_file") in (".env", [".env"]), f"{name} does not load .env"


# --- 2. robots.txt product token ------------------------------------------------------------


@pytest.mark.parametrize(
    "ua, token",
    [
        (PLAIN_UA, "GermanyCareerCrawler"),
        (MOZILLA_UA, "GermanyCareerCrawler"),
        ("Mozilla/5.0 (compatible;GermanyCareerCrawler/2; +https://x)", "GermanyCareerCrawler"),
        ("germanycareercrawler", "germanycareercrawler"),
        ("My-Bot_2/1.0 contact: ops@example.org", "My-Bot_2"),
        ("Mozilla/5.0 (X11; Linux) SomeBot/1.2", "SomeBot"),
    ],
)
def test_robots_token(ua, token):
    assert robots_token(ua) == token


@pytest.mark.parametrize(
    "configured, ua, expected",
    [
        ("ops@example.org", PLAIN_UA, "ops@example.org"),
        ("", "GermanyCareerCrawler/0.1 (+https://x/bot; mailto:ops@example.org)", "ops@example.org"),
        ("not an address", PLAIN_UA, None),  # never invent or accept garbage
        ("", PLAIN_UA, None),
    ],
)
def test_contact_email(configured, ua, expected):
    assert contact_email(configured, ua) == expected


ROBOTS = """\
User-agent: GermanyCareerCrawlerX
Disallow: /

User-agent: GermanyCareerCrawler
Disallow: /intern/
Crawl-delay: 7

User-agent: *
Disallow: /
"""


class _Resp:
    status = 200

    def text(self):
        return ROBOTS


@pytest.mark.parametrize("ua", [PLAIN_UA, MOZILLA_UA])
def test_verification_robots_uses_the_product_token(ua):
    async def fetch(_url):
        return _Resp()

    async def run():
        cache = RobotsCache(fetch)
        await cache.check("https://firma.de/jobs", ua)  # our group allows it; "*" and ...X do not apply
        with pytest.raises(VerifyRobotsDisallowed):
            await cache.check("https://firma.de/intern/x", ua)

    asyncio.run(run())


def test_other_groups_do_not_match_through_the_mozilla_prefix():
    """A group for "Mozilla" (a browser-compat prefix, not us) must not apply to our requests: the
    token, not the whole User-Agent string, is what robots.txt groups are matched against."""

    class Resp:
        status = 200

        def text(self):
            return "User-agent: Mozilla\nDisallow: /\n"

    async def fetch(_url):
        return Resp()

    asyncio.run(RobotsCache(fetch).check("https://firma.de/jobs", MOZILLA_UA))  # does not raise


# --- 3. website verification sends the configured User-Agent; disallowed URLs are not fetched --


class _Recorder(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, str]] = []  # host, path, user-agent

    def do_GET(self):  # noqa: N802
        host = self.headers["Host"].split(":")[0]
        self.seen.append((host, self.path, self.headers.get("User-Agent", "")))
        if self.path == "/robots.txt":
            body, status = b"User-agent: GermanyCareerCrawler\nDisallow: /karriere/\n", 200
        elif host == "gesperrt.de":
            body, status = b"never served", 200
        else:
            body, status = b"<html><title>Beispiel GmbH</title><body>Beispiel GmbH Dresden</body></html>", 200
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def recorder():
    _Recorder.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def test_verification_sends_the_configured_user_agent(env_ua, recorder):
    settings = env_ua(MOZILLA_UA)
    port = recorder.server_port
    policy = NetPolicy(
        allowed_ports=frozenset({80, 443, port}), allowed_private=(ipaddress.ip_network("127.0.0.1/32"),)
    )

    async def resolver(host, _port):
        return ["127.0.0.1"]

    results = asyncio.run(
        verify_many(
            [(1, f"http://beispiel.de:{port}/", "Beispiel GmbH", "beispiel.de")],
            policy=policy,
            settings=settings,
            resolver=resolver,
        )
    )
    assert results[1].status == "verified"
    assert {p for _h, p, _ua in _Recorder.seen} == {"/robots.txt", "/"}
    assert {ua for _h, _p, ua in _Recorder.seen} == {MOZILLA_UA}


def test_verification_does_not_fetch_a_disallowed_url(env_ua, recorder):
    settings = env_ua(PLAIN_UA)
    port = recorder.server_port
    policy = NetPolicy(
        allowed_ports=frozenset({80, 443, port}), allowed_private=(ipaddress.ip_network("127.0.0.1/32"),)
    )

    async def resolver(host, _port):
        return ["127.0.0.1"]

    results = asyncio.run(
        verify_many(
            [(1, f"http://beispiel.de:{port}/karriere/", "Beispiel GmbH", "beispiel.de")],
            policy=policy,
            settings=settings,
            resolver=resolver,
        )
    )
    assert results[1].status == "robots_disallowed"
    assert [p for _h, p, _ua in _Recorder.seen] == ["/robots.txt"]  # the page itself was never requested


# --- 4. career crawler: same identity, robots.txt, crawl-delay -------------------------------


class _Site(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests: list[tuple[str, str]] = []  # path, user-agent

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((path, request.headers.get("user-agent", "")))
        if path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS, request=request)
        return httpx.Response(200, html="<html><title>ok</title></html>", request=request)


async def _public(_host, _port):
    return ["93.184.215.14"]  # public-looking, never connected to (transport is injected)


def _fetcher(ua: str) -> tuple[Fetcher, _Site]:
    site = _Site()
    settings = CrawlerSettings(per_domain_delay_s=0.0, browser_enabled=False)
    return (
        Fetcher(
            settings, policy=NetPolicy(), user_agent=ua, transport=site, resolver=_public, throttle=MemoryThrottle()
        ),
        site,
    )


@pytest.mark.parametrize("ua", [PLAIN_UA, MOZILLA_UA])
def test_crawler_obeys_robots_for_its_token_and_never_fetches_disallowed(ua):
    async def run():
        f, site = _fetcher(ua)
        try:
            await f.get("https://firma.de/jobs")  # allowed for GermanyCareerCrawler
            with pytest.raises(RobotsDisallowed):
                await f.get("https://firma.de/intern/geheim")
            assert await f.robots.crawl_delay("https://firma.de/jobs") == 7.0
        finally:
            await f.aclose()
        return site

    site = asyncio.run(run())
    paths = [p for p, _ in site.requests]
    assert "/intern/geheim" not in paths
    assert paths.count("/robots.txt") == 1 and "/jobs" in paths
    assert {u for _p, u in site.requests} == {ua}  # robots.txt and pages carry the same identity


# --- 5. browser data requests honour robots.txt -------------------------------------------------


class _Route:
    def __init__(self):
        self.action = None

    async def abort(self, reason="failed"):
        self.action = ("abort", reason)

    async def fulfill(self, **kw):
        self.action = ("fulfill", kw["status"])


def _xhr(url, kind="xhr"):
    frame = object()
    return (
        SimpleNamespace(
            url=url,
            method="GET",
            resource_type=kind,
            headers={"accept": "application/json"},
            frame=frame,
            is_navigation_request=lambda: False,
        ),
        SimpleNamespace(main_frame=frame),
    )


def test_browser_xhr_is_checked_against_robots_but_static_assets_are_not():
    async def run():
        f, site = _fetcher(PLAIN_UA)
        r = BrowserRenderer(CrawlerSettings(), f)
        out = {}
        try:
            for name, url, kind in [
                ("blocked-xhr", "https://firma.de/intern/jobs.json", "xhr"),
                ("allowed-xhr", "https://firma.de/api/jobs.json", "fetch"),
                ("asset", "https://firma.de/intern/app.js", "script"),
            ]:
                req, page = _xhr(url, kind)
                route = _Route()
                await r._handle(route, req, page, {"requests": 0, "cache": {}, "stop": None})
                out[name] = route.action
        finally:
            await f.aclose()
        return out, site

    out, site = asyncio.run(run())
    assert out["blocked-xhr"] == ("abort", "blockedbyclient")
    assert out["allowed-xhr"] == ("fulfill", 200)
    assert out["asset"] == ("fulfill", 200)  # part of an already allowed page
    assert "/intern/jobs.json" not in [p for p, _ in site.requests]


# --- 6./7. public bot page data; the dashboard stays protected ---------------------------------


@pytest.fixture
def client(env_ua):
    env_ua(MOZILLA_UA)
    from gcc_core.app import create_app

    return TestClient(create_app())


def test_bot_info_is_public_and_leaks_nothing(client, env_ua):
    env_ua(MOZILLA_UA, BOT_CONTACT_EMAIL="crawler-admin@example.org")
    r = client.get("/api/public/bot")  # no cookie, no CSRF header
    assert r.status_code == 200
    body = r.json()
    assert body["user_agent"] == MOZILLA_UA
    assert body["robots_token"] == "GermanyCareerCrawler"
    assert body["contact_email"] == "crawler-admin@example.org"
    assert body["per_domain_delay_s"] > 0 and body["max_crawl_delay_s"] > 0
    assert set(body) == {
        "name",
        "user_agent",
        "robots_token",
        "contact_email",
        "per_domain_delay_s",
        "max_crawl_delay_s",
    }
    s = get_settings()
    for secret in (s.secret_key, s.admin_password, "postgresql", "redis://"):
        assert secret not in r.text


def test_bot_info_without_contact_does_not_invent_one(client):
    assert client.get("/api/public/bot").json()["contact_email"] is None


UNPROTECTED = {
    ("GET", "/api/health"),
    ("GET", "/api/public/bot"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
}


def test_every_other_endpoint_requires_login(client):
    """Walks the OpenAPI schema: anything not explicitly public answers 401 without a session."""
    checked = 0
    for path, ops in client.app.openapi()["paths"].items():
        for method in ops:
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"} or (method.upper(), path) in UNPROTECTED:
                continue
            r = client.request(method.upper(), path.format_map(_AnyInt()), headers=H)
            assert r.status_code == 401, f"{method.upper()} {path} answered {r.status_code} without login"
            checked += 1
    assert checked >= 20


class _AnyInt(dict):
    def __missing__(self, key):
        return "1"
