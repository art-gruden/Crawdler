import httpx
import pytest

from company_discovery.industries import classify_nace, classify_wfs, get_industries
from company_discovery.overpass import (
    OverpassBudgetExceeded,
    OverpassClient,
    OverpassError,
    OverpassQueryError,
    build_query,
    parse_response,
)
from company_discovery.regions import expand_leaves, find_region_by_name, get_region
from gcc_core.db import SessionLocal

from .conftest import load_fixture


def test_sachsen_has_13_kreise():
    leaves = expand_leaves(["DE-SN"])
    assert len(leaves) == 13
    assert {r.code for r in leaves} >= {"14612", "14713", "14511", "14628"}
    assert sum(r.kind == "kreisfreie_stadt" for r in leaves) == 3
    # state + one of its Kreise does not duplicate
    assert len(expand_leaves(["DE-SN", "14612"])) == 13
    assert [r.code for r in expand_leaves(["14612", "14713"])] == ["14612", "14713"]


@pytest.mark.parametrize(
    "name, code",
    [
        ("Dresden", "14612"),
        ("Landkreis Leipzig", "14729"),
        ("Leipzig", "14713"),
        ("Stadt Leipzig", "14713"),
        ("Kreis Bautzen", "14625"),
        ("Sächsische Schweiz-Osterzgebirge", "14628"),
        ("Landkreis Meissen", "14627"),
        ("Vogtland", "14523"),
        ("Sachsen", "DE-SN"),
    ],
)
def test_find_region_by_name(name, code):
    assert find_region_by_name(name).code == code


def test_query_contains_region_and_selectors():
    q = build_query(get_region("14612"), get_industries(["it", "industrial"]), timeout_s=180, maxsize_mb=256)
    assert q.startswith("[out:json][timeout:180][maxsize:268435456];")
    assert '["de:regionalschluessel"~"^14612"]' in q
    assert '["de:amtlicher_gemeindeschluessel"~"^14612"]' in q
    assert '["office"~"^(it|software|telecommunication)$"]["name"][~"^(website|contact:website)$"~"."](area.a);' in q
    assert '["man_made"="works"]' in q
    assert '["industrial"]["name"]' in q
    assert q.rstrip().endswith("out tags center;")
    assert ".a out ids;" in q


def test_state_query_uses_iso_code():
    assert 'area["ISO3166-2"="DE-SN"]["admin_level"="4"]->.a;' in get_region("DE-SN").overpass_area()


def test_parse_dresden_fixture():
    res = parse_response(load_fixture("overpass_dresden.json"), get_industries(None))
    assert res.area_ids == [3600191645]
    assert res.osm_base.isoformat() == "2026-11-08T21:14:03+00:00"
    names = [c.name for c in res.companies]
    assert "Elbtal Cloud Systems GmbH" in names
    assert len(res.companies) == 7  # the element without a name is skipped
    assert res.skipped == 1
    elbtal = next(c for c in res.companies if c.osm_id == 9100000001)
    assert "phone" not in elbtal.tags and "email" not in elbtal.tags  # data minimisation
    assert elbtal.industries == ["it"]
    works = next(c for c in res.companies if c.osm_type == "way")
    assert works.lat == 51.12 and works.lon == 13.78
    assert works.industries == ["industrial"]
    neustadt = next(c for c in res.companies if c.osm_id == 9100000002)
    assert neustadt.websites == ["neustadt-software.de"]
    institute = next(c for c in res.companies if c.osm_type == "relation")
    assert institute.industries == ["technology"]
    assert institute.url == "https://www.openstreetmap.org/relation/9300000001"


def test_parse_runtime_error_remark():
    with pytest.raises(OverpassQueryError):
        parse_response({"elements": [], "remark": 'runtime error: Query timed out in "query" at line 4'}, [])


def test_nace_and_wfs_mapping():
    assert classify_nace("62.01") == ["it"]
    assert classify_nace("J63.11") == ["it"]
    assert classify_nace("WZ 28.41") == ["industrial", "engineering"]
    assert classify_nace("10.71") == []
    assert classify_wfs("MASCHINE!, MICRO!")[0] == ["industrial", "engineering", "it", "technology"]
    assert classify_wfs("life sciences")[0] == ["life_sciences", "technology"]
    assert classify_wfs("ENERGIE!") == ([], ["ENERGIE!"])


# ------------------------------------------------------------------ client


class FakeOverpass:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[httpx.Request] = []
        self.status_text = "Connected as: 1\nRate limit: 2\n1 slots available now.\n"

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return httpx.Response(200, text=self.status_text)
        self.calls.append(request)
        status, body = self.responses.pop(0)
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)


def make_client(settings, fake, **overrides):
    s = settings.model_copy(update=overrides)
    sleeps: list[float] = []
    client = OverpassClient(
        s, SessionLocal, http=httpx.Client(transport=httpx.MockTransport(fake.handler)), sleep=sleeps.append
    )
    return client, sleeps


def test_client_caches_and_counts(settings, db):
    fake = FakeOverpass([(200, load_fixture("overpass_dresden.json"))])
    client, _ = make_client(settings, fake)
    payload, meta = client.run("[out:json];node(1);out;")
    assert not meta.from_cache and meta.attempts == 1 and meta.bytes > 100
    assert fake.calls[0].headers["user-agent"].startswith("GermanyCareerCrawler/")
    assert b"data=" in fake.calls[0].content
    payload2, meta2 = client.run("[out:json];node(1);out;")
    assert meta2.from_cache and payload2 == payload
    assert len(fake.calls) == 1
    usage = client.usage_today(db)
    assert usage["requests"] == 1 and usage["bytes"] == meta.bytes


def test_client_429_uses_status_endpoint(settings):
    fake = FakeOverpass([(429, "Too Many Requests"), (200, {"elements": []})])
    fake.status_text = "Rate limit: 2\nSlot available after: 2026-11-09T10:00:07Z, in 7 seconds.\n"
    client, sleeps = make_client(settings, fake)
    _, meta = client.run("q1")
    assert meta.statuses == [429, 200]
    assert sum(sleeps) == pytest.approx(12, abs=0.5)  # 7 s + 5 s margin


def test_client_504_backoff_then_gives_up(settings):
    fake = FakeOverpass([(504, "Gateway Timeout")] * 3)
    client, sleeps = make_client(settings, fake, overpass_max_retries=2)
    with pytest.raises(OverpassError, match="after 3 attempts"):
        client.run("q2")
    assert sum(sleeps) == pytest.approx(60 + 120, abs=1)


def test_client_budget(settings):
    fake = FakeOverpass([(200, {"elements": []})] * 3)
    client, _ = make_client(settings, fake, overpass_max_requests_per_day=2)
    client.run("a")
    client.run("b")
    with pytest.raises(OverpassBudgetExceeded):
        client.run("c")
    assert len(fake.calls) == 2


def test_client_400_reports_overpass_error(settings):
    body = '<p><strong style="color:#FF0000">Error</strong>: line 1: parse error: Unknown type "nwrx" </p>'
    fake = FakeOverpass([(400, body)])
    client, _ = make_client(settings, fake)
    with pytest.raises(OverpassQueryError, match="Unknown type"):
        client.run("bad")


def test_client_min_interval(settings, db):
    fake = FakeOverpass([(200, {"elements": []})] * 2)
    client, sleeps = make_client(settings, fake, overpass_min_interval_s=30)
    client.run("x1")
    client.run("x2")
    assert sleeps and 25 < sum(sleeps) <= 30
