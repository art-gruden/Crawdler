"""SiteScan against the offline fixture websites (no database, no network)."""

from __future__ import annotations

import json

import pytest

from career_crawler.engine import CompanyInfo, FingerprintView, SiteScan
from career_crawler.fetch import AdjustableLimiter, MemoryThrottle, parse_retry_after
from career_crawler.testing import FixtureTransport
from gcc_core.urls import domain_key

from .crawler_helpers import SITES, crawler_settings, expected, fixture_fetcher

EXPECTED = expected()


def info(host: str, i: int = 1) -> CompanyInfo:
    return CompanyInfo(i, host, domain_key(host), f"https://{host}/")


async def scan(host: str, **kw):
    s = kw.pop("settings", None) or crawler_settings()
    f, t = fixture_fetcher(s, **{k: kw.pop(k) for k in ("overrides", "root", "transport") if k in kw})
    try:
        r = await SiteScan(f, s, info(host), **kw).run()
    finally:
        await f.aclose()
    return r, t


@pytest.mark.parametrize("host", sorted(EXPECTED))
async def test_fixture_site_outcomes(host):
    exp = EXPECTED[host]
    r, _ = await scan(host)
    assert r.outcome == exp["outcome"], [(e.kind, e.url) for e in r.errors]
    if exp.get("needs_browser"):
        assert r.browser_urls and not r.jobs
        return
    got = {j.job.title for j in r.jobs}
    assert got == {j["title"] for j in exp["jobs"]}
    by_title = {j.job.title: j.job for j in r.jobs}
    for e in exp["jobs"]:
        job = by_title[e["title"]]
        for field in ("city", "postcode", "remote_type", "salary_min", "salary_max", "ats_job_id"):
            if field in e:
                assert getattr(job, field) == e[field], (e["title"], field)
        if "date_posted" in e:
            assert (job.date_posted.isoformat() if job.date_posted else None) == e["date_posted"], e["title"]
        if "employment_types" in e:
            assert set(e["employment_types"]) <= set(job.employment_types), (e["title"], job.employment_types)
        if "skills" in e:
            assert set(e["skills"]) <= {s for s, _ in job.skills}
    if exp.get("ats"):
        assert r.best_strategy == "ats"


async def test_robots_disallow_is_respected():
    r, t = await scan("gcc-fixture-static.de")
    assert not any("/intern/" in req for req in t.requests)
    assert t.requests[0] == "gcc-fixture-static.de/robots.txt"


async def test_rate_limit_stops_and_honours_retry_after():
    r, t = await scan("gcc-fixture-ratelimit.de")
    assert r.stop is not None and r.stop.retry_after_s == 120
    assert [x for x in t.requests if "ratelimit" in x] == [
        "gcc-fixture-ratelimit.de/robots.txt",
        "gcc-fixture-ratelimit.de/",
    ]
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", 5) == 0
    assert parse_retry_after(None, 7) == 7


async def test_challenge_is_not_bypassed():
    r, t = await scan("gcc-fixture-challenge.de")
    assert r.outcome == "access_restricted" and len([x for x in t.requests if "challenge" in x]) == 2


async def test_remembered_ats_skips_discovery():
    fp = FingerprintView(
        best_strategy="ats",
        ats_type="personio",
        ats_account="fixturecorp",
        career_urls=["https://fixturecorp.jobs.personio.de/"],
        full_discovery_due=False,
    )
    r, t = await scan("gcc-fixture-personio.de", fingerprint=fp)
    assert len(r.jobs) == 3 and r.complete
    assert not any(req.startswith("gcc-fixture-personio.de") for req in t.requests)  # straight to the feed


async def test_cached_strategy_falls_back_when_ats_is_gone(tmp_path):
    import shutil

    root = tmp_path / "sites"
    shutil.copytree(SITES, root)
    (root / "fixturecorp.jobs.personio.de" / "_routes.json").write_text(json.dumps({"/xml": {"status": 404}}))
    fp = FingerprintView(best_strategy="ats", ats_type="personio", ats_account="fixturecorp", full_discovery_due=False)
    r, _ = await scan("gcc-fixture-personio.de", fingerprint=fp, root=root)
    assert any(e.stage == "ats" for e in r.errors)
    assert r.outcome in ("career_page_no_jobs", "no_career_page")  # crawled the site instead of giving up


