"""Vacancy statistics dashboard + Telegram reports: counting rules, periods, geography, filters, security."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from career_crawler import jobstats, statspublish, statsreport
from career_crawler.config import CrawlerSettings, get_crawler_settings
from career_crawler.models import Job, StatPublication
from gcc_core.app import create_app
from gcc_core.models import GeoLocation

from .crawler_helpers import make_company
from .dach_helpers import company as dach_company
from .dach_helpers import ingest, raw
from .stats_helpers import add_event, add_job, utc
from .test_dach_events import age, posting, scan

H = {"X-Requested-With": "gcc-ui"}
BASE = "/api/crawler/stats/jobs"
SNAP = "2026-05-20T10:00:00+00:00"  # Wed 12:00 in Berlin (CEST, UTC+2)
EP = ("summary", "trends", "specializations", "cities", "countries", "specialization-city", "config")


# --- fixtures ------------------------------------------------------------------------------------------------


@pytest.fixture
def make_client():
    clients = []

    def make(settings: CrawlerSettings | None = None, login: bool = True) -> TestClient:
        app = create_app()
        if settings is not None:
            app.dependency_overrides[get_crawler_settings] = lambda: settings
        c = TestClient(app)
        if login:
            r = c.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"}, headers=H)
            assert r.status_code == 200, r.text
        clients.append(c)
        return c

    return make


@pytest.fixture
def client(make_client):
    return make_client()


def get(c: TestClient, path: str, **params):
    params.setdefault("as_of", SNAP)
    return c.get(f"{BASE}/{path}", params=params)


def ok(c: TestClient, path: str, **params) -> dict:
    r = get(c, path, **params)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def world(db):
    """Nine vacancies around SNAP (2026-05-20 10:00 UTC). Local day 20 May starts 19 May 22:00 UTC."""
    a, b = make_company(db, "a.de", name="A GmbH"), make_company(db, "b.de", name="B GmbH")
    j = {}
    j["n1"] = add_job(db, a, 1, at=utc(2026, 5, 20, 6), cities=("Berlin",), spec="backend-development")
    j["n2"] = add_job(db, a, 2, at=utc(2026, 5, 20, 7), cities=("Berlin", "München"), spec="devops-platform", secondary=("backend-development",))
    j["n3"] = add_job(db, a, 3, at=utc(2026, 5, 20, 8), cities=("München",))
    j["n4"] = add_job(db, b, 4, at=utc(2026, 5, 19, 21, 30), cities=("Wien",), spec="backend-development")  # 23:30 local, 19 May
    j["n5"] = add_job(db, b, 5, at=utc(2026, 5, 19, 10), cities=("Zürich",), spec="qa-testing")  # exactly 24 h before SNAP
    j["n6"] = add_job(db, b, 6, at=utc(2026, 5, 18, 12), country="DE", work_model="REMOTE", spec="backend-development")
    j["n7"] = add_job(db, a, 7, at=utc(2026, 5, 17, 12))  # no place, no country, no specialization
    j["n8"] = add_job(db, a, 8, at=utc(2026, 5, 10, 12), cities=("Hamburg",), status="closed")
    j["n9"] = add_job(db, a, 9, at=utc(2026, 5, 20, 5), countries=("PL",))  # outside DACH
    db.commit()
    return j


# --- the counting rule -----------------------------------------------------------------------------------------


def test_new_counts_use_discovery_time_and_each_job_once(client, world):
    s = ok(client, "summary", period="today")
    new = {k["key"]: k["new"] for k in s["kpis"]}
    assert new == {"today": 3, "last_24h": 5, "7d": 7, "30d": 8}  # 30 d includes the closed vacancy; PL is out of scope
    assert s["new"]["total"] == 3
    assert s["active"] == 7  # n1..n7 open; n8 closed; n9 outside DACH
    assert s["period"]["timezone"] == "Europe/Berlin"
    assert s["period"]["start"] == "2026-05-19T22:00:00+00:00"
    assert ok(client, "summary", period="yesterday")["new"]["total"] == 2  # n4 (23:30 local) and n5


def test_calendar_day_and_rolling_24h_differ(client, world):
    today = ok(client, "summary", period="today")["new"]["total"]
    rolling = ok(client, "summary", period="last_24h")["new"]["total"]
    assert (today, rolling) == (3, 5)
    p = ok(client, "summary", period="last_24h")["period"]
    assert p["calendar_days"] is False and p["start"] == "2026-05-19T10:00:00+00:00"  # inclusive start


def test_rescan_update_close_reopen_never_count_as_new(db, client):
    co = dach_company(db, "ev.de", "Ev GmbH", "DE")
    scan(db, co, [posting()])
    first = db.query(Job).one().discovered_at
    scan(db, co, [posting()])  # plain rescan
    scan(db, co, [posting(description_text=posting().description_text + " Neu: Homeoffice möglich und mehr Gehalt.")])
    age(db, co)
    scan(db, co, [], complete=True)
    scan(db, co, [], complete=True)  # closed
    scan(db, co, [posting()])  # reopened
    db.expire_all()
    job = db.query(Job).one()
    assert job.discovered_at == first and job.status == "open"
    ev = dict(db.execute(text("SELECT event_type, count(*) FROM job_events GROUP BY 1")).all())
    assert ev["job.created"] == 1 and ev["job.closed"] == 1 and ev["job.reopened"] == 1
    s = client.get(f"{BASE}/summary", params={"period": "today"}).json()
    assert s["new"]["total"] == 1
    assert s["closed"] == 1 and s["reopened"] == 1
    assert s["data_quality"]["created_event_pct"] == 100.0


def test_updates_reopens_and_closes_are_counted_separately(db, client, world):
    add_event(db, world["n1"], "updated", utc(2026, 5, 20, 9))
    add_event(db, world["n1"], "updated", utc(2026, 5, 20, 9, 30))  # same vacancy twice: one vacancy updated
    add_event(db, world["n4"], "closed", utc(2026, 5, 20, 8))
    add_event(db, world["n8"], "reopened", utc(2026, 5, 20, 8))
    db.commit()
    s = ok(client, "summary", period="today")
    assert (s["updated"], s["closed"], s["reopened"]) == (1, 1, 1)
    assert s["new"]["total"] == 3  # none of them is "new"


def test_duplicate_urls_and_ats_sources_do_not_inflate(db, client):
    co = dach_company(db, "dup.de", "Dup GmbH", "DE")
    a = raw("Python Entwickler (m/w/d)", "dup.de", 1, location_raw="Berlin", ats_type="greenhouse", ats_account="dup", ats_job_id="42")
    # the same posting reached through its tracking-parameter URL and through the ATS board
    b = raw("Python Entwickler (m/w/d)", "dup.de", 1, source_url="https://dup.de/jobs/1?utm_source=x&gh_src=y", location_raw="Berlin", ats_type="greenhouse", ats_account="dup", ats_job_id="42")
    c = raw("Python Entwickler (m/w/d)", "boards.greenhouse.io", 7, source_url="https://boards.greenhouse.io/dup/jobs/42", location_raw="Berlin", ats_type="greenhouse", ats_account="dup", ats_job_id="42")
    ingest(db, co, [a, b, c])
    assert db.query(Job).count() == 1
    s = client.get(f"{BASE}/summary", params={"period": "today"}).json()
    assert s["new"]["total"] == 1


def test_original_first_discovery_time_survives_rescans(db, client, world):
    before = db.get(Job, world["n1"].id).discovered_at
    co = db.get(Job, world["n1"].id).company_id
    assert co
    db.execute(text("UPDATE jobs SET last_seen_at = now(), updated_at = now(), title = 'Geändert'"))
    db.commit()
    db.expire_all()
    assert db.get(Job, world["n1"].id).discovered_at == before
    assert ok(client, "summary", period="today")["new"]["total"] == 3


# --- time zones, boundaries, DST ---------------------------------------------------------------------------------


def test_day_boundaries_are_start_inclusive_end_exclusive(db, client):
    co = make_company(db, "edge.de", name="Edge")
    add_job(db, co, 1, at=utc(2026, 5, 19, 21, 59), cities=("Berlin",))  # last minute of 19 May (local)
    add_job(db, co, 2, at=utc(2026, 5, 19, 22, 0), cities=("Berlin",))  # exactly local midnight → 20 May
    add_job(db, co, 3, at=utc(2026, 5, 20, 21, 59), cities=("Berlin",))  # in the future of SNAP, but in the 20 May day
    db.commit()
    assert ok(client, "summary", period="yesterday")["new"]["total"] == 1
    assert ok(client, "summary", period="today")["new"]["total"] == 1  # the future one is after the snapshot
    r = ok(client, "summary", period="custom", **{"from": "2026-05-19", "to": "2026-05-19"})
    assert r["new"]["total"] == 1
    assert ok(client, "summary", period="custom", **{"from": "2026-05-19", "to": "2026-05-20"}, as_of="2026-05-21T00:00:00+00:00")["new"]["total"] == 3


def test_timezone_is_configurable(db, client):
    co = make_company(db, "tz.de", name="Tz")
    add_job(db, co, 1, at=utc(2026, 5, 20, 3), cities=("Berlin",))  # 05:00 Berlin, 23:00 on 19 May in New York
    db.commit()
    assert ok(client, "summary", period="today")["new"]["total"] == 1
    ny = ok(client, "summary", period="today", timezone="America/New_York")
    assert ny["new"]["total"] == 0 and ny["period"]["timezone"] == "America/New_York"
    assert ok(client, "summary", period="yesterday", timezone="America/New_York")["new"]["total"] == 1


def test_spring_forward_day_is_23_hours_and_buckets_are_right(db, client):
    co = make_company(db, "dst.de", name="Dst")
    add_job(db, co, 1, at=utc(2026, 3, 28, 23, 0), cities=("Berlin",))  # 00:00 CET on 29 Mar: first minute
    add_job(db, co, 2, at=utc(2026, 3, 29, 21, 59), cities=("Berlin",))  # 23:59 CEST on 29 Mar: last minute
    add_job(db, co, 3, at=utc(2026, 3, 29, 22, 0), cities=("Berlin",))  # 00:00 on 30 Mar
    db.commit()
    kw = dict(period="custom", **{"from": "2026-03-29", "to": "2026-03-29"}, as_of="2026-04-02T00:00:00+00:00")
    s = ok(client, "summary", **kw)
    assert s["new"]["total"] == 2
    start, end = dt.datetime.fromisoformat(s["period"]["start"]), dt.datetime.fromisoformat(s["period"]["end"])
    assert end - start == dt.timedelta(hours=23)
    t = ok(client, "trends", period="custom", **{"from": "2026-03-28", "to": "2026-03-30"}, as_of="2026-04-02T00:00:00+00:00")
    assert [(p["label"], p["count"]) for p in t["points"]] == [("2026-03-28", 0), ("2026-03-29", 2), ("2026-03-30", 1)]


def test_autumn_back_day_is_25_hours(db, client):
    co = make_company(db, "dst2.de", name="Dst2")
    add_job(db, co, 1, at=utc(2026, 10, 25, 22, 30), cities=("Berlin",))  # 23:30 CET, still 25 Oct
    add_job(db, co, 2, at=utc(2026, 10, 25, 23, 0), cities=("Berlin",))  # 00:00 on 26 Oct
    db.commit()
    kw = dict(period="custom", **{"from": "2026-10-25", "to": "2026-10-25"}, as_of="2026-10-30T00:00:00+00:00")
    s = ok(client, "summary", **kw)
    assert s["new"]["total"] == 1
    start, end = dt.datetime.fromisoformat(s["period"]["start"]), dt.datetime.fromisoformat(s["period"]["end"])
    assert end - start == dt.timedelta(hours=25)


def test_previous_period_has_the_same_elapsed_length(client, world):
    p = ok(client, "summary", period="today")["period"]
    assert p["in_progress"] is True and p["counted_until"].startswith("2026-05-20T10:00")
    # today so far = 12 h; the previous period is yesterday's first 12 h, not the whole day
    assert p["previous"] == {"start": "2026-05-18T22:00:00+00:00", "end": "2026-05-19T10:00:00+00:00"}
    s = ok(client, "summary", period="today")
    assert s["new"]["previous"] == 0 and s["new"]["change_pct"] is None  # n4 (21:30) is after 10:00 UTC
    w = ok(client, "summary", period="7d")["new"]
    assert w["total"] == 7 and w["previous"] == 1  # n8 (10 May) lies in the previous 6.5 days


# --- geography ----------------------------------------------------------------------------------------------------


def test_unique_city_breakdown_adds_up_and_buckets_are_explicit(client, world):
    r = ok(client, "cities", period="30d", limit=0)
    rows = {x["label"]: x for x in r["rows"]}
    assert r["additive"] is True and r["totals"]["sel"] == 8 == sum(x["sel"] for x in r["rows"])
    assert {k: v["sel"] for k, v in rows.items()} == {
        "Berlin": 1, "München": 1, "Wien": 1, "Zürich": 1, "Hamburg": 1,
        "Multi-location (Germany)": 1, "Remote (no location)": 1, "Unknown location": 1,
    }  # fmt: skip
    assert rows["Multi-location (Germany)"]["kind"] == "multi" and rows["Multi-location (Germany)"]["clickable"] is False
    assert rows["Berlin"]["country_code"] == "DE" and rows["Berlin"]["region_code"] == "DE-BE"


def test_coverage_mode_overlaps_and_is_not_additive(client, world):
    r = ok(client, "cities", period="30d", mode="coverage", limit=0)
    rows = {x["label"]: x["sel"] for x in r["rows"]}
    assert r["additive"] is False and r["totals"] is None
    assert rows["Berlin"] == 2 and rows["München"] == 2  # n2 lists both cities
    assert sum(rows.values()) > 8


def test_same_named_cities_in_different_countries_stay_distinct(db, client):
    de = GeoLocation(loc_key="DE|DE-SN|neustadt", country_code="DE", region_code="DE-SN", city="Neustadt", search_name="neustadt")
    at = GeoLocation(loc_key="AT|AT-3|neustadt", country_code="AT", region_code="AT-3", city="Neustadt", search_name="neustadt")
    db.add_all([de, at])
    db.commit()
    try:
        co = make_company(db, "ns.de", name="Ns")
        add_job(db, co, 1, at=utc(2026, 5, 20, 6), cities=("Neustadt",), country="DE")
        add_job(db, co, 2, at=utc(2026, 5, 20, 6, 5), cities=("Neustadt",), country="AT")
        add_job(db, co, 3, at=utc(2026, 5, 20, 6, 10), cities=("Neustadt",), country="AT")
        db.commit()
        rows = ok(client, "cities", period="today", limit=0)["rows"]
        got = {(x["country_code"], x["region_code"]): x["sel"] for x in rows}
        assert got == {("DE", "DE-SN"): 1, ("AT", "AT-3"): 2}
        assert ok(client, "summary", period="today", city="Neustadt", country="AT")["new"]["total"] == 2
        assert ok(client, "summary", period="today", city="Neustadt")["new"]["total"] == 3
        assert ok(client, "summary", period="today", city="neustadt", country="DE")["new"]["total"] == 1  # case-insensitive
    finally:
        db.execute(text("DELETE FROM job_locations WHERE location_id IN (:a, :b)"), {"a": de.id, "b": at.id})
        db.execute(text("DELETE FROM jobs"))
        db.execute(text("DELETE FROM geo_locations WHERE id IN (:a, :b)"), {"a": de.id, "b": at.id})
        db.commit()


def test_city_aliases_resolve_to_one_city(db, client):
    co = dach_company(db, "alias.de", "Alias GmbH", "DE")
    ingest(db, co, [raw("Entwickler A", "alias.de", 1, location_raw="München"), raw("Entwickler B", "alias.de", 2, location_raw="Munich"), raw("Entwickler C", "alias.de", 3, location_raw="MÜNCHEN, Bayern")])
    rows = client.get(f"{BASE}/cities", params={"period": "today", "limit": 0}).json()["rows"]
    assert [(x["label"], x["sel"]) for x in rows] == [("München", 3)]


def test_region_and_country_levels_and_drilldown(client, world):
    reg = ok(client, "cities", period="30d", level="region", limit=0)
    got = {x["region_code"]: x["sel"] for x in reg["rows"] if x["kind"] == "region"}
    assert got == {"DE-BE": 1, "DE-BY": 1, "AT-9": 1, "CH-ZH": 1, "DE-HH": 1}
    only_be = ok(client, "cities", period="30d", region="DE-BE", limit=0)["rows"]
    assert [x["label"] for x in only_be if x["kind"] == "city"] == ["Berlin"]
    assert ok(client, "summary", period="30d", region="DE-BE")["new"]["total"] == 2  # n1 and the multi-location n2


def test_countries_chart_buckets(client, world):
    r = ok(client, "countries", period="30d")
    rows = {x["code"]: x["sel"] for x in r["rows"]}
    assert rows == {"DE": 5, "AT": 1, "CH": 1, "UNKNOWN": 1}
    assert r["totals"]["sel"] == 8 and r["empty"] is False


def test_unknown_location_and_unclassified_are_visible(client, world):
    spec = ok(client, "specializations", period="30d", limit=0)
    by = {x["slug"]: x["sel"] for x in spec["rows"]}
    assert by["unclassified"] == 3 and by["backend-development"] == 3
    assert spec["totals"]["sel"] == 8 and spec["completeness"]["classified"] == 5
    cities = {x["kind"]: x["sel"] for x in ok(client, "cities", period="30d", limit=0)["rows"] if x["kind"] in ("remote", "unknown")}
    assert cities == {"remote": 1, "unknown": 1}


# --- specializations -------------------------------------------------------------------------------------------------


def test_specialization_table_columns_sorting_and_change(client, world):
    r = ok(client, "specializations", period="30d", limit=0)
    row = {x["slug"]: x for x in r["rows"]}["backend-development"]
    assert (row["sel"], row["today"], row["7d"], row["30d"], row["active"]) == (3, 1, 3, 3, 3)
    assert row["overlap"] == 4  # n2 lists it as secondary: not additive
    assert r["totals"]["sel"] == 8
    names = [x["name"] for x in ok(client, "specializations", period="30d", limit=0, sort="name", order="asc")["rows"]]
    assert names == sorted(names, key=str.lower)
    top2 = ok(client, "specializations", period="30d", limit=2)
    assert len(top2["rows"]) == 2 and top2["rest"]["rows"] == top2["total_rows"] - 2
    assert sum(x["sel"] for x in top2["rows"]) + top2["rest"]["sel"] == 8
    grp = ok(client, "specializations", period="30d", level="group", limit=0)
    assert {x["slug"]: x["sel"] for x in grp["rows"]} == {"information-technology": 5, "unclassified": 3}


def test_specialization_change_percent(db, client):
    co = make_company(db, "chg.de", name="Chg")
    for i in range(2):
        add_job(db, co, i, at=utc(2026, 5, 20, 1 + i), cities=("Berlin",), spec="backend-development")
    for i in range(4):
        add_job(db, co, 10 + i, at=utc(2026, 5, 13, 1 + i), cities=("Berlin",), spec="backend-development")
    db.commit()
    # 7 days: 14..20 May (2 jobs) versus 7..13 May (4 jobs, all before the 14 May start? the 13th belongs to the previous week)
    r = ok(client, "specializations", period="7d", limit=0)
    row = r["rows"][0]
    assert (row["sel"], row["prev"], row["change_pct"]) == (2, 4, -50.0)


def test_combined_filters(client, world):
    base = ok(client, "summary", period="30d")["new"]["total"]
    assert base == 8
    by = lambda **kw: ok(client, "summary", period="30d", **kw)["new"]["total"]  # noqa: E731
    assert by(country="DE") == 5
    assert by(country=["DE", "AT"]) == 6
    assert by(country="DE", specialization="backend-development") == 2  # n1, n6
    assert by(country="DE", specialization="information-technology") == 3  # group includes its members: n1, n2, n6
    assert by(specialization="unclassified") == 3
    assert by(specialization=["unclassified", "qa-testing"]) == 4
    assert by(country="DE", specialization="backend-development", city="Berlin") == 1
    assert by(country="CH", specialization="backend-development") == 0
    assert ok(client, "cities", period="30d", country="AT", limit=0)["totals"]["sel"] == 1
    assert ok(client, "specialization-city", period="30d", country="DE")["total"] == 5


def test_matrix_cells_add_up_and_list_top_combinations(client, world):
    r = ok(client, "specialization-city", period="30d", level="specialization", rows=20, cols=20)
    assert r["total"] == 8 and not r["empty"]
    cells = sum(sum(row) for row in r["cells"])
    assert cells + r["other"]["in_buckets"] == 8  # the remote / multi / unknown buckets are not columns
    top = r["top_combinations"][0]
    assert top["count"] == 1 and {"specialization", "place"} <= set(top)
    small = ok(client, "specialization-city", period="30d", rows=1, cols=1)
    assert len(small["rows"]) == 1 and len(small["columns"]) == 1


def test_initial_catalogue_load_is_flagged_and_can_be_excluded(db, client):
    old = make_company(db, "old.de", name="Old")
    fresh = make_company(db, "fresh.de", name="Fresh")
    add_job(db, old, 1, at=utc(2026, 4, 1), cities=("Berlin",))  # the company was crawled before
    add_job(db, old, 2, at=utc(2026, 5, 20, 6), cities=("Berlin",))
    for i in range(3):  # a company first crawled today: its whole catalogue appears at once
        add_job(db, fresh, 10 + i, at=utc(2026, 5, 20, 6, i), cities=("Berlin",))
    db.commit()
    s = ok(client, "summary", period="today")
    assert s["new"]["total"] == 4 and s["data_quality"]["initial_load"] == 3
    assert any(w["code"] == "initial_load" for w in s["data_quality"]["warnings"])
    for ep, key in (("summary", None), ("cities", "totals"), ("specializations", "totals"), ("countries", "totals")):
        r = ok(client, ep, period="today", exclude_initial_load="true")
        total = r["new"]["total"] if ep == "summary" else r[key]["sel"]
        assert total == 1, ep


# --- consistency: dashboard == Telegram ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [{}, {"country": "DE"}, {"country": ["DE", "CH"], "specialization": "backend-development"}, {"specialization": "unclassified"}, {"region": "DE-BY"}],
)
@pytest.mark.parametrize("period", ["today", "last_24h", "7d", "30d"])
def test_every_view_and_the_telegram_post_report_the_same_total(client, world, period, kw):
    q = {"period": period, **kw}
    total = ok(client, "summary", **q)["new"]["total"]
    assert ok(client, "trends", **q)["total"] == total
    assert ok(client, "specializations", limit=0, **q)["totals"]["sel"] == total
    assert ok(client, "cities", limit=0, **q)["totals"]["sel"] == total
    assert ok(client, "countries", **q)["totals"]["sel"] == total
    assert ok(client, "specialization-city", **q)["total"] == total
    body = {k: ([v] if k in ("country", "region", "city", "specialization") and not isinstance(v, list) else v) for k, v in q.items()}
    body.update(as_of=SNAP, language="en")
    rep = client.post(f"{BASE}/telegram-preview", json=body, headers=H).json()
    assert rep["data"]["new_jobs_total"] == total
    if total:
        assert f"{total:,}" in rep["text"].split("\n")[0]
        top = {r["slug"]: r["sel"] for r in ok(client, "specializations", limit=0, **q)["rows"] if r["id"]}
        for s in rep["data"]["specializations"]:
            assert top[s["slug"]] == s["count"]


def test_vacancy_table_click_through_matches_the_statistics(client, world):
    s = ok(client, "summary", period="30d", country="DE", specialization="backend-development")
    sel = dt.datetime.fromisoformat(s["period"]["start"]), dt.datetime.fromisoformat(s["period"]["counted_until"])
    r = client.get(
        "/api/jobs",
        params={"status": "all", "discovered_from": sel[0].isoformat(), "discovered_to": sel[1].isoformat(), "country_code": "DE", "specialization": "backend-development", "dach_scope": "true", "limit": 100},
    )
    assert r.status_code == 200 and r.json()["total"] == s["new"]["total"] == 2
    # unclassified + city drill-down
    r = client.get("/api/jobs", params={"status": "all", "dach_scope": "true", "specialization": "unclassified", "limit": 100})
    assert r.json()["total"] == ok(client, "summary", period="30d", specialization="unclassified")["new"]["total"] == 3
    r = client.get("/api/jobs", params={"status": "all", "place_city": "München", "country_code": "DE", "limit": 100})
    assert {j["title"] for j in r.json()["items"]} == {"Job 2", "Job 3"}


# --- empty and invalid input ---------------------------------------------------------------------------------------------


def test_empty_period_returns_zeros_and_an_empty_post(client, world):
    q = {"period": "custom", "from": "2025-01-01", "to": "2025-01-07"}
    s = ok(client, "summary", **q)
    assert s["new"]["total"] == 0 and s["new"]["companies"] == 0
    t = ok(client, "trends", **q)
    assert t["empty"] is True and len(t["points"]) == 7 and t["total"] == 0
    assert all(r["sel"] == 0 for r in ok(client, "specializations", **q)["rows"])  # rows may remain for "active"
    assert all(r["sel"] == 0 for r in ok(client, "cities", **q)["rows"])
    assert ok(client, "specialization-city", **q)["empty"] is True
    rep = client.post(f"{BASE}/telegram-preview", json={**q, "as_of": SNAP}, headers=H).json()
    assert rep["empty"] is True and "No new vacancies" in rep["text"] and "discovered!" not in rep["text"]
    assert "👉" not in rep["text"]


def test_no_data_at_all(client):
    s = ok(client, "summary", period="30d")
    assert s["new"]["total"] == 0 and s["active"] == 0 and s["data_quality"]["created_event_pct"] is None
    assert ok(client, "countries", period="30d")["empty"] is True


@pytest.mark.parametrize(
    "path,params",
    [
        ("summary", {"period": "forever"}),
        ("summary", {"period": "custom"}),
        ("summary", {"period": "custom", "from": "2026-05-10", "to": "2026-05-01"}),
        ("summary", {"period": "custom", "from": "2026-13-45", "to": "2026-05-01"}),
        ("summary", {"period": "custom", "from": "1999-01-01", "to": "1999-01-05"}),
        ("summary", {"period": "custom", "from": "2024-01-01", "to": "2026-05-01"}),
        ("summary", {"timezone": "Mars/Olympus"}),
        ("summary", {"timezone": "../../etc/passwd"}),
        ("summary", {"country": "D1"}),
        ("summary", {"country": "Germany"}),
        ("summary", {"region": "XX"}),
        ("summary", {"specialization": "no-such-thing"}),
        ("summary", {"city": "x" * 200}),
        ("summary", {"as_of": "yesterday"}),
        ("summary", {"as_of": "2999-01-01T00:00:00+00:00"}),
        ("summary", {"as_of": "2026-05-20T10:00:00"}),
        ("cities", {"mode": "weird"}),
        ("cities", {"limit": -1}),
        ("specializations", {"sort": "price"}),
        ("specialization-city", {"rows": 0}),
    ],
)
def test_invalid_input_is_rejected_with_422(client, path, params):
    r = client.get(f"{BASE}/{path}", params={"as_of": SNAP, **params} if "as_of" not in params else params)
    assert r.status_code == 422, (params, r.status_code, r.text[:200])
    assert "Traceback" not in r.text


def test_hostile_strings_are_inert(client, world):
    for evil in ["'; DROP TABLE jobs;--", "Berlin' OR '1'='1", "%", "\\", "<script>alert(1)</script>"]:
        r = get(client, "summary", period="30d", city=evil)
        assert r.status_code == 200 and r.json()["new"]["total"] == 0
    assert db_ok(client)


def db_ok(client) -> bool:
    return client.get("/api/health").json()["database"] == "ok"


def test_invalid_post_bodies(client):
    base = {"period": "today", "as_of": SNAP}
    for extra in [{"language": "fr"}, {"format": "markdown"}, {"top_specializations": 4}, {"top_cities": 100}, {"bot_link": "javascript:alert(1)"}, {"bot_link": "https://evil.example/x"}, {"bot_link": "https://t.me/ab"}, {"period": "custom"}, {"country": ["XX1"]}]:
        r = client.post(f"{BASE}/telegram-preview", json={**base, **extra}, headers=H)
        assert r.status_code == 422, (extra, r.text[:200])
    assert client.post(f"{BASE}/telegram-preview", json={**base, "surprise": 1}, headers=H).status_code == 422


# --- access -----------------------------------------------------------------------------------------------------------------


def test_admin_only(make_client, world):
    anon = make_client(login=False)
    for ep in EP:
        assert anon.get(f"{BASE}/{ep}").status_code == 401, ep
    assert anon.get(f"{BASE}/telegram-publications").status_code == 401
    assert anon.post(f"{BASE}/telegram-preview", json={}, headers=H).status_code == 401
    assert anon.post(f"{BASE}/telegram-publish", json={}, headers=H).status_code == 401
    # a bot-API style bearer token is not an admin session
    assert anon.get(f"{BASE}/summary", headers={"Authorization": "Bearer " + "t" * 40}).status_code == 401
    # unsafe methods need the CSRF header, even for the admin
    admin = make_client()
    assert admin.post(f"{BASE}/telegram-preview", json={"period": "today"}).status_code == 403
    assert admin.get(f"{BASE}/summary").status_code == 200


def test_not_exposed_through_the_public_bot_api(make_client):
    c = make_client(login=False)
    spec = c.get("/api/openapi.json").json()["paths"]
    assert not [p for p in spec if p.startswith("/api/bot/v1") and "stats" in p]
    assert c.get("/api/bot/v1/stats/jobs/summary").status_code in (401, 404)


# --- Telegram text ---------------------------------------------------------------------------------------------------------------


def preview(client, **kw):
    r = client.post(f"{BASE}/telegram-preview", json={"period": "30d", "as_of": SNAP, **kw}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_post_layout_and_real_numbers(client, world):
    p = preview(client, bot_link="https://t.me/dach_jobs_bot", top_specializations=3, top_cities=3)
    lines = p["text"].split("\n")
    assert lines[0] == "🚀 8 new job opportunities discovered!"
    assert "🇩🇪 Germany 5 · 🇦🇹 Austria 1 · 🇨🇭 Switzerland 1" in p["text"]
    assert "💼 Top specializations:" in lines and "📍 Top cities:" in lines
    assert "• Backend Development — 3" in lines
    assert "📅 Period: 2026-04-21 – 2026-05-20" in lines
    assert "🤖 Find jobs matching your experience and skills." in lines
    assert "👉 https://t.me/dach_jobs_bot" in lines
    assert lines[-1] == "Source: DACH Career Crawler — newly discovered vacancies in our database."
    assert "published" not in p["text"].lower() and "all of" not in p["text"].lower()
    assert len([ln for ln in lines if ln.startswith("• ")]) <= 6  # top 3 + top 3
    assert p["data"]["other_countries"] == 1  # the vacancy without a country is reported, not hidden
    assert "Unclassified" not in p["text"] and "Remote (no location)" not in p["text"]


@pytest.mark.parametrize("lang,head", [("uk", "🚀 Знайдено 8 нових вакансій!"), ("ru", "🚀 Найдено 8 новых вакансий!"), ("de", "🚀 8 neue Stellen entdeckt!"), ("en", "🚀 8 new job opportunities discovered!")])
def test_languages(client, world, lang, head):
    assert preview(client, language=lang)["text"].split("\n")[0] == head


def test_slavic_plurals_and_singular():
    assert statsreport._headline("uk", 1).format(n=1) == "🚀 Знайдено 1 нову вакансію!"
    assert "нові вакансії" in statsreport._headline("uk", 3)
    assert "нових вакансій" in statsreport._headline("uk", 11) and "нових вакансій" in statsreport._headline("uk", 112)
    assert "новую вакансию" in statsreport._headline("ru", 21)
    assert statsreport._headline("en", 1).format(n=1) == "🚀 1 new job opportunity discovered!"
    assert statsreport._headline("de", 1).format(n=1) == "🚀 1 neue Stelle entdeckt!"


def test_options_switch_parts_off(client, world):
    p = preview(client, include_countries=False, include_bot_link=False, bot_link="https://t.me/dach_jobs_bot")
    assert "🇩🇪" not in p["text"] and "t.me" not in p["text"]
    q = preview(client, include_bot_link=True)  # no link configured anywhere
    assert "👉" not in q["text"] and any("bot link" in w for w in q["warnings"])


def test_server_side_bot_link_is_used_and_invalid_one_ignored(make_client, world):
    c = make_client(CrawlerSettings(stats_telegram_bot_link="https://t.me/server_bot"))
    assert "👉 https://t.me/server_bot" in preview(c)["text"]
    c2 = make_client(CrawlerSettings(stats_telegram_bot_link="javascript:alert(1)"))
    p = preview(c2)
    assert "javascript" not in p["text"] and "👉" not in p["text"]


def test_escaping_in_all_formats(db, client):
    ev = GeoLocation(loc_key="DE|DE-SN|a_b*c[d](e)<x>&y", country_code="DE", region_code="DE-SN", city="A_B*C[D](E) <x> & y", search_name="a_b")
    db.add(ev)
    db.commit()
    try:
        co = make_company(db, "esc.de", name="Esc")
        add_job(db, co, 1, at=utc(2026, 5, 20, 6), cities=("A_B*C[D](E) <x> & y",), spec="qa-testing")
        db.commit()
        plain = preview(client, period="today", format="plain")["text"]
        assert "A_B*C[D](E) <x> & y (DE) — 1" in plain and "&amp;" not in plain
        html_ = preview(client, period="today", format="html")["text"]
        assert "A_B*C[D](E) &lt;x&gt; &amp; y (DE)" in html_ and "<x>" not in html_
        assert "QA &amp; Test Engineering" in html_ and html_.count("<b>") == html_.count("</b>")
        md = preview(client, period="today", format="markdownv2", bot_link="https://t.me/dach_jobs_bot")["text"]
        assert "A\\_B\\*C\\[D\\]\\(E\\) <x\\> & y \\(DE\\)" in md
        assert "[t\\.me/dach\\_jobs\\_bot](https://t.me/dach_jobs_bot)" in md
    finally:
        db.execute(text("DELETE FROM job_locations"))
        db.execute(text("DELETE FROM jobs"))
        db.execute(text("DELETE FROM geo_locations WHERE id = :i"), {"i": ev.id})
        db.commit()


def test_escape_helpers():
    assert statsreport.esc("a<b>&c", "html") == "a&lt;b&gt;&amp;c"
    assert statsreport.esc("1.5 (x) - y! _z_ [w]", "markdownv2") == "1\\.5 \\(x\\) \\- y\\! \\_z\\_ \\[w\\]"
    assert statsreport.esc("<b>", "plain") == "<b>"
    for ch in "_*[]()~`>#+-=|{}.!\\":
        assert statsreport.esc(ch, "markdownv2") == "\\" + ch


def test_post_never_exceeds_the_telegram_limit(client, world):
    assert preview(client, top_specializations=10, top_cities=10)["length"] < statsreport.MAX_MESSAGE


# --- Phase 4: publishing --------------------------------------------------------------------------------------------------------


TOKEN = "123456:SECRET-token-value-abcdefghijklmnop"


def pub_settings(**kw) -> CrawlerSettings:
    base = dict(stats_telegram_enabled=True, stats_telegram_bot_token=TOKEN, stats_telegram_chat_id="@dach_jobs", stats_telegram_min_interval_s=0)
    base.update(kw)
    return CrawlerSettings(**base)


class FakeTelegram:
    def __init__(self, replies=None):
        import httpx

        self.calls: list[dict] = []
        self.replies = list(replies or [])
        self.httpx = httpx

    def client(self, settings):
        httpx = self.httpx

        def handler(request):
            import json

            self.calls.append({"url": str(request.url), "json": json.loads(request.content)})
            if self.replies:
                r = self.replies.pop(0)
                if isinstance(r, Exception):
                    raise r
                return httpx.Response(r[0], json=r[1])
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 77 + len(self.calls)}})

        return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def tg(monkeypatch):
    f = FakeTelegram()
    monkeypatch.setattr(statspublish, "make_client", f.client)
    return f


def publish(c, p, **kw):
    body = {"period": "30d", "as_of": SNAP, "confirm": True, "approved_sha256": p["sha256"], **kw}
    return c.post(f"{BASE}/telegram-publish", json=body, headers=H)


def test_publishing_is_off_by_default(client, world, tg):
    p = preview(client)
    assert p["telegram"] == {"enabled": False, "configured": False, "post_empty": False, "bot_link": None}
    r = publish(client, p)
    assert r.status_code == 403 and tg.calls == []


def test_enabled_without_credentials_is_refused(make_client, world, tg):
    c = make_client(CrawlerSettings(stats_telegram_enabled=True))
    assert publish(c, preview(c)).status_code == 409 and tg.calls == []


def test_publish_requires_confirm_and_the_approved_text(make_client, world, tg):
    c = make_client(pub_settings())
    p = preview(c)
    r = c.post(f"{BASE}/telegram-publish", json={"period": "30d", "as_of": SNAP, "approved_sha256": p["sha256"]}, headers=H)
    assert r.status_code == 422 and tg.calls == []
    r = publish(c, p, approved_sha256="0" * 64)  # stale / tampered preview
    assert r.status_code == 409 and "changed" in r.text and tg.calls == []
    r = publish(c, p, language="de")  # different options than the approved preview
    assert r.status_code == 409 and tg.calls == []


def test_publish_sends_once_and_records_history(make_client, world, tg, db):
    c = make_client(pub_settings())
    p = preview(c)
    r = publish(c, p)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "sent" and out["message_id"] == 78 and out["text"] == p["text"]
    assert len(tg.calls) == 1
    call = tg.calls[0]
    assert call["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert call["json"]["chat_id"] == "@dach_jobs" and call["json"]["text"] == p["text"] and "parse_mode" not in call["json"]
    # repeated publishing does not duplicate
    r2 = publish(c, p)
    assert r2.status_code == 409 and "already published" in r2.text
    assert len(tg.calls) == 1 and db.query(StatPublication).count() == 1
    hist = c.get(f"{BASE}/telegram-publications").json()["items"]
    assert len(hist) == 1 and hist[0]["status"] == "sent" and hist[0]["created_by"] == "admin"


def test_token_never_leaks(make_client, world, tg, db):
    c = make_client(pub_settings())
    p = preview(c)
    responses = [
        preview(c).__repr__(),
        publish(c, p).text,
        c.get(f"{BASE}/telegram-publications").text,
        c.get(f"{BASE}/config").text,
        c.get("/api/openapi.json").text,
    ]
    stored = db.execute(text("SELECT row_to_json(s)::text FROM stat_publications s")).scalars().all()
    for blob in [*responses, *stored]:
        assert TOKEN not in blob and "SECRET-token" not in blob
    assert c.get(f"{BASE}/config").json()["telegram"] == {"enabled": True, "configured": True, "post_empty": False, "bot_link": None}


def test_failed_publish_is_logged_scrubbed_and_can_be_retried(make_client, world, monkeypatch, db):
    import httpx

    f = FakeTelegram([httpx.ConnectError(f"cannot reach https://api.telegram.org/bot{TOKEN}/sendMessage"), (400, {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"})])
    monkeypatch.setattr(statspublish, "make_client", f.client)
    c = make_client(pub_settings())
    p = preview(c, format="html")
    r = publish(c, p, format="html")
    assert r.status_code == 502 and TOKEN not in r.text
    row = db.query(StatPublication).one()
    assert row.status == "failed" and row.attempts == 1 and TOKEN not in (row.error or "") and "***" in row.error
    r = publish(c, p, format="html")  # second failure: Telegram's own answer is stored
    db.expire_all()
    assert r.status_code == 502 and "can't parse entities" in r.text
    assert db.query(StatPublication).one().attempts == 2 and db.query(StatPublication).one().response["error_code"] == 400
    r = publish(c, p, format="html")  # the next attempt succeeds and reuses the same record
    db.expire_all()
    assert r.status_code == 200 and db.query(StatPublication).count() == 1 and db.query(StatPublication).one().status == "sent"
    assert f.calls[-1]["json"]["parse_mode"] == "HTML"


def test_rate_limit_from_telegram_is_reported_not_retried(make_client, world, monkeypatch):
    f = FakeTelegram([(429, {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 17", "parameters": {"retry_after": 17}})])
    monkeypatch.setattr(statspublish, "make_client", f.client)
    c = make_client(pub_settings())
    r = publish(c, preview(c))
    assert r.status_code == 429 and r.json()["detail"]["retry_after"] == 17 and len(f.calls) == 1


def test_minimum_interval_between_attempts(make_client, world, tg):
    c = make_client(pub_settings(stats_telegram_min_interval_s=60))
    assert publish(c, preview(c)).status_code == 200
    p2 = preview(c, language="de")
    r = publish(c, p2, language="de")
    assert r.status_code == 429 and len(tg.calls) == 1


def test_other_configuration_or_period_is_a_new_publication(make_client, world, tg):
    c = make_client(pub_settings())
    assert publish(c, preview(c)).status_code == 200
    assert publish(c, preview(c, language="de"), language="de").status_code == 200
    kw = dict(period="7d")
    assert publish(c, preview(c, **kw), **kw).status_code == 200
    assert len(tg.calls) == 3


def test_in_flight_publication_blocks_a_duplicate(make_client, world, tg, db):
    c = make_client(pub_settings())
    p = preview(c)
    assert publish(c, p).status_code == 200
    db.execute(text("UPDATE stat_publications SET status = 'pending', updated_at = now()"))
    db.commit()
    r = publish(c, p)
    assert r.status_code == 409 and "right now" in r.text and len(tg.calls) == 1


def test_empty_reports_are_not_posted_unless_enabled(make_client, tg):
    q = {"period": "custom", "from": "2025-01-01", "to": "2025-01-02"}
    c = make_client(pub_settings())
    p = preview(c, **q)
    assert p["empty"] is True
    r = publish(c, p, **q)
    assert r.status_code == 409 and tg.calls == []
    c2 = make_client(pub_settings(stats_telegram_post_empty=True))
    assert publish(c2, preview(c2, **q), **q).status_code == 200


def test_settings_defaults_keep_everything_off():
    s = CrawlerSettings()
    assert s.stats_telegram_enabled is False and s.stats_telegram_bot_token is None and s.stats_timezone == "Europe/Berlin"
    assert statspublish.status(s) == {"enabled": False, "configured": False, "post_empty": False, "bot_link": None}


def test_stats_period_resolution_unit():
    snap = dt.datetime(2026, 5, 20, 10, tzinfo=dt.UTC)
    p = jobstats.resolve_period("30d", "Europe/Berlin", snap)
    assert p.local(p.start).date().isoformat() == "2026-04-21" and p.display() == ("2026-04-21", "2026-05-20")
    with pytest.raises(jobstats.StatsError):
        jobstats.resolve_period("custom", "Europe/Berlin", snap, "2026-05-02", "2026-05-01")
