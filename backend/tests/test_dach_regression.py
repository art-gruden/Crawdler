"""Backward compatibility, legacy backfill (Sachsen data survives) and data-quality invariants."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from career_crawler.backfill import backfill_jobs, run_all
from career_crawler.models import Job, JobSkill
from gcc_core.db import get_engine

from .crawler_helpers import make_company
from .dach_helpers import company, ingest, raw
from .test_job_export import add_job

H = {"X-Requested-With": "gcc-ui"}


@pytest.fixture
def client():
    from gcc_core.app import create_app

    c = TestClient(create_app())
    assert c.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"}, headers=H).status_code == 200
    return c


def legacy_job(db, c, n, **kw):
    j = add_job(db, c, n, **kw)
    db.execute(text("UPDATE jobs SET norm_version = 1 WHERE id = :i"), {"i": j.id})
    db.commit()
    return j


def test_legacy_sachsen_rows_are_backfilled_without_events_or_duplicates(db):
    c = make_company(db, "alt-sachsen.de", name="Alt GmbH")
    a = legacy_job(db, c, 1, title="Kubernetes Engineer", city="Dresden", country="DE", salary_min=50000, salary_max=60000,
                   salary_currency="EUR", salary_period="year", salary_raw="50.000 - 60.000 € pro Jahr")
    b = legacy_job(db, c, 2, title="Altes Projekt", city="Wilsdruff", country=None)
    db.add(JobSkill(job_id=a.id, skill="Kubernetes", category="devops"))
    db.commit()
    with get_engine().begin() as conn:
        n1 = backfill_jobs(conn)
        n2 = backfill_jobs(conn)
        counts_before = conn.execute(text("SELECT (SELECT count(*) FROM geo_locations), (SELECT count(*) FROM job_locations)")).one()
        run_all(conn)
        counts_after = conn.execute(text("SELECT (SELECT count(*) FROM geo_locations), (SELECT count(*) FROM job_locations)")).one()
    assert n1 >= 2 and n2 == 0  # second pass finds nothing left to do
    assert counts_before == counts_after  # idempotent: no duplicate locations
    db.expire_all()
    ja, jb = db.get(Job, a.id), db.get(Job, b.id)
    assert ja.country_codes == ["DE"] and ja.geo_status in ("resolved", "partial") and ja.norm_version >= 2
    assert ja.title == "Kubernetes Engineer" and ja.salary_min == 50000 and ja.salary_currency == "EUR"  # untouched data
    # a place with no country evidence stays honest about it
    assert jb.country_codes in ([], None) or "DE" not in jb.country_codes or jb.geo_status != "resolved"
    assert db.execute(text("SELECT count(*) FROM job_events")).scalar() == 0  # backfill is silent


def test_legacy_eur_without_evidence_is_not_kept_as_a_fact(db):
    c = make_company(db, "chf.ch", name="CHF AG")
    j = legacy_job(db, c, 1, title="Engineer", city="Zürich", salary_min=100000, salary_max=120000, salary_currency="EUR",
                   salary_period="year", salary_raw="CHF 100'000 - 120'000")
    with get_engine().begin() as conn:
        backfill_jobs(conn)
    db.expire_all()
    assert db.get(Job, j.id).salary_currency == "CHF"


def test_old_jobs_api_contract_is_preserved_and_new_filters_are_additive(client, db):
    de = company(db, "a-firma.de", "A GmbH", "DE")
    ch = company(db, "b-ag.ch", "B AG", "CH")
    ingest(db, de, [raw("Senior Python Developer", "a-firma.de", 1, location_raw="Dresden, Sachsen", description_text="Python. Vollzeit. Hybrid.")])
    ingest(db, ch, [raw("Software Engineer", "b-ag.ch", 1, location_raw="Zürich, Schweiz", description_text="Kubernetes.")])
    body = client.get("/api/jobs").json()
    assert body["total"] == 2
    item = body["items"][0]
    for k in ("id", "company_name", "title", "city", "country", "remote_type", "employment_types", "salary_min", "skills", "status"):
        assert k in item
    assert {"country_codes", "region_codes", "work_model", "seniority"} <= set(item)
    assert client.get("/api/jobs", params={"country_code": "CH"}).json()["total"] == 1
    assert client.get("/api/jobs", params={"region": "DE-SN"}).json()["items"][0]["title"] == "Senior Python Developer"
    assert client.get("/api/jobs", params={"work_model": "HYBRID,"}).status_code == 200
    assert client.get("/api/jobs", params={"specialization": "bogus"}).status_code == 422
    assert client.get("/api/jobs", params={"specialization": "information-technology"}).json()["total"] >= 1
    # exports keep working with the same filters
    csv = client.get("/api/jobs/export.csv", params={"country_code": "CH"})
    assert csv.status_code == 200 and "Software Engineer" in csv.text and "Senior Python" not in csv.text
    assert client.get("/api/jobs/skills").status_code == 200


def test_admin_routes_still_require_login(db):
    from gcc_core.app import create_app

    anon = TestClient(create_app())
    assert anon.get("/api/jobs").status_code == 401
    assert anon.get("/api/jobs/export.csv").status_code == 401


def test_public_bot_page_is_still_public():
    from gcc_core.app import create_app

    r = TestClient(create_app()).get("/api/public/bot")
    assert r.status_code == 200


def test_reference_data_is_complete():
    with get_engine().connect() as conn:
        by_country = dict(conn.execute(text("SELECT country_code, count(*) FROM geo_regions GROUP BY 1")).all())
        specs = conn.execute(text("SELECT count(*) FROM specializations")).scalar()
        dup = conn.execute(text("SELECT count(*) FROM (SELECT loc_key FROM geo_locations GROUP BY 1 HAVING count(*) > 1) x")).scalar()
    assert by_country["DE"] == 16 and by_country["AT"] == 9 and by_country["CH"] == 26
    assert specs >= 50 and dup == 0


def test_data_quality_invariants_after_ingest(db):
    de = company(db, "q.de", "Q GmbH", "DE")
    ingest(db, de, [raw(f"Entwickler {i}", "q.de", i, location_raw=loc, description_text="Python Entwickler. 60.000 - 70.000 € pro Jahr.")
                    for i, loc in enumerate(["Berlin", "Wien", "Zürich", "Wilsdruff", "Rheinfelden"], 1)])
    rows = db.execute(text("SELECT geo_status, country_codes, salary_min, salary_currency, norm_version FROM jobs")).all()
    assert len(rows) == 5
    for status, countries, smin, cur, ver in rows:
        assert status in ("resolved", "partial", "ambiguous", "unknown")
        if status in ("ambiguous", "unknown"):
            assert not countries  # nothing is silently assigned
        if smin is not None:
            assert cur is not None
        assert ver >= 2
    # every open job with a country has a matching job_locations row
    orphan = db.execute(text("SELECT count(*) FROM jobs j WHERE cardinality(j.country_codes) > 0 AND NOT EXISTS "
                             "(SELECT 1 FROM job_locations jl WHERE jl.job_id = j.id)")).scalar()
    assert orphan == 0
    assert dt  # noqa


def test_backfill_survives_a_row_that_cannot_be_derived(db, monkeypatch):
    from career_crawler import backfill

    c = make_company(db, "kaputt.de", name="Kaputt GmbH")
    bad = legacy_job(db, c, 1, title="Defekt", city="Dresden")
    good = legacy_job(db, c, 2, title="Gut", city="Berlin")
    real = backfill.derive_job_fields

    def flaky(row, skills):
        if row["id"] == bad.id:
            raise RecursionError("boom")
        return real(row, skills)

    monkeypatch.setattr(backfill, "derive_job_fields", flaky)
    with get_engine().begin() as conn:
        assert backfill.backfill_jobs(conn) == 2
    db.expire_all()
    assert db.get(Job, bad.id).norm_version >= 2 and db.get(Job, bad.id).geo_status == "unknown"
    assert db.get(Job, good.id).country_codes == ["DE"]