async def test_ssrf_private_address_is_blocked():
    # the company's own host resolves to a private address (e.g. DNS rebinding to 10.0.0.5)
    r, t = await scan("gcc-fixture-static.de", overrides={"gcc-fixture-static.de": "10.0.0.5"})
    assert r.stop is not None and not t.requests
    assert r.errors[0].kind in ("blocked_address", "stop")
    r, t = await scan("gcc-fixture-static.de", overrides={"gcc-fixture-static.de": "169.254.169.254"})
    assert not t.requests


async def test_external_hosts_are_bounded(tmp_path):
    root = tmp_path / "sites"
    host = "gcc-fixture-hops.de"
    (root / host).mkdir(parents=True)
    links = "".join(f'<a href="https://karriere.ext{i}-gcc.de/">Karriere {i}</a>' for i in range(6))
    (root / host / "index.html").write_text(f"<html><body><h1>Start</h1>{links}</body></html>")
    for i in range(6):
        (root / f"karriere.ext{i}-gcc.de").mkdir()
        (root / f"karriere.ext{i}-gcc.de" / "index.html").write_text("<h1>Karriere</h1>")
    r, t = await scan(host, root=root)
    hosts = {req.split("/")[0] for req in t.requests if "ext" in req}
    assert len(hosts) == 2  # max_external_ats_hops


async def test_page_budget_and_early_stop():
    s = crawler_settings(max_pages_per_company=3)
    r, _ = await scan("gcc-fixture-paginated.de", settings=s)
    assert r.pages_fetched == 3 and not r.complete  # budget hit -> not a complete read
    assert r.jobs  # list entries are kept even without detail pages
    r, _ = await scan("gcc-fixture-nocareer.de")
    assert r.pages_fetched <= 12


async def test_unchanged_detail_page_is_not_reextracted():
    from career_crawler.engine import KnownPage, text_hash
    from career_crawler.extract import html as h

    body = (SITES / "gcc-fixture-static.de" / "karriere" / "devops-engineer.html").read_text()
    t = h.parse(body)
    from career_crawler.extract.normalize import clean_text

    known = {
        "https://gcc-fixture-static.de/karriere/devops-engineer": KnownPage(
            "job_detail", text_hash(clean_text(t.body.text(separator="\n"))), None, None
        )
    }
    r, _ = await scan("gcc-fixture-static.de", known_pages=known)
    assert "https://gcc-fixture-static.de/karriere/devops-engineer" in r.unchanged_job_urls
    assert len(r.jobs) == 3  # the list entry still counts as seen; the detail was not re-parsed


async def test_conditional_get_304(tmp_path):
    import shutil

    from career_crawler.engine import KnownPage

    root = tmp_path / "sites"
    shutil.copytree(SITES, root)
    host = root / "gcc-fixture-static.de"
    (host / "_routes.json").write_text(
        json.dumps(
            {
                "/karriere/linux-administrator": {
                    "file": "karriere/linux-administrator.html",
                    "headers": {"etag": '"v1"'},
                }
            }
        )
    )
    known = {"https://gcc-fixture-static.de/karriere/linux-administrator": KnownPage("job_detail", "x", '"v1"', None)}
    r, _ = await scan("gcc-fixture-static.de", known_pages=known, root=root)
    assert "https://gcc-fixture-static.de/karriere/linux-administrator" in r.unchanged_job_urls
    assert r.pages_unchanged >= 1


async def test_throttle_spacing_and_limiter():
    clock = {"t": 0.0}
    slept = []

    async def fake_sleep(d):
        slept.append(d)
        clock["t"] += d

    th = MemoryThrottle(clock=lambda: clock["t"], sleep=fake_sleep)
    for _ in range(3):
        await th.wait("x.de", 2.0)
    assert slept == [2.0, 2.0]
    await th.cool_down("x.de", 100)
    await th.wait("x.de", 2.0)
    assert slept[-1] >= 100
    lim = AdjustableLimiter(2)
    async with lim:
        async with lim:
            assert lim.active == 2
    await lim.set_limit(5)
    assert lim.limit == 5


def test_fixture_transport_rejects_path_traversal():
    assert FixtureTransport._resolve(SITES / "gcc-fixture-static.de", "/../gcc-fixture-jsonld.de/jobs", "") is None
