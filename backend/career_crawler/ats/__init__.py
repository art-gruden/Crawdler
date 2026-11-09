"""Career-platform plugins and evidence-based ATS detection."""

from __future__ import annotations

import re

from .adapters import AtsFeedError, GreenhouseAdapter, LeverAdapter, PersonioAdapter
from .base import AtsMatch, FeedAdapter, FeedResult

FEED_ADAPTERS: list[FeedAdapter] = [PersonioAdapter(), GreenhouseAdapter(), LeverAdapter()]
# Page adapters (schema.org JobPosting, generic HTML) live in career_crawler.extract and are
# applied by the engine to every fetched page; listed here so the plugin set is in one place.
PAGE_ADAPTERS = ("jsonld", "html")

_URL_IN_TEXT = re.compile(
    r"https?://[A-Za-z0-9.-]+\.(?:personio\.(?:de|com)|greenhouse\.io|lever\.co)/[^\s\"'<>)\\]*", re.I
)
ATS_HOSTS = re.compile(r"(\.jobs\.personio\.(de|com)|greenhouse\.io|\.lever\.co)$", re.I)


def adapter(name: str) -> FeedAdapter | None:
    return next((a for a in FEED_ADAPTERS if a.name == name), None)


def detect_url(url: str) -> AtsMatch | None:
    for a in FEED_ADAPTERS:
        m = a.detect(url)
        if m:
            return m
    return None


def detect(urls: list[str], inline_scripts: str = "") -> list[AtsMatch]:
    """ATS accounts evidenced by link/iframe/script URLs or URLs written inside inline scripts."""
    found: dict[tuple[str, str], AtsMatch] = {}
    candidates = list(urls) + _URL_IN_TEXT.findall(inline_scripts[:500_000])
    for u in candidates:
        m = detect_url(u)
        if m:
            found.setdefault((m.ats_type, m.account.lower()), m)
    return list(found.values())


__all__ = [
    "ATS_HOSTS",
    "FEED_ADAPTERS",
    "PAGE_ADAPTERS",
    "AtsFeedError",
    "AtsMatch",
    "FeedResult",
    "adapter",
    "detect",
    "detect_url",
]
