"""Search-aware crawling: parameter validation, bounded planning, pagination, dedup, completeness and
lifecycle safety - end to end against the mock search API (no restricted provider is contacted)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select

from career_crawler import ats
from career_crawler.ats import capabilities as caps
from career_crawler.ats.capabilities import SearchParamError, SearchQuery
from career_crawler.ats.mocksearch import CAPS, HOST, MockSearchAdapter, MockSearchTransport
from career_crawler.ats.searchcrawl import SearchLimits, SearchPlanner, run_search
from career_crawler.engine import CompanyInfo, SiteScan
from career_crawler.models import CareerSource, Job, JobEvent
from career_crawler.resources import ResourceController
from career_crawler.runner import BatchRunner
from gcc_core import scan_queue
from gcc_core.db import session_scope
from gcc_core.models import Company
from gcc_core.urls import domain_key

from .crawler_helpers import crawler_settings, fixture_fetcher, make_company
from .test_crawler_runner import CalmProbe

ATS_SITES = Path(__file__).parent / "fixtures" / "ats"
MOCK_HOST = "gcc-ats-mock.de"


@pytest.fixture(autouse=True)
def mock_provider():
    ats.register(MockSearchAdapter(), "https://search.mock-ats.example.com/{a}")
    yield
    ats.unregister("mocksearch")


def job(i: str, title: str, city: str, country: str, **kw) -> dict:
    return {"id": i, "title": title, "city": city, "country": country, "description": f"{title} in {city}", **kw}


def dataset() -> list[dict]:
    jobs = []
    for i in range(12):
        jobs.append(job(f"e{i:02}", f"Backend Engineer {i}", "Berlin", "DE"))
    for i in range(12, 22):
        jobs.append(job(f"e{i:02}", f"Platform Engineer {i}", "München", "DE"))
    for i in range(22, 30):
        jobs.append(job(f"e{i:02}", f"Cloud Engineer {i}", "Hamburg", "DE"))
    for i in range(4):
        jobs.append(job(f"a{i}", f"Software Engineer AT {i}", "Wien", "AT"))
    for i in range(3):
        jobs.append(job(f"d{i}", f"Data Scientist {i}", "Zürich", "CH"))
    for i in range(3, 5):
        jobs.append(job(f"d{i}", f"Data Analyst {i}", "Berlin", "DE"))
    jobs.append(job("x1", "Data Engineer", "Berlin", "DE"))  # matches *both* keywords
    for i in range(3):
        jobs.append(job(f"s{i}", f"Sales Representative {i}", "Berlin", "DE"))  # no keyword matches
    return jobs


ENGINEER_DE = {f"e{i:02}" for i in range(30)} | {"x1"}
FOUND_BY_KEYWORDS = ENGINEER_DE | {f"a{i}" for i in range(4)} | {f"d{i}" for i in range(5)}  # 40 postings


def search_settings(**kw):
    base = dict(
        ats_enabled_providers="personio,greenhouse,lever,mocksearch",
        ats_search_enabled=True,
        ats_search_keywords="engineer,data",
        ats_search_countries="DE,AT,CH",
        ats_search_max_pages=2,  # reachable per query: 2 x 10 = 20  -> big queries are saturated
    )
    base.update(kw)
    return crawler_settings(**base)


def transport(jobs=None, **kw) -> MockSearchTransport:
    return MockSearchTransport(ATS_SITES, {"acme": jobs if jobs is not None else dataset()}, **kw)


async def scan(settings, t=None):
    t = t or transport()
    f, t = fixture_fetcher(settings, transport=t)
    try:
        r = await SiteScan(f, settings, CompanyInfo(1, MOCK_HOST, domain_key(MOCK_HOST), f"https://{MOCK_HOST}/")).run()
    finally:
        await f.aclose()
    return r, t


def src_of(r):
    return next(s for s in r.sources.values() if s.ats_type == "mocksearch")


# --- parameter validation -------------------------------------------------------------------------


def test_parameters_are_validated_before_any_url_is_built():
    spec = CAPS.search
    ok = spec.validate(SearchQuery(keyword="  engineer ", country="de", city="München"))
    assert (ok.keyword, ok.country, ok.city) == ("engineer", "DE", "München")
    for bad in (
        SearchQuery(keyword="x" * 61),
        SearchQuery(keyword="a\nb"),
        SearchQuery(keyword="   "),
        SearchQuery(country="DEU"),
        SearchQuery(country="1x"),
        SearchQuery(city="Berlin\x00"),
        SearchQuery(region="Bayern"),  # the mock provider has no region parameter
    ):
        with pytest.raises(SearchParamError):
            spec.validate(bad)
    assert spec.page_size(1000) == 10 and spec.page_size(0) == 1


async def test_invalid_query_is_recorded_and_never_sent():
    f, t = fixture_fetcher(search_settings(), transport=transport())
    m = ats.detect_url("https://search.mock-ats.example.com/acme")
    try:
        out = await run_search(
            f, MockSearchAdapter(), m, keywords=["ok", "bad\nkeyword"], countries=["DE"], limits=SearchLimits(max_queries=10)
        )
    finally:
        await f.aclose()
    assert [q.error is not None for q in out.queries] == [False, True]
    assert out.queries[1].error.startswith("invalid_param")
    assert all("bad" not in u for u in t.api_calls)


# --- planning -------------------------------------------------------------------------------------


def test_planner_is_bounded_deduplicated_and_breadth_first():
    p = SearchPlanner(CAPS, keywords=["engineer", "data", "Engineer"], countries=["DE", "AT"], max_queries=3)
    got = list(p)
    assert len(got) == 3 and p.pending == 1  # 2 countries x 2 distinct keywords (case-insensitive) = 4, budget 3
    keys = [q.key() for q in got]
    assert len(set(keys)) == len(keys)
    assert all(q.city is None for q in got)  # base queries first
    # refinement only on request, only for providers with a city parameter, and only once
    p2 = SearchPlanner(CAPS, keywords=["engineer"], countries=["DE"], max_queries=50)
    base = next(p2)
    assert p2.refine(base) == 9 and p2.refine(base) == 0
    assert p2.refine(SearchQuery(keyword="x", country="DE", city="Berlin")) == 0
    assert len(list(p2)) == 9


def test_planner_without_keyword_or_country_support_degrades_gracefully():
    spec = caps.SearchSpec(params={"keyword": caps.ParamSpec("keyword", "keyword")})
    only_kw = caps.ProviderCapabilities("x", False, False, spec, "fixture_only", "t")
    got = list(SearchPlanner(only_kw, keywords=["a", "b"], countries=["DE", "AT"], max_queries=10))
    assert [(q.keyword, q.country) for q in got] == [("a", None), ("b", None)]  # country ignored, not sent


def test_strategy_selection():
    cap = caps.CAPABILITIES
    assert caps.select_strategy(cap["greenhouse"], search_enabled=True).name == "catalog"
    assert caps.select_strategy(CAPS, search_enabled=False).name == "none"
    assert caps.select_strategy(CAPS, search_enabled=True).name == "search"
    assert caps.needs_search_supplement(cap["smartrecruiters"], catalog_capped=True, search_enabled=True)
    assert not caps.needs_search_supplement(cap["smartrecruiters"], catalog_capped=False, search_enabled=True)
    assert not caps.needs_search_supplement(cap["greenhouse"], catalog_capped=True, search_enabled=True)  # no search API


# --- end to end ----------------------------------------------------------------------------------


async def test_keyword_and_location_search_end_to_end():
    r, t = await scan(search_settings())
    assert {j.job.ats_job_id for j in r.jobs} == FOUND_BY_KEYWORDS
    assert len(r.jobs) == 40  # the shared posting x1 is stored once although two keywords return it
    src = src_of(r)
    assert src.strategy == "search" and src.ok and not src.complete and not r.complete
    d = src.detail
    assert d["queries_saturated"] >= 1 and d["queries_run"] > 6  # saturated DE/engineer was split by city
    assert d["queries_run"] <= 40 and d["queries_failed"] == 0 and d["unique_jobs"] == 40
    assert any("city=Berlin" in u for u in t.api_calls) and any("city=M%C3%BCnchen" in u for u in t.api_calls)
    by = {j.job.ats_job_id: j.job for j in r.jobs}
    assert by["a0"].country == "AT" and by["a0"].city == "Wien"
    assert by["d0"].city == "Zürich" and by["d0"].country == "CH"
    assert by["x1"].ats_type == "mocksearch" and by["x1"].method == "ats"


async def test_location_search_finds_the_postings_a_big_keyword_query_cannot_reach():
    # With a window of 40 and 2 pages of 10, "engineer in DE" (31 postings) is cut off at 20 ...
    s = search_settings(ats_search_max_queries=1, ats_search_keywords="engineer", ats_search_countries="DE")
    r, _ = await scan(s)
    assert len(r.jobs) == 20 and src_of(r).detail["queries_saturated"] == 1
    # ... and the bounded city refinement recovers all of them
    r, _ = await scan(search_settings(ats_search_keywords="engineer", ats_search_countries="DE"))
    assert {j.job.ats_job_id for j in r.jobs} == ENGINEER_DE


async def test_query_and_job_caps_bound_the_work():
    r, t = await scan(search_settings(ats_search_max_queries=2))
    d = src_of(r).detail
    assert d["queries_run"] == 2 and d["skipped_by_budget"] > 0 and len(t.api_calls) <= 2 * 2
    r, _ = await scan(search_settings(ats_search_max_jobs=15))
    assert len(r.jobs) == 15 and src_of(r).detail["job_cap_hit"] and src_of(r).detail["aborted"] == "job_cap"


async def test_search_only_provider_is_skipped_when_search_is_disabled():
    r, t = await scan(search_settings(ats_search_enabled=False))
    src = src_of(r)
    assert src.error == "search_disabled" and src.strategy == "disabled" and not r.jobs and not t.api_calls


async def test_failed_queries_make_the_result_smaller_not_wrong():
    t = transport(fail_when=lambda u: 500 if "country=AT" in u else None)
    r, _ = await scan(search_settings(), t)
    src = src_of(r)
    assert src.ok and not src.complete and src.detail["queries_failed"] >= 1
    assert {j.job.ats_job_id for j in r.jobs} == FOUND_BY_KEYWORDS - {f"a{i}" for i in range(4)}


async def test_all_queries_failing_is_a_source_error_with_no_jobs():
    r, _ = await scan(search_settings(), transport(fail_when=lambda u: 500))
    src = src_of(r)
    assert not src.ok and not src.complete and not r.jobs and src.error
    assert src.detail["aborted"] == "too_many_errors"


async def test_rate_limit_keeps_partial_results_and_stops_the_scan_politely():
    calls = {"n": 0}

    def after_three(u):
        calls["n"] += 1
        return 429 if calls["n"] > 3 else None

    r, t = await scan(search_settings(), transport(fail_when=after_three))
    assert r.stop is not None and r.stop.kind == "rate_limited" and r.stop.retry_after_s == 120
    assert len(r.jobs) > 0 and not r.complete
    assert len(t.api_calls) == 4  # no hammering after the 429


async def test_robots_disallow_stops_the_search(tmp_path):
    t = transport()
    orig = t.handle_async_request

    async def with_robots(request):
        if request.url.path == "/robots.txt" and request.headers.get("host", "").split(":")[0] == HOST:
            import httpx

            return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"User-agent: *\nDisallow: /api/\n", request=request)
        return await orig(request)

    t.handle_async_request = with_robots
    r, _ = await scan(search_settings(), t)
    src = src_of(r)
    assert not src.ok and src.error == "robots_disallowed" and not r.jobs and not t.api_calls


async def test_capped_catalogue_is_supplemented_by_search_and_never_complete(monkeypatch):
    """SmartRecruiters-style provider: catalogue cut at the cap -> bounded search adds the DACH slices."""
    import httpx

    from career_crawler.ats.smartrecruiters import SmartRecruitersAdapter

    monkeypatch.setattr(SmartRecruitersAdapter, "PAGE", 5)
    posts = [
        {"id": str(i), "name": f"Engineer {i}", "releasedDate": "2026-10-01T00:00:00Z",
         "location": {"city": "Berlin" if i % 2 else "Madrid", "country": "de" if i % 2 else "es"}}
        for i in range(1, 31)
    ]

    class SrTransport(MockSearchTransport):
        async def handle_async_request(self, request):
            if request.headers.get("host", "").split(":")[0] != "api.smartrecruiters.com":
                return await super().handle_async_request(request)
            self.requests.append(f"api.smartrecruiters.com{request.url.path}?{request.url.query.decode()}")
            if request.url.path == "/robots.txt":
                return httpx.Response(200, content=b"User-agent: *\nAllow: /\n", request=request)
            q = dict(request.url.params)
            rows = [p for p in posts if (not q.get("country") or p["location"]["country"] == q["country"])]
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 5))
            return httpx.Response(200, json={"offset": off, "limit": lim, "totalFound": len(rows), "content": rows[off : off + lim]}, request=request)

    t = SrTransport(ATS_SITES, {"acme": []})
    s = crawler_settings(
        ats_enabled_providers="smartrecruiters",
        ats_catalog_max_jobs=10,
        ats_search_enabled=True,
        ats_search_keywords="",
        ats_search_countries="DE",
        ats_search_page_size=5,
        ats_search_max_pages=10,
    )
    r, _ = await scan_host("gcc-ats-sr.de", s, t)
    src = next(x for x in r.sources.values() if x.ats_type == "smartrecruiters")
    assert src.strategy == "catalog+search" and src.ok and not src.complete and not r.complete
    assert src.detail["capped"] and src.detail["search"]["queries_run"] >= 1
    ids = {j.job.ats_job_id for j in r.jobs}
    assert {str(i) for i in range(1, 31) if i % 2} <= ids  # every Berlin posting, even beyond the cap
    assert len(ids) == len(r.jobs)  # catalogue part and search part overlap: stored once


async def scan_host(host, settings, t):
    f, t = fixture_fetcher(settings, transport=t)
    try:
        r = await SiteScan(f, settings, CompanyInfo(1, host, domain_key(host), f"https://{host}/")).run()
    finally:
        await f.aclose()
    return r, t


# --- persistence, events and lifecycle safety -----------------------------------------------------


async def run_batch(settings, t):
    f, t = fixture_fetcher(settings, transport=t)
    runner = BatchRunner(settings, worker="w", fetcher=f, controller=ResourceController(settings, probe=CalmProbe()))
    try:
        return await runner.run(time_budget_s=120)
    finally:
        await f.aclose()


def enqueue(host: str = MOCK_HOST) -> int:
    with session_scope() as s:
        c = db_company(s, host)
        scan_queue.request_scan(s, c, source="test")
        return c.id


def db_company(s, host):
    c = s.execute(select(Company).where(Company.website_url == f"https://{host}/")).scalar_one_or_none()
    return c or make_company(s, host)


async def test_search_results_are_stored_with_events_and_diagnostics(db):
    s = search_settings()
    enqueue()
    await run_batch(s, transport())
    jobs = db.execute(select(Job)).scalars().all()
    assert len(jobs) == 40 and all(j.status == "open" and j.ats_type == "mocksearch" for j in jobs)
    created = db.execute(select(func.count()).select_from(JobEvent).where(JobEvent.event_type == "job.created")).scalar()
    assert created == 40
    src = db.execute(select(CareerSource).where(CareerSource.ats_type == "mocksearch")).scalar_one()
    assert src.scan_strategy == "search" and src.scan_complete is False and src.last_complete_scan_at is None
    assert src.scan_detail["queries_run"] > 6 and src.last_scan_at is not None and src.last_error is None
    assert all(j.career_source_id == src.id for j in jobs)
    # a second identical scan neither duplicates nor emits new "created" events
    enqueue()
    await run_batch(s, transport())
    db.expire_all()
    assert db.execute(select(func.count()).select_from(Job)).scalar() == 40
    assert db.execute(select(func.count()).select_from(JobEvent).where(JobEvent.event_type == "job.created")).scalar() == 40


async def test_partial_failed_and_narrow_searches_never_close_vacancies(db):
    # aggressive lifecycle: ONE counted miss would close a posting immediately
    strict = dict(close_after_misses=1, close_min_days=0)
    enqueue()
    await run_batch(search_settings(**strict), transport())
    assert db.execute(select(func.count()).select_from(Job).where(Job.status == "open")).scalar() == 40

    scenarios = {
        "narrow filter": (search_settings(ats_search_keywords="data", ats_search_countries="CH", **strict), transport()),
        "nothing matches": (search_settings(ats_search_keywords="zzz", **strict), transport()),
        "all queries fail": (search_settings(**strict), transport(fail_when=lambda u: 500)),
        "rate limited": (search_settings(**strict), transport(fail_when=lambda u: 429)),
        "provider empty": (search_settings(**strict), transport(jobs=[])),
        "search turned off": (search_settings(ats_search_enabled=False, **strict), transport()),
        "provider disabled": (search_settings(ats_enabled_providers="personio,greenhouse,lever", **strict), transport()),
    }
    for name, (settings, t) in scenarios.items():
        enqueue()
        await run_batch(settings, t)
        db.expire_all()
        rows = db.execute(select(Job)).scalars().all()
        assert len(rows) == 40, name
        assert all(j.status == "open" and j.miss_count == 0 for j in rows), name
    closed_events = db.execute(select(func.count()).select_from(JobEvent).where(JobEvent.event_type == "job.closed")).scalar()
    assert closed_events == 0


async def test_control_a_complete_catalogue_does_close_under_the_same_settings(db, tmp_path):
    """Guards the test above: with these settings a *complete* read really does close a missing posting."""
    root = tmp_path / "sites"
    shutil.copytree(ATS_SITES, root)
    strict = dict(close_after_misses=1, close_min_days=0)
    host = "gcc-ats-n26.de"
    f, _ = fixture_fetcher(crawler_settings(**strict), root=root)
    s = crawler_settings(**strict)
    enqueue(host)
    runner = BatchRunner(s, worker="w", fetcher=f, controller=ResourceController(s, probe=CalmProbe()))
    await runner.run(time_budget_s=60)
    assert db.execute(select(func.count()).select_from(Job).where(Job.status == "open")).scalar() == 5
    p = root / "boards-api.greenhouse.io" / "n26like.json"
    data = json.loads(p.read_text())
    data["jobs"] = data["jobs"][:-1]
    data["meta"]["total"] = 4
    p.write_text(json.dumps(data))
    enqueue(host)
    await runner.run(time_budget_s=60)
    await f.aclose()
    db.expire_all()
    assert db.execute(select(func.count()).select_from(Job).where(Job.status == "closed")).scalar() == 1


# --- admin diagnostics -----------------------------------------------------------------------------

H = {"X-Requested-With": "gcc-ui"}


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from gcc_core.app import create_app

    c = TestClient(create_app())
    assert c.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"}, headers=H).status_code == 200
    return c


def test_diagnostics_require_login():
    from fastapi.testclient import TestClient

    from gcc_core.app import create_app

    c = TestClient(create_app())
    assert c.get("/api/crawler/ats/providers").status_code in (401, 403)
    assert c.get("/api/crawler/ats/sources").status_code in (401, 403)


async def test_admin_diagnostics_show_capabilities_access_counts_completeness_and_errors(client, db, tmp_path):
    # 1) a search-scanned provider (partial by design)
    enqueue()
    await run_batch(search_settings(), transport())
    # 2) a complete greenhouse catalogue
    enqueue("gcc-ats-n26.de")
    s = crawler_settings()
    f, _ = fixture_fetcher(s, root=ATS_SITES)
    await BatchRunner(s, worker="w", fetcher=f, controller=ResourceController(s, probe=CalmProbe())).run(time_budget_s=60)
    await f.aclose()
    # 3) smartrecruiters detected but the provider is disabled (default); ashby enabled but robots-blocked
    enqueue("gcc-ats-sr.de")
    blocked = tmp_path / "sites"
    shutil.copytree(ATS_SITES, blocked)
    (blocked / "api.ashbyhq.com" / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    s2 = crawler_settings(ats_enabled_providers="personio,greenhouse,lever,ashby")
    f, _ = fixture_fetcher(s2, root=blocked)
    enqueue("gcc-ats-ashby.de")
    await BatchRunner(s2, worker="w", fetcher=f, controller=ResourceController(s2, probe=CalmProbe())).run(time_budget_s=60)
    await f.aclose()

    body = client.get("/api/crawler/ats/providers").json()
    assert body["settings"]["enabled_providers"] == ["greenhouse", "lever", "personio"]
    assert body["settings"]["search_enabled"] is False
    p = {x["provider"]: x for x in body["providers"]}
    assert {"personio", "greenhouse", "lever", "smartrecruiters", "ashby"} <= set(p)

    gh = p["greenhouse"]
    assert gh["capabilities"]["live_ready"] is True and gh["capabilities"]["access_status"] == "live_validated"
    assert gh["observed_access"] == "ok" and gh["open_jobs"] == 5 and gh["jobs_in_last_scans"] == 5
    assert gh["sources"]["scan_complete"] == 1 and gh["strategies"] == {"catalog": 1}
    assert gh["last_complete_scan_at"] is not None

    ms = p["mocksearch"]
    assert ms["open_jobs"] == 40 and ms["sources"]["scan_partial"] == 1 and ms["sources"]["scan_complete"] == 0
    assert ms["strategies"] == {"search": 1} and ms["last_complete_scan_at"] is None

    sr = p["smartrecruiters"]
    assert sr["enabled"] is False and sr["capabilities"]["live_ready"] is False
    assert sr["observed_access"] == "not_fetched" and sr["errors"][0]["error"] == "provider_disabled"
    assert sr["capabilities"]["search"]["unverified_params"]

    ab = p["ashby"]
    assert ab["enabled"] is False  # the API reads the process settings; the scan above used a test override
    assert ab["observed_access"] == "robots_blocked" and ab["errors"][0] == {**ab["errors"][0], "error": "robots_disallowed"}
    assert p["lever"]["capabilities"]["access_status"] == "live_empty_only" and p["lever"]["observed_access"] == "no_data"

    srcs = client.get("/api/crawler/ats/sources", params={"only": "incomplete"}).json()
    assert [x["provider"] for x in srcs] == ["mocksearch"]
    assert srcs[0]["search"]["queries_run"] > 6 and srcs[0]["scan_complete"] is False
    assert {x["provider"] for x in client.get("/api/crawler/ats/sources", params={"only": "disabled"}).json()} == {"smartrecruiters"}
    assert {x["provider"] for x in client.get("/api/crawler/ats/sources", params={"only": "failing"}).json()} == {"ashby"}
    assert {x["provider"] for x in client.get("/api/crawler/ats/sources", params={"only": "search"}).json()} == {"mocksearch"}
    assert client.get("/api/crawler/ats/sources", params={"only": "bogus"}).status_code == 422
    assert client.get("/api/crawler/ats/sources", params={"provider": "greenhouse"}).json()[0]["reported_total"] == 5
