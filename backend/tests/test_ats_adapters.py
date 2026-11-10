"""ATS adapters against offline fixtures (documented API structures; no network, no database).

The fixtures model the public structures of each provider. Passing them proves the *parsing*; it does
not make a provider live-ready - that is tracked separately in ``career_crawler.ats.capabilities``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from career_crawler import ats
from career_crawler.ats import capabilities as caps
from career_crawler.ats.adapters import AtsFeedError, GreenhouseAdapter, LeverAdapter
from career_crawler.ats.ashby import AshbyAdapter
from career_crawler.ats.smartrecruiters import SmartRecruitersAdapter
from career_crawler.config import CrawlerSettings
from career_crawler.engine import CompanyInfo, SiteScan
from career_crawler.extract.normalize import normalize
from career_crawler.fetch import RobotsDisallowed
from gcc_core.urls import domain_key

from .crawler_helpers import crawler_settings, fixture_fetcher

ATS_SITES = Path(__file__).parent / "fixtures" / "ats"


async def fetch_feed(url: str, *, settings=None, root=ATS_SITES):
    match = ats.detect_url(url)
    assert match is not None, url
    s = settings or crawler_settings()
    f, t = fixture_fetcher(s, root=root)
    try:
        res = await ats.adapter(match.ats_type).fetch(f, match)
    finally:
        await f.aclose()
    return match, res, t


def norm(raw):
    return normalize(raw, max_chars=40_000)


async def scan(host: str, *, settings=None, root=ATS_SITES, transport=None):
    s = settings or crawler_settings()
    f, t = fixture_fetcher(s, root=root, transport=transport)
    try:
        r = await SiteScan(f, s, CompanyInfo(1, host, domain_key(host), f"https://{host}/")).run()
    finally:
        await f.aclose()
    return r, t


# --- Greenhouse ---------------------------------------------------------------------------------


async def test_greenhouse_board_structure():
    m, res, _ = await fetch_feed("https://boards.greenhouse.io/n26like")
    assert res.complete and res.reported_total == 5 and len(res.jobs) == 5
    by_id = {j.ats_job_id: j for j in res.jobs}
    be = by_id["7001"]
    assert be.title == "Senior Backend Engineer (Payments)"
    assert be.location_raw == "Berlin" and be.locations == [{"city": "Berlin, Germany"}]
    assert be.date_posted_raw == "2026-10-12T08:00:00-04:00"  # first_published, never updated_at
    assert be.employment_raw == ["Full-time"] and be.remote_raw == "Hybrid"
    assert "<p>Build payment services" in be.description_html  # entity-escaped content is unescaped
    assert be.salary and be.salary["min"] == 85000 and be.salary["max"] == 105000 and be.salary["currency"] == "EUR"
    assert be.source_values["language"] == "en" and be.source_values["offices"] == ["Berlin"]
    assert by_id["7004"].location_raw == "Remote - Germany" and by_id["7004"].source_values["language"] == "de"
    assert by_id["7002"].salary is None  # pay ranges only exist when the employer published them
    job = norm(be)
    assert job.ats_type == "greenhouse" and job.ats_job_id == "7001" and job.salary_min == 85000
    assert job.city == "Berlin" and job.country == "DE"


async def test_greenhouse_duplicate_titles_stay_distinct_by_job_id():
    _, res, _ = await fetch_feed("https://boards.greenhouse.io/bitpandalike")
    assert res.complete and res.reported_total == 4
    titles = [j.title for j in res.jobs]
    assert titles.count("AML Operations Associate") == 2 and titles.count("Frontend Engineer") == 2
    assert len({j.ats_job_id for j in res.jobs}) == 4
    assert len({norm(j).source_url for j in res.jobs}) == 4


async def test_greenhouse_total_mismatch_is_not_complete():
    _, res, _ = await fetch_feed("https://boards.greenhouse.io/short")
    assert len(res.jobs) == 3 and res.reported_total == 5
    assert res.complete is False  # board claims 5 postings: never let a short list close anything


async def test_greenhouse_repeated_id_in_payload_is_one_job():
    _, res, _ = await fetch_feed("https://boards.greenhouse.io/dupid")
    assert [j.ats_job_id for j in res.jobs] == ["7001", "7002"]


async def test_greenhouse_oversized_content_falls_back_to_the_list_without_bodies():
    s = crawler_settings(max_feed_bytes=6_000)
    _, res, t = await fetch_feed("https://boards.greenhouse.io/bigboard", settings=s)
    assert len(res.jobs) == 5 and res.complete
    assert all(j.source_values["no_content"] for j in res.jobs) and all(not j.description_html for j in res.jobs)
    assert [r for r in t.requests if "bigboard" in r][-1].endswith("/jobs")


async def test_greenhouse_garbage_is_a_feed_error():
    root_dir = ATS_SITES
    f, _ = fixture_fetcher(crawler_settings(), root=root_dir)
    m = ats.detect_url("https://boards.greenhouse.io/nosuchboard")
    try:
        with pytest.raises(AtsFeedError):
            await GreenhouseAdapter().fetch(f, m)
    finally:
        await f.aclose()


# --- Lever --------------------------------------------------------------------------------------


async def test_lever_postings_structure():
    _, res, _ = await fetch_feed("https://jobs.lever.co/acme")
    assert res.complete and len(res.jobs) == 3
    j = {x.title: x for x in res.jobs}["Backend Engineer"]
    assert j.remote_raw == "hybrid" and j.source_values["country"] == "DE"
    assert j.locations == [{"city": "Munich", "country": "DE"}]
    assert j.salary and j.salary["min"] == 70000 and j.salary["period"] == "year"
    assert "Requirements" in j.description_html and "diversity" in j.description_html
    multi = {x.title: x for x in res.jobs}["Account Executive DACH"]
    assert multi.locations == [{"city": "Zurich"}, {"city": "Vienna"}]  # country applies to one location only
    assert norm(j).work_model == "HYBRID"


async def test_lever_empty_board_is_complete_and_empty():
    # the diagnostics report saw exactly this for the official example account (HTTP 200, "[]")
    _, res, _ = await fetch_feed("https://jobs.lever.co/empty")
    assert res.jobs == [] and res.complete


async def test_lever_overlapping_pages_are_deduplicated(monkeypatch):
    monkeypatch.setattr(LeverAdapter, "PAGE", 2)
    _, res, _ = await fetch_feed("https://jobs.lever.co/pagedco")
    assert len(res.jobs) == 3 and res.pages == 3 and res.complete


async def test_lever_page_cap_is_never_complete(monkeypatch):
    monkeypatch.setattr(LeverAdapter, "PAGE", 2)
    monkeypatch.setattr(LeverAdapter, "MAX_PAGES", 2)
    _, res, _ = await fetch_feed("https://jobs.lever.co/pagedco")
    assert res.complete is False


# --- SmartRecruiters ----------------------------------------------------------------------------


async def test_smartrecruiters_catalogue_structure():
    m, res, _ = await fetch_feed("https://jobs.smartrecruiters.com/AcmeSR")
    assert m.feed_url.endswith("/v1/companies/AcmeSR/postings?destination=PUBLIC")
    assert res.complete and res.reported_total == 4 and len(res.jobs) == 4
    by = {j.title: j for j in res.jobs}
    sre = by["Site Reliability Engineer"]
    assert sre.source_url == "https://jobs.smartrecruiters.com/AcmeSR/744000001"
    assert sre.locations == [{"city": "Munich", "region": "BY", "country": "DE"}]
    assert sre.employment_raw == ["Full-time"] and sre.date_posted_raw == "2026-10-30T10:00:00.000Z"
    assert by["Platform Engineer (Remote)"].remote_raw == "remote"
    assert sre.description_html is None and sre.source_values["detail_pending"]
    job = norm(sre)
    assert job.city == "München" and job.country == "DE" and job.ats_type == "smartrecruiters"  # gazetteer name
    assert norm(by["Data Scientist"]).country == "AT"


async def test_smartrecruiters_paging_and_total_check(monkeypatch):
    monkeypatch.setattr(SmartRecruitersAdapter, "PAGE", 2)
    _, res, _ = await fetch_feed("https://jobs.smartrecruiters.com/AcmeSR")
    assert len(res.jobs) == 4 and res.pages == 2 and res.complete
    monkeypatch.undo()
    _, res, _ = await fetch_feed("https://jobs.smartrecruiters.com/ShortSR")  # claims 9, lists 4
    assert len(res.jobs) == 4 and res.reported_total == 9 and res.complete is False


async def test_smartrecruiters_catalogue_cap_is_flagged(monkeypatch):
    monkeypatch.setattr(SmartRecruitersAdapter, "PAGE", 2)
    monkeypatch.setattr(SmartRecruitersAdapter, "MAX_JOBS", 2)
    _, res, _ = await fetch_feed("https://jobs.smartrecruiters.com/AcmeSR")
    assert res.detail["capped"] is True and res.complete is False and len(res.jobs) == 2


def test_smartrecruiters_search_url_is_built_from_validated_parameters():
    a = SmartRecruitersAdapter()
    m = a.detect("https://jobs.smartrecruiters.com/AcmeSR")
    spec = a.capabilities.search
    q = spec.validate(caps.SearchQuery(keyword="Data & Analytics", country="de", city="München"))
    url = a.search_url(m, q, 100, 50)
    assert "q=Data+%26+Analytics" in url and "country=de" in url and "city=M%C3%BCnchen" in url
    assert "limit=50" in url and "offset=100" in url and "destination=PUBLIC" in url


# --- Ashby --------------------------------------------------------------------------------------


async def test_ashby_board_structure():
    m, res, _ = await fetch_feed("https://jobs.ashbyhq.com/acmeab")
    assert m.feed_url.endswith("/posting-api/job-board/acmeab?includeCompensation=true")
    assert res.complete and len(res.jobs) == 3  # the unlisted draft is not public
    assert res.detail["unlisted_skipped"] == 1
    by = {j.title: j for j in res.jobs}
    staff = by["Staff Software Engineer"]
    assert staff.salary and staff.salary == {**staff.salary, "min": 90000.0, "max": 120000.0, "currency": "EUR", "period": "year"}
    assert staff.locations[0] == {"city": "Berlin", "region": "Berlin", "country": "Germany"}
    assert {"city": "Hamburg", "country": "Germany"} in staff.locations
    assert by["Backend Engineer"].remote_raw == "remote"
    assert by["Working Student Data"].employment_raw == ["Internship"]
    job = norm(staff)
    assert job.city == "Berlin" and job.country == "DE" and job.salary_min == 90000 and "platform" in job.description


# --- detection ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,typ,acc",
    [
        ("https://jobs.smartrecruiters.com/BoschGroup/743999-foo", "smartrecruiters", "BoschGroup"),
        ("https://careers.smartrecruiters.com/DeliveryHero", "smartrecruiters", "DeliveryHero"),
        ("https://api.smartrecruiters.com/v1/companies/BoschGroup/postings?limit=5", "smartrecruiters", "BoschGroup"),
        ("https://jobs.smartrecruiters.com/oneclick-ui/company/Acme/publication/1", "smartrecruiters", "Acme"),
        ("https://jobs.ashbyhq.com/deepl", "ashby", "deepl"),
        ("https://jobs.ashbyhq.com/smallpdf/abc-123/application", "ashby", "smallpdf"),
        ("https://api.ashbyhq.com/posting-api/job-board/deepl", "ashby", "deepl"),
        ("https://boards.greenhouse.io/n26", "greenhouse", "n26"),
        ("https://job-boards.eu.greenhouse.io/bitpanda", "greenhouse", "bitpanda"),
        ("https://jobs.lever.co/acme", "lever", "acme"),
    ],
)
def test_detection_reads_the_account_from_evidence(url, typ, acc):
    m = ats.detect_url(url)
    assert m and (m.ats_type, m.account) == (typ, acc)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.smartrecruiters.com/",
        "https://jobs.smartrecruiters.com/",
        "https://jobs.smartrecruiters.com/oneclick-ui/",
        "https://jobs.ashbyhq.com/",
        "https://example.com/jobs.ashbyhq.com/deepl",
        "https://evil.com/?u=https://jobs.ashbyhq.com/deepl",
    ],
)
def test_detection_never_guesses(url):
    assert ats.detect_url(url) is None


def test_detection_finds_new_providers_in_inline_scripts():
    found = ats.detect([], 'var u="https://jobs.smartrecruiters.com/AcmeSR/embed"; x="https://jobs.ashbyhq.com/acmeab"')
    assert {(m.ats_type, m.account) for m in found} == {("smartrecruiters", "AcmeSR"), ("ashby", "acmeab")}


# --- capabilities are honest ---------------------------------------------------------------------


def test_only_validated_providers_are_live_ready():
    c = caps.CAPABILITIES
    assert c["greenhouse"].access_status == "live_validated"
    assert c["lever"].access_status == "live_empty_only"  # an empty board proves nothing about parsing
    assert c["smartrecruiters"].access_status == "blocked" and c["ashby"].access_status == "blocked"
    live_ready = {n for n, x in c.items() if x.access_status in caps.LIVE_READY}
    assert live_ready == {"greenhouse"}
    assert all(v.as_dict()["live_ready"] == (n == "greenhouse") for n, v in c.items() if v.access_status != "test_only")


def test_default_settings_keep_unverified_providers_off():
    enabled = caps.enabled_providers(CrawlerSettings().ats_enabled_providers)
    assert enabled == {"personio", "greenhouse", "lever"}
    assert not CrawlerSettings().ats_search_enabled


# --- access control: robots.txt still decides, even when a provider is enabled --------------------


def blocked_root(tmp_path, host: str, robots: str | None, status: int | None = None) -> Path:
    root = tmp_path / "sites"
    shutil.copytree(ATS_SITES, root)
    if robots is not None:
        (root / host / "robots.txt").write_text(robots)
    if status is not None:
        routes = json.loads((root / host / "_routes.json").read_text())
        routes["/robots.txt"] = {"status": status}
        (root / host / "_routes.json").write_text(json.dumps(routes))
    return root


async def test_smartrecruiters_robots_disallow_is_respected_when_enabled(tmp_path):
    root = blocked_root(tmp_path, "api.smartrecruiters.com", "User-agent: *\nDisallow: /v1/companies/\n")
    s = crawler_settings(ats_enabled_providers="personio,greenhouse,lever,smartrecruiters")
    r, t = await scan("gcc-ats-sr.de", settings=s, root=root)
    src = next(x for x in r.sources.values() if x.ats_type == "smartrecruiters")
    assert not src.ok and src.error == "robots_disallowed" and not src.complete
    assert not r.jobs and not any("/postings" in req for req in t.requests)  # the API itself was never requested
    assert not r.complete


async def test_ashby_unauthorised_robots_is_treated_as_blocked(tmp_path):
    root = blocked_root(tmp_path, "api.ashbyhq.com", None, status=401)
    (root / "api.ashbyhq.com" / "robots.txt").unlink()
    s = crawler_settings(ats_enabled_providers="personio,greenhouse,lever,ashby")
    r, t = await scan("gcc-ats-ashby.de", settings=s, root=root)
    src = next(x for x in r.sources.values() if x.ats_type == "ashby")
    assert not src.ok and not r.jobs
    assert not any("/posting-api/job-board" in req and not req.endswith("robots.txt") for req in t.requests)


async def test_disabled_provider_is_recorded_but_never_fetched():
    for host, typ in (("gcc-ats-sr.de", "smartrecruiters"), ("gcc-ats-ashby.de", "ashby")):
        r, t = await scan(host)  # default settings
        src = next(x for x in r.sources.values() if x.ats_type == typ)
        assert src.error == "provider_disabled" and src.strategy == "disabled" and not src.ok and not src.complete
        assert src.evidence["access_status"] == "blocked"
        assert not any(req.startswith(("api.smartrecruiters.com", "api.ashbyhq.com")) for req in t.requests)
        assert not r.jobs and not r.complete


async def test_enabled_smartrecruiters_and_ashby_work_end_to_end_offline():
    s = crawler_settings(ats_enabled_providers="personio,greenhouse,lever,smartrecruiters,ashby")
    r, _ = await scan("gcc-ats-sr.de", settings=s)
    assert len(r.jobs) == 4 and r.complete and {j.job.ats_type for j in r.jobs} == {"smartrecruiters"}
    src = next(x for x in r.sources.values() if x.ats_type == "smartrecruiters")
    assert src.strategy == "catalog" and src.reported_total == 4 and src.complete
    r, _ = await scan("gcc-ats-ashby.de", settings=s)
    assert len(r.jobs) == 3 and r.complete


async def test_greenhouse_end_to_end_from_company_page():
    r, _ = await scan("gcc-ats-n26.de")
    assert len(r.jobs) == 5 and r.complete
    src = next(x for x in r.sources.values() if x.ats_type == "greenhouse")
    assert src.strategy == "catalog" and src.reported_total == 5 and src.complete and src.ok


def test_robots_error_type_is_exported():
    assert issubclass(RobotsDisallowed, Exception) and AshbyAdapter().name == "ashby"


async def test_unauthorised_robots_is_strict_only_for_not_yet_cleared_ats_hosts(tmp_path):
    root = tmp_path / "sites"
    for host in ("api.ashbyhq.com", "api.smartrecruiters.com", "gcc-plain-company.de", "api.lever.co"):
        (root / host).mkdir(parents=True)
        (root / host / "_routes.json").write_text(json.dumps({"/robots.txt": {"status": 403}}))
    f, _ = fixture_fetcher(crawler_settings(), root=root)
    try:
        for url in ("https://api.ashbyhq.com/posting-api/job-board/x", "https://api.smartrecruiters.com/v1/companies/x/postings"):
            with pytest.raises(RobotsDisallowed):
                await f.robots.check(url)
        # everywhere else the established RFC 9309 behaviour is unchanged: a 4xx robots.txt means "no rules"
        await f.robots.check("https://gcc-plain-company.de/karriere")
        await f.robots.check("https://api.lever.co/v0/postings/x")
    finally:
        await f.aclose()


async def test_probe_reports_validation_verdicts_offline(monkeypatch, tmp_path):
    """The operator probe on offline fixtures: non-empty -> validated, empty -> NOT validated, robots -> blocked."""
    import shutil as sh

    from career_crawler import runner
    from career_crawler.ats import probe as probe_mod

    root = tmp_path / "sites"
    sh.copytree(ATS_SITES, root)
    (root / "api.smartrecruiters.com" / "robots.txt").write_text("User-agent: *\nDisallow: /v1/\n")
    settings = crawler_settings()
    monkeypatch.setattr("career_crawler.config.get_crawler_settings", lambda: settings)
    monkeypatch.setattr(runner, "build_fetcher", lambda s, **kw: fixture_fetcher(s, root=root)[0])
    ok = await probe_mod.probe("https://jobs.lever.co/acme", save=str(tmp_path / "raw"))
    assert ok["verdict"] == "validated_nonempty" and ok["postings"] == 3 and ok["coverage"]["location"] == 1.0
    assert (tmp_path / "raw" / "lever_acme_0.json").exists()
    empty = await probe_mod.probe("https://jobs.lever.co/empty")
    assert empty["verdict"] == "valid_but_empty" and "NOT validated" in empty["reason"]
    gh = await probe_mod.probe("https://boards.greenhouse.io/short")
    assert gh["verdict"] == "partial" and gh["reported_total"] == 5
    blocked = await probe_mod.probe("https://jobs.smartrecruiters.com/AcmeSR")
    assert blocked["verdict"] == "blocked" and blocked["access_status_in_code"] == "blocked"
    assert (await probe_mod.probe("https://example.com/careers"))["verdict"] == "unsupported"
