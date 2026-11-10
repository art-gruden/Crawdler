"""Bot API: authentication, read-only scope, search semantics, facets, pagination (fixtures only)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gcc_core import botauth

from .dach_helpers import BOT_TOKEN, company, ingest, raw

B = "/api/bot/v1"
AUTH = {"Authorization": f"Bearer {BOT_TOKEN}"}
H = {"X-Requested-With": "gcc-ui"}


@pytest.fixture(autouse=True)
def _keys(settings, monkeypatch):
    monkeypatch.setattr(settings, "bot_api_keys", f"telegram:{BOT_TOKEN}")
    monkeypatch.setattr(settings, "bot_rate_limit_per_min", 1000)
    monkeypatch.setattr(settings, "bot_auth_fail_limit_per_min", 1000)
    botauth.limiter._mem.clear()
    botauth._load.cache_clear()
    yield
    botauth._load.cache_clear()


@pytest.fixture
def client():
    from gcc_core.app import create_app

    return TestClient(create_app())


@pytest.fixture
def data(db):
    de = company(db, "a-firma.de", "A GmbH", "DE")
    ch = company(db, "b-ag.ch", "B AG", "CH")
    at = company(db, "c-gmbh.at", "C GmbH", "AT")
    ids = {}
    ids["dresden"] = ingest(
        db, de, [raw("Senior Python Developer (m/w/d)", "a-firma.de", 1, location_raw="Dresden, Sachsen",
                     description_text="Python, Django, PostgreSQL. Deutsch C1 erforderlich, Englisch B2. 60.000 - 75.000 € pro Jahr. Vollzeit. Hybrid.")]
    )[0]
    ids["berlin"] = ingest(
        db, de, [raw("Frontend Entwickler React", "a-firma.de", 2, location_raw="Berlin",
                     description_text="React, TypeScript. Deutsch B1. Vollzeit. 100% remote in Deutschland.")]
    )[0]
    ids["zurich"] = ingest(
        db, ch, [raw("Software Engineer Kubernetes", "b-ag.ch", 1, location_raw="Zürich, Schweiz",
                     description_text="Python, Kubernetes, Docker. English C1. CHF 120'000 - 140'000 per year.")]
    )[0]
    ids["wien"] = ingest(
        db, at, [raw("Projektmanager", "c-gmbh.at", 1, location_raw="Wien", description_text="Spannende Aufgaben. Teilzeit.")]
    )[0]
    return ids


def get(client, path, **params):
    return client.get(B + path, params=params, headers=AUTH)


def titles(r):
    assert r.status_code == 200, r.text
    return [i["title"] for i in r.json()["items"]]


# --- authentication -------------------------------------------------------------------------------


def test_requires_a_token(client, data):
    for path in ("/jobs", f"/jobs/{data['wien']}", "/facets/countries", "/changes", "/changes/head"):
        r = client.get(B + path)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        assert client.get(B + path, headers={"Authorization": "Bearer nope"}).status_code == 401
        assert client.get(B + path, headers={"Authorization": f"Basic {BOT_TOKEN}"}).status_code == 401


def test_token_is_read_only(client, data):
    for method in ("post", "put", "patch", "delete"):
        r = getattr(client, method)(B + "/jobs", headers=AUTH)
        assert r.status_code == 405


def test_admin_session_does_not_open_the_bot_api_and_token_not_admin(client, data):
    assert client.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"}, headers=H).status_code == 200
    assert client.get(B + "/jobs").status_code == 401  # cookie only
    anon = TestClient(client.app)
    assert anon.get("/api/jobs", headers=AUTH).status_code == 401  # bot token on an admin route
    assert anon.get("/api/crawler/status", headers=AUTH).status_code in (401, 404)


def test_disabled_and_misconfigured(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "bot_api_keys", "")
    botauth._load.cache_clear()
    assert client.get(B + "/jobs", headers=AUTH).status_code == 503
    monkeypatch.setattr(settings, "bot_api_keys", "telegram:short")
    botauth._load.cache_clear()
    r = client.get(B + "/jobs", headers=AUTH)
    assert r.status_code == 503 and "short" not in r.text


def test_rotation_accepts_two_keys_and_digest_form(client, settings, monkeypatch):
    import hashlib

    new = "n" * 40
    digest = hashlib.sha256(new.encode()).hexdigest()
    monkeypatch.setattr(settings, "bot_api_keys", f"old:{BOT_TOKEN},new:sha256:{digest}")
    botauth._load.cache_clear()
    assert client.get(B + "/facets/countries", headers=AUTH).status_code == 200
    assert client.get(B + "/facets/countries", headers={"Authorization": f"Bearer {new}"}).status_code == 200
    monkeypatch.setattr(settings, "bot_api_keys", f"new:sha256:{digest}")
    botauth._load.cache_clear()
    assert client.get(B + "/facets/countries", headers=AUTH).status_code == 401


def test_rate_limit_and_failed_attempt_limit(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "bot_rate_limit_per_min", 3)
    codes = [get(client, "/facets/countries").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]
    r = get(client, "/facets/countries")
    assert "retry-after" in r.headers
    botauth.limiter._mem.clear()
    monkeypatch.setattr(settings, "bot_auth_fail_limit_per_min", 2)
    bad = [client.get(B + "/jobs", headers={"Authorization": "Bearer x"}).status_code for _ in range(4)]
    assert bad == [401, 401, 429, 429]


def test_token_never_logged(client, data, capsys):
    client.get(B + "/jobs", headers={"Authorization": "Bearer leaky-token-value-1234567890abcdef"})
    get(client, "/jobs")
    out = capsys.readouterr()
    assert "leaky-token-value" not in out.out + out.err and BOT_TOKEN not in out.out + out.err


# --- search ------------------------------------------------------------------------------------------------


def test_geography_filters_and_facets(client, data):
    assert titles(get(client, "/jobs", country="CH")) == ["Software Engineer Kubernetes"]
    assert sorted(titles(get(client, "/jobs", country="DE"))) == ["Frontend Entwickler React", "Senior Python Developer (m/w/d)"]
    assert titles(get(client, "/jobs", region="DE-SN")) == ["Senior Python Developer (m/w/d)"]
    assert len(titles(get(client, "/jobs", country="DE,AT"))) == 3
    countries = {c["code"]: c for c in get(client, "/facets/countries").json()}
    assert countries["DE"]["open_jobs"] == 2 and countries["AT"]["open_jobs"] == 1 and countries["CH"]["open_jobs"] == 1
    regions = {r["code"]: r["open_jobs"] for r in get(client, "/facets/regions", country="DE").json()}
    assert len(regions) == 16 and regions["DE-SN"] == 1 and regions["DE-BE"] == 1
    assert len(get(client, "/facets/regions", country="AT").json()) == 9
    assert len(get(client, "/facets/regions", country="CH").json()) == 26
    cities = get(client, "/facets/cities", country="DE", region="DE-SN").json()
    assert [c["city"] for c in cities] == ["Dresden"]
    assert get(client, "/facets/cities", country="CH", q="zuer").json()[0]["city"] == "Zürich"  # ue == ü
    city_id = cities[0]["id"]
    assert titles(get(client, "/jobs", city=city_id)) == ["Senior Python Developer (m/w/d)"]
    assert get(client, "/facets/regions", country="XX").status_code == 422


def test_filters_or_within_and_across(client, data):
    assert len(titles(get(client, "/jobs", country="DE", work_model="HYBRID,REMOTE"))) == 2
    assert titles(get(client, "/jobs", country="DE", work_model="REMOTE")) == ["Frontend Entwickler React"]
    assert titles(get(client, "/jobs", skills="Python", skills_mode="all")) == [
        "Software Engineer Kubernetes", "Senior Python Developer (m/w/d)"
    ]
    assert titles(get(client, "/jobs", skills="python,kubernetes", skills_mode="all")) == ["Software Engineer Kubernetes"]
    assert get(client, "/jobs", skills="NoSuchSkill").status_code == 422
    assert titles(get(client, "/jobs", employment="PART_TIME")) == ["Projektmanager"]
    assert titles(get(client, "/jobs", seniority="SENIOR")) == ["Senior Python Developer (m/w/d)"]


def test_unknown_values_are_explicit(client, data):
    # Wien/Projektmanager has no salary, no seniority: unknown never matches a filter by accident
    assert "Projektmanager" not in titles(get(client, "/jobs", seniority="SENIOR"))
    got = titles(get(client, "/jobs", seniority="SENIOR", include_unknown="seniority"))
    assert "Projektmanager" in got and "Senior Python Developer (m/w/d)" in got
    assert get(client, "/jobs", include_unknown="bogus").status_code == 422


def test_salary_needs_a_currency_and_never_converts(client, data):
    assert get(client, "/jobs", salary_min=50000).status_code == 422
    assert titles(get(client, "/jobs", salary_min=100000, currency="CHF")) == ["Software Engineer Kubernetes"]
    assert titles(get(client, "/jobs", salary_min=50000, currency="EUR")) == ["Senior Python Developer (m/w/d)"]
    assert titles(get(client, "/jobs", salary_min=1, currency="USD")) == []
    assert titles(get(client, "/jobs", sort="salary_desc", currency="EUR")) == ["Senior Python Developer (m/w/d)"]
    assert get(client, "/jobs", sort="salary_desc").status_code == 422


def test_text_search_exclusion_and_relevance(client, data):
    assert titles(get(client, "/jobs", q="python")) == ["Software Engineer Kubernetes", "Senior Python Developer (m/w/d)"] or len(
        titles(get(client, "/jobs", q="python"))
    ) == 2
    assert titles(get(client, "/jobs", q="python", exclude="kubernetes")) == ["Senior Python Developer (m/w/d)"]
    assert titles(get(client, "/jobs", q="python -kubernetes")) == ["Senior Python Developer (m/w/d)"]
    r = get(client, "/jobs", q="python", sort="relevance", skills="Python")
    assert r.status_code == 200 and all(i["relevance"] is not None for i in r.json()["items"])
    assert get(client, "/jobs", q="python").json()["items"][0]["relevance"] is None


def test_language_fit(client, data):
    r = get(client, "/jobs", languages="de:C1,en:B2", lang_mode="strict", country="DE")
    assert sorted(titles(r)) == ["Frontend Entwickler React", "Senior Python Developer (m/w/d)"]
    assert {i["language_fit"] for i in r.json()["items"]} == {"compatible"}
    low = get(client, "/jobs", languages="de:B1,en:B1", country="DE")
    assert "Senior Python Developer (m/w/d)" not in titles(low)  # German C1 exceeds B1
    assert "Frontend Entwickler React" in titles(low)
    assert get(client, "/jobs", languages="de:Z9").status_code == 422


def test_status_and_new_since(client, data):
    assert len(titles(get(client, "/jobs"))) == 4
    assert titles(get(client, "/jobs", new_since="2999-01-01T00:00:00Z")) == []
    assert len(titles(get(client, "/jobs", new_since="2000-01-01T00:00:00Z"))) == 4
    assert titles(get(client, "/jobs", status="closed")) == []


def test_cursor_pagination_is_stable_and_complete(client, data, db):
    from career_crawler import store

    co = company(db, "bulk.de", "Bulk GmbH", "DE")
    ingest(db, co, [raw(f"Developer {i}", "bulk.de", i, location_raw="Leipzig, Sachsen") for i in range(1, 8)])
    for sort in ("newest", "relevance"):
        seen: list[int] = []
        cursor = None
        for _ in range(20):
            params = dict(limit=3, sort=sort, q="developer")
            if cursor:
                params["cursor"] = cursor
            body = get(client, "/jobs", **params).json()
            seen += [i["id"] for i in body["items"]]
            if not body["has_more"]:
                assert body["next_cursor"] is None
                break
            cursor = body["next_cursor"]
        assert len(seen) == len(set(seen)) == 8, sort  # 7 new + the Dresden "Developer"
    # a cursor from another query is rejected, a garbled one too
    body = get(client, "/jobs", limit=2).json()
    assert get(client, "/jobs", limit=2, country="CH", cursor=body["next_cursor"]).status_code == 422
    assert get(client, "/jobs", cursor="garbage").status_code == 422
    assert get(client, "/jobs", limit=101).status_code == 422
    assert store  # imported for side effects


def test_job_detail_and_public_schema_hides_internals(client, data):
    r = get(client, f"/jobs/{data['dresden']}")
    assert r.status_code == 200
    body = r.json()
    assert body["description"] and body["company"]["name"] == "A GmbH"
    assert body["locations"][0]["region"] == "DE-SN" and body["locations"][0]["country_name"] == "Germany"
    assert body["salary"]["currency"] == "EUR" and body["salary"]["basis"] in ("gross", "unknown")
    assert {x["language"]: x["level"] for x in body["languages"]}["de"] == "C1"
    assert body["work_model"] == "HYBRID" and body["seniority"] == "SENIOR"
    blob = r.text
    for secret in ("dedup_key", "extraction_method", "miss_count", "content_hash", "source_values", "description_hash"):
        assert secret not in blob
    assert get(client, "/jobs/999999").status_code == 404
    assert "extraction" not in body["quality"]["field_confidence"]


def test_facets_for_specializations_and_skills(client, data):
    specs = get(client, "/facets/specializations").json()
    it = next(s for s in specs if s["slug"] == "information-technology")
    assert it["open_jobs"] >= 3 and it["children"]
    skills = {s["name"]: s["open_jobs"] for s in get(client, "/facets/skills").json()}
    assert skills["Python"] == 2 and "Kubernetes" in skills
    assert titles(get(client, "/jobs", specialization="information-technology")) != []
    assert get(client, "/jobs", specialization="nonsense").status_code == 422


def test_openapi_documents_the_bot_api(client):
    spec = client.get("/openapi.json")
    paths = spec.json()["paths"] if spec.status_code == 200 else client.app.openapi()["paths"]
    assert f"{B}/changes" in paths and f"{B}/jobs" in paths
