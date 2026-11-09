"""Shared helpers for career-crawler tests: offline fixture websites, never the network."""

from __future__ import annotations

import json
from pathlib import Path

from career_crawler.config import CrawlerSettings
from career_crawler.fetch import Fetcher, MemoryThrottle
from career_crawler.testing import FixtureTransport, fixture_resolver
from gcc_core.models import Company
from gcc_core.netsafe import NetPolicy
from gcc_core.urls import domain_key

SITES = Path(__file__).parent.parent / "benchmark" / "sites"


def expected() -> dict:
    return json.loads((SITES / "_expected.json").read_text("utf-8"))


def crawler_settings(**kw) -> CrawlerSettings:
    base = {"per_domain_delay_s": 0.0, "browser_enabled": False, "early_stop_after": 8}
    base.update(kw)
    return CrawlerSettings(**base)


def fixture_fetcher(
    settings: CrawlerSettings,
    *,
    root: Path = SITES,
    overrides: dict | None = None,
    transport: FixtureTransport | None = None,
) -> tuple[Fetcher, FixtureTransport]:
    t = transport or FixtureTransport(root)
    f = Fetcher(
        settings,
        policy=NetPolicy(),
        user_agent="GCC-Test/1.0",
        transport=t,
        resolver=fixture_resolver(t, overrides),
        throttle=MemoryThrottle(),
    )
    return f, t


def make_company(db, host: str, *, name: str | None = None, verified: bool = True) -> Company:
    c = Company(
        name=name or host.split(".")[0],
        name_key=(name or host).lower(),
        website_url=f"https://{host}/",
        domain=domain_key(host),
        verification_status="verified" if verified else "unverified",
    )
    db.add(c)
    db.flush()
    return c
