"""Website verification.

For each company website:
1. robots.txt of the host is fetched and evaluated for our robots.txt product token. If the homepage
   is disallowed, nothing else is requested and the domain is never crawled.
2. The homepage is fetched with the SSRF-safe client (max. 5 redirects, every hop
   re-checked, robots.txt checked for every new host).
3. The response is classified. 401/403, CAPTCHA and bot challenges are recorded as
   ``access_restricted`` and not retried or worked around.

Statuses that may change by themselves (timeouts, 429, 5xx) get exponential back-off.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import re
from dataclasses import dataclass, field

from protego import Protego
from sqlalchemy import select
from sqlalchemy.orm import Session

from gcc_core import scan_queue
from gcc_core.botinfo import robots_token
from gcc_core.log import get_logger
from gcc_core.models import RETRYABLE_VERIFICATION, Company
from gcc_core.netsafe import (
    BlockedAddress,
    DnsError,
    FetchError,
    NetPolicy,
    Resolver,
    TlsError,
    safe_get,
    system_resolver,
)
from gcc_core.urls import UrlRejected, normalize_website

from .service import name_key

log = get_logger(__name__)

PARKED = re.compile(
    r"(domain (is|may be) for sale|this domain is for sale|diese domain (kaufen|steht zum verkauf|ist zu verkaufen)|"
    r"sedoparking|parkingcrew|bodis\.com|domain parked|parked free|dan\.com/buy|afternic|"
    r"diese domain wurde (gerade )?registriert|hier entsteht (eine neue|in kürze)|domain reserviert)",
    re.I,
)
CHALLENGE = re.compile(
    r"(cf-chl-|challenges\.cloudflare\.com|cf_chl_opt|/cdn-cgi/challenge-platform|ddos-guard|"
    r"g-recaptcha|h-captcha|hcaptcha\.com|captcha-delivery\.com|px-captcha|"
    r"attention required! \| cloudflare|just a moment\.\.\.|checking your browser)",
    re.I,
)
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
TAGS = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.I | re.S)


@dataclass
class Verification:
    status: str
    final_url: str | None = None
    final_domain: str | None = None
    http_status: int | None = None
    confidence: float | None = None
    detail: dict = field(default_factory=dict)


class RobotsCache:
    def __init__(self, fetch):
        self._fetch = fetch
        self._cache: dict[str, Protego | str] = {}

    async def check(self, url: str, user_agent: str) -> None:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._cache:
            self._cache[origin] = await self._load(origin)
        rules = self._cache[origin]
        if rules == "unavailable":
            raise RobotsUnavailable(origin)
        if isinstance(rules, Protego) and not rules.can_fetch(url, robots_token(user_agent)):
            raise RobotsDisallowed(url)

    async def _load(self, origin: str):
        # Connection, TLS, DNS and SSRF errors propagate: the site itself is the problem,
        # and the caller may still try plain http. Only a server answering 5xx/429 for
        # robots.txt means "robots.txt unavailable" (RFC 9309: assume full disallow).
        res = await self._fetch(origin + "/robots.txt")
        if res.status >= 500 or res.status == 429:
            return "unavailable"
        if res.status >= 400:  # 4xx: no restrictions (RFC 9309)
            return None
        return Protego.parse(res.text())


class RobotsDisallowed(Exception):
    pass


class RobotsUnavailable(Exception):
    pass


class RedirectRejected(Exception):
    pass


def _visible_text(markup: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", TAGS.sub(" ", markup))).strip()


def name_found(company_name: str, title: str, text: str, host: str) -> bool:
    tokens = [t for t in name_key(company_name).split() if len(t) >= 3]
    if not tokens:
        return False
    from .regions import fold

    haystack = fold(f"{title} {text[:200_000]}")
    host_flat = re.sub(r"[^a-z0-9]", "", host.lower())
    hits = sum(1 for t in tokens if f" {t} " in f" {haystack} " or t.replace(" ", "") in host_flat)
    return hits >= max(1, (len(tokens) + 1) // 2)


async def verify_url(
    url: str,
    company_name: str,
    expected_domain: str | None,
    *,
    policy: NetPolicy,
    user_agent: str,
    timeout: float,
    max_bytes: int,
    resolver: Resolver = system_resolver,
) -> Verification:
    async def fetch_robots(robots_url: str):
        return await safe_get(
            robots_url,
            policy=policy,
            user_agent=user_agent,
            timeout=timeout,
            max_bytes=256 * 1024,
            accept="text/plain,*/*;q=0.5",
            resolver=resolver,
            max_redirects=3,
        )

    robots = RobotsCache(fetch_robots)

    async def before(hop_url: str) -> None:
        if hop_url != url:  # a redirect: do not follow it to social networks or directories
            try:
                normalize_website(hop_url, allowed_ports=policy.allowed_ports)
            except UrlRejected as exc:
                raise RedirectRejected(f"{exc.reason}: {hop_url}") from exc
        await robots.check(hop_url, user_agent)

    candidates = [url]
    if url.startswith("https://"):
        candidates.append("http://" + url[len("https://") :])

    last_error: Verification | None = None
    for attempt_url in candidates:
        try:
            res = await safe_get(
                attempt_url,
                policy=policy,
                user_agent=user_agent,
                timeout=timeout,
                max_bytes=max_bytes,
                resolver=resolver,
                before_request=before,
            )
        except RobotsDisallowed as exc:
            return Verification("robots_disallowed", detail={"url": str(exc)})
        except RedirectRejected as exc:
            return Verification("redirected", detail={"reason": str(exc)[:300]})
        except RobotsUnavailable as exc:
            return Verification("robots_unavailable", detail={"origin": str(exc)})
        except BlockedAddress as exc:
            return Verification("blocked_address", detail={"error": str(exc)})
        except DnsError as exc:
            return Verification("dns_error", detail={"error": str(exc)})
        except TlsError as exc:
            last_error = Verification("unreachable", detail={"error": f"TLS: {exc}"[:500]})
            continue  # try plain http once
        except FetchError as exc:
            last_error = Verification("unreachable", detail={"error": f"{exc.kind}: {exc}"[:500]})
            if attempt_url.startswith("https://"):
                continue
            return last_error
        return classify(res, company_name, expected_domain)
    return last_error or Verification("unreachable")


def classify(res, company_name: str, expected_domain: str | None) -> Verification:
    detail: dict = {"redirects": res.redirects[:5], "ip": res.ip}
    try:
        final = normalize_website(res.final_url, allowed_ports={80, 443, *_port(res.final_url)})
        final_domain = final.domain
    except UrlRejected as exc:
        return Verification("redirected", res.final_url, None, res.status, None, {**detail, "reason": exc.reason})
    server = res.headers.get("server", "")
    body = res.text() if res.content_type in ("text/html", "application/xhtml+xml", "text/plain", "") else ""
    title = html.unescape(TITLE.search(body).group(1)).strip()[:300] if TITLE.search(body) else ""
    detail["title"] = title
    base = Verification("verified", res.final_url, final_domain, res.status, None, detail)

    if res.status in (401, 403) or res.headers.get("cf-mitigated") == "challenge":
        base.status = "access_restricted"
        detail["reason"] = f"HTTP {res.status}" + (f" ({server})" if server else "")
        return base
    if res.status == 429:
        base.status = "rate_limited"
        return base
    if res.status >= 400:
        base.status = "http_error"
        return base
    if CHALLENGE.search(body[:100_000]) and len(_visible_text(body)) < 3000:
        base.status = "access_restricted"
        detail["reason"] = "bot challenge / CAPTCHA page"
        return base
    text = _visible_text(body)
    if PARKED.search(text[:20_000]) or PARKED.search(title):
        base.status = "parked"
        return base
    matched = name_found(company_name, title, text, final_domain or "")
    detail["name_match"] = matched
    detail["content_type"] = res.content_type
    confidence = 0.5 + (0.3 if matched else 0.0)
    if final_domain and expected_domain and final_domain == expected_domain:
        confidence += 0.2
    elif final_domain and expected_domain:
        detail["redirected_from"] = expected_domain
    base.confidence = round(confidence, 2)
    return base


def _port(url: str) -> set[int]:
    from urllib.parse import urlsplit

    try:
        p = urlsplit(url).port
    except ValueError:
        return set()
    return {p} if p else set()


# --------------------------------------------------------------------------- persistence


def backoff(attempts: int) -> dt.timedelta:
    hours = min(4 ** max(attempts - 1, 0), 168)  # 1h, 4h, 16h, 64h, then 7 days
    return dt.timedelta(hours=hours)


def apply_result(session: Session, company: Company, result: Verification, *, settings) -> str | None:
    """Store the result. Returns 'queued' / 'already_queued' if the company was queued for scanning."""
    now = dt.datetime.now(dt.UTC)
    company.verification_attempts = (company.verification_attempts or 0) + 1
    detail = dict(result.detail)
    detail["checked_url"] = company.website_url
    status = result.status

    if status == "verified" and result.final_domain and result.final_domain != company.domain:
        owner = session.execute(
            select(Company.id).where(Company.domain == result.final_domain, Company.id != company.id)
        ).scalar_one_or_none()
        if owner is not None:
            status = "redirected"
            detail["duplicate_of_company_id"] = owner
        else:
            detail["previous_domain"] = company.domain
            company.domain = result.final_domain
            company.website_url = result.final_url
    company.verification_status = status
    company.verification_detail = detail
    company.verification_confidence = result.confidence
    company.verified_at = now
    if status in RETRYABLE_VERIFICATION:
        company.next_verify_at = now + backoff(company.verification_attempts)
    elif status == "verified":
        company.verification_attempts = 0
        company.next_verify_at = now + dt.timedelta(days=settings.verify_recheck_days)
    else:  # permanent until somebody changes the website or asks again
        company.next_verify_at = now + dt.timedelta(days=settings.verify_recheck_days * 3)

    manual = bool(company.queue_after_verify)
    company.queue_after_verify = False
    if status == "verified" and manual:
        return scan_queue.request_scan(session, company, source="manual", priority=scan_queue.PRIORITY_MANUAL)
    if status == "verified" and settings.auto_queue_verified and company.scan_status == "not_queued":
        return scan_queue.request_scan(session, company, source="discovery")
    return None


async def verify_many(
    companies: list[tuple[int, str, str, str | None]],
    *,
    policy: NetPolicy,
    settings,
    resolver: Resolver = system_resolver,
) -> dict[int, Verification]:
    """companies: (id, url, name, domain). Bounded concurrency."""
    sem = asyncio.Semaphore(settings.verify_concurrency)
    results: dict[int, Verification] = {}

    async def one(cid: int, url: str, name: str, domain: str | None):
        async with sem:
            try:
                results[cid] = await verify_url(
                    url,
                    name,
                    domain,
                    policy=policy,
                    user_agent=settings.crawler_user_agent,
                    timeout=settings.verify_timeout_s,
                    max_bytes=settings.verify_max_bytes,
                    resolver=resolver,
                )
            except Exception as exc:  # never let one site kill the batch
                log.warning("verify.unexpected_error", company_id=cid, error=repr(exc))
                results[cid] = Verification("unreachable", detail={"error": repr(exc)[:300]})

    await asyncio.gather(*(one(*c) for c in companies))
    return results


__all__ = ["verify_url", "verify_many", "apply_result", "classify", "Verification"]
