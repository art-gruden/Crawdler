"""Website verification against local HTTP servers.

Host names are mapped to 127.0.0.1 by a fake resolver; the test policy allows exactly
127.0.0.1 and the server's port. The default policy must block the same requests.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from company_discovery.verify import apply_result, backoff, verify_url
from gcc_core.models import Company, ScanRequest
from gcc_core.netsafe import DnsError, NetPolicy

UA = "GermanyCareerCrawler/0.1 (+https://example.org)"

SITES: dict[str, dict[str, tuple[int, dict, str]]] = {
    "firma-a.de": {
        "/robots.txt": (200, {}, "User-agent: *\nDisallow: /intern/\n"),
        "/": (
            200,
            {"Content-Type": "text/html; charset=utf-8"},
            "<html><head><title>Firma A GmbH – Software aus Dresden</title></head><body>Willkommen bei Firma A</body></html>",
        ),
    },
    "blocked-robots.de": {
        "/robots.txt": (200, {}, "User-agent: GermanyCareerCrawler\nDisallow: /\n"),
        "/": (200, {}, "<title>should never be fetched</title>"),
    },
    "forbidden.de": {"/robots.txt": (404, {}, ""), "/": (403, {"Server": "cloudflare"}, "Forbidden")},
    "challenge.de": {
        "/robots.txt": (404, {}, ""),
        "/": (
            200,
            {"Content-Type": "text/html"},
            "<title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x.js'></script>",
        ),
    },
    "parked.de": {
        "/robots.txt": (404, {}, ""),
        "/": (
            200,
            {"Content-Type": "text/html"},
            "<title>parked.de</title><p>This domain is for sale! Contact Sedo.</p>",
        ),
    },
    "moved.de": {"/robots.txt": (404, {}, ""), "/": (301, {"Location": "http://neue-firma.de:{port}/"}, "")},
    "neue-firma.de": {
        "/robots.txt": (404, {}, ""),
        "/": (200, {"Content-Type": "text/html"}, "<title>Moved GmbH</title>Moved GmbH ist jetzt Neue Firma"),
    },
    "to-metadata.de": {"/robots.txt": (404, {}, ""), "/": (302, {"Location": "http://metadata.de:{port}/latest"}, "")},
    "to-social.de": {"/robots.txt": (404, {}, ""), "/": (302, {"Location": "https://www.facebook.com/firma"}, "")},
    "robots-500.de": {"/robots.txt": (503, {}, ""), "/": (200, {}, "x")},
    "busy.de": {"/robots.txt": (404, {}, ""), "/": (429, {"Retry-After": "120"}, "slow down")},
}
REQUESTS: list[tuple[str, str]] = []


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        host = self.headers["Host"].split(":")[0]
        REQUESTS.append((host, self.path))
        status, headers, body = SITES.get(host, {}).get(self.path, (404, {}, "not found"))
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v.replace("{port}", str(self.server.server_port)))
        payload = body.encode()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


async def resolver(host: str, port: int) -> list[str]:
    if host == "metadata.de":
        return ["169.254.169.254"]
    if host == "internal.de":
        return ["10.0.0.5"]
    if host in SITES:
        return ["127.0.0.1"]
    raise DnsError(f"cannot resolve {host}")


@pytest.fixture
def policy(server):
    return NetPolicy(
        allowed_ports=frozenset({80, 443, server.server_port}), allowed_private=(ipaddress.ip_network("127.0.0.1/32"),)
    )


async def check(host, server, policy, name="Firma", expected=None):
    url = f"http://{host}:{server.server_port}/"
    return await verify_url(
        url, name, expected or host, policy=policy, user_agent=UA, timeout=5, max_bytes=65536, resolver=resolver
    )


async def test_verified_with_name_match(server, policy):
    r = await check("firma-a.de", server, policy, name="Firma A GmbH")
    assert r.status == "verified"
    assert r.detail["name_match"] is True
    assert r.detail["title"].startswith("Firma A GmbH")
    assert r.final_domain == "firma-a.de" and r.confidence == 1.0


async def test_robots_disallow_stops_before_homepage(server, policy):
    REQUESTS.clear()
    r = await check("blocked-robots.de", server, policy)
    assert r.status == "robots_disallowed"
    assert ("blocked-robots.de", "/") not in REQUESTS
    assert ("blocked-robots.de", "/robots.txt") in REQUESTS


@pytest.mark.parametrize(
    "host, status",
    [
        ("forbidden.de", "access_restricted"),
        ("challenge.de", "access_restricted"),
        ("parked.de", "parked"),
        ("robots-500.de", "robots_unavailable"),
        ("busy.de", "rate_limited"),
        ("to-metadata.de", "blocked_address"),
        ("to-social.de", "redirected"),
        ("does-not-exist.de", "dns_error"),
    ],
)
async def test_statuses(server, policy, host, status):
    r = await check(host, server, policy)
    assert r.status == status, r.detail


async def test_requests_are_made_once_per_site(server, policy):
    REQUESTS.clear()
    await check("forbidden.de", server, policy)
    assert REQUESTS.count(("forbidden.de", "/")) == 1  # 403 is not retried or worked around


async def test_redirect_to_other_domain(server, policy):
    r = await check("moved.de", server, policy, name="Moved GmbH")
    assert r.status == "verified"
    assert r.final_domain == "neue-firma.de"
    assert r.detail["redirected_from"] == "moved.de"


async def test_default_policy_blocks_loopback(server):
    r = await check("firma-a.de", server, NetPolicy(allowed_ports=frozenset({80, 443, server.server_port})))
    assert r.status == "blocked_address"


@pytest.mark.parametrize(
    "ip, allowed",
    [
        ("8.8.8.8", True),
        ("2a00:1450:4001:80b::2003", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.178.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("fd00::1", False),
        ("::ffff:10.0.0.1", False),
        ("fe80::1", False),
    ],
)
def test_address_policy(ip, allowed):
    assert NetPolicy().address_allowed(ip) is allowed


# ------------------------------------------------------------------ persistence


def _company(db, domain, **kw):
    c = Company(name=kw.pop("name", "Firma"), name_key="firma", domain=domain, website_url=f"https://{domain}/", **kw)
    db.add(c)
    db.commit()
    return c


def test_apply_verified_queues_scan(db, settings):
    from company_discovery.verify import Verification

    c = _company(db, "firma-a.de")
    queued = apply_result(
        db, c, Verification("verified", "https://firma-a.de/", "firma-a.de", 200, 1.0, {}), settings=settings
    )
    db.commit()
    assert queued == "queued"
    assert c.scan_status == "queued" and c.verification_status == "verified"
    assert c.next_verify_at > dt.datetime.now(dt.UTC) + dt.timedelta(days=29)
    assert db.query(ScanRequest).filter_by(company_id=c.id, status="pending", source="discovery").count() == 1


def test_apply_redirect_adopts_free_domain_and_detects_duplicates(db, settings):
    from company_discovery.verify import Verification

    a = _company(db, "alt-firma.de")
    other = _company(db, "konzern.de", name="Konzern")
    res = Verification(
        "verified", "https://neu-firma.de/", "neu-firma.de", 200, 0.8, {"redirected_from": "alt-firma.de"}
    )
    apply_result(db, a, res, settings=settings.model_copy(update={"auto_queue_verified": False}))
    assert a.domain == "neu-firma.de" and a.verification_detail["previous_domain"] == "alt-firma.de"
    b = _company(db, "tochter.de")
    res2 = Verification("verified", "https://konzern.de/", "konzern.de", 200, 0.5, {})
    apply_result(db, b, res2, settings=settings)
    db.commit()
    assert b.verification_status == "redirected"
    assert b.verification_detail["duplicate_of_company_id"] == other.id
    assert b.scan_status == "not_queued"


def test_retryable_status_backs_off(db, settings):
    from company_discovery.verify import Verification

    c = _company(db, "busy.de")
    for expected_hours in (1, 4, 16):
        apply_result(db, c, Verification("rate_limited"), settings=settings)
        delta = c.next_verify_at - dt.datetime.now(dt.UTC)
        assert abs(delta - dt.timedelta(hours=expected_hours)) < dt.timedelta(minutes=1)
    assert backoff(10) == dt.timedelta(hours=168)


def test_manual_request_is_queued_after_verification(db, settings):
    from company_discovery.verify import Verification

    c = _company(db, "manuell.de", queue_after_verify=True)
    no_auto = settings.model_copy(update={"auto_queue_verified": False})
    assert (
        apply_result(db, c, Verification("verified", "https://manuell.de/", "manuell.de", 200, 1, {}), settings=no_auto)
        == "queued"
    )
    db.commit()
    req = db.query(ScanRequest).filter_by(company_id=c.id).one()
    assert req.priority == 10 and req.source == "manual" and c.queue_after_verify is False


async def test_https_failure_falls_back_to_http(server, policy):
    """https:// against a plain-HTTP server fails the TLS handshake; robots.txt and the
    homepage are then fetched over http instead of reporting 'robots_unavailable'."""
    url = f"https://firma-a.de:{server.server_port}/"
    r = await verify_url(
        url, "Firma A GmbH", "firma-a.de", policy=policy, user_agent=UA, timeout=3, max_bytes=65536, resolver=resolver
    )
    assert r.status == "verified", r.detail
    assert r.final_url.startswith("http://firma-a.de:")
