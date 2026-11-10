"""CSV / YAML export of the Vacancies table."""

from __future__ import annotations

import csv
import datetime as dt
import io

import pytest
import yaml
from fastapi.testclient import TestClient

from career_crawler.models import Job, JobSkill
from gcc_core.models import Company

from .crawler_helpers import make_company

H = {"X-Requested-With": "gcc-ui"}
LONG = "Wir suchen Verstärkung.\n\nAufgaben:\n- Kubernetes betreiben\n- Größe & Qualität: 100 % «Umlaute» äöüß\n"


@pytest.fixture
def client():
    from gcc_core.app import create_app

    c = TestClient(create_app())
    assert (
        c.post(
            "/api/auth/login", json={"username": "admin", "password": "correct horse battery"}, headers=H
        ).status_code
        == 200
    )
    return c


def add_job(db, company: Company, n: int, **kw) -> Job:
    base = dict(
        company_id=company.id,
        dedup_key=f"url:https://{company.domain}/j/{n}",
        title=f"Job {n}",
        title_norm=f"job {n}",
        source_url=f"https://{company.domain}/j/{n}",
        extraction_method="html",
        description=LONG,
        requirements=["Linux", "Python"],
        responsibilities=["Betrieb"],
        languages=[{"lang": "de", "level": "B2"}],
        source_values={"raw": {"title": f"Job {n}"}},
        confidence=0.9,
        status="open",
        employment_types=["full_time"],
        city="Dresden",
        remote_type="hybrid",
        date_posted=dt.date(2026, 11, 1),
    )
    base.update(kw)
    j = Job(**base)
    db.add(j)
    db.flush()
    return j


@pytest.fixture
def seeded(db):
    c = make_company(db, "export-firma.de", name="Export Firma GmbH")
    a = add_job(db, c, 1, title='=HYPERLINK("http://evil.example","klick")', city="München")
    b = add_job(db, c, 2, title="Kubernetes Engineer", city="Dresden")
    z = add_job(db, c, 3, title="Altes Projekt", status="closed", description=None)
    db.add_all(
        [
            JobSkill(job_id=b.id, skill="Kubernetes", category="devops"),
            JobSkill(job_id=b.id, skill="Linux", category="os"),
        ]
    )
    db.commit()
    return {"company": c, "a": a, "b": b, "z": z}


def rows(text: str) -> list[dict]:
    assert text.startswith("﻿")  # BOM for Excel
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


def test_csv_follows_the_table_filters(client, seeded):
    r = client.get("/api/jobs/export.csv", params={"company_id": seeded["company"].id})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"].startswith('attachment; filename="vacancies-')
    got = rows(r.text)
    assert {x["id"] for x in got} == {
        str(seeded["a"].id),
        str(seeded["b"].id),
    }  # default status=open: closed one excluded

    # the table and the export agree for every filter
    for params in (
        {"city": "Dresden"},
        {"skill": "Kubernetes"},
        {"status": "all"},
        {"q": "kubernetes"},
        {"status": "closed"},
    ):
        params = {**params, "company_id": seeded["company"].id}
        table = {i["id"] for i in client.get("/api/jobs", params=params).json()["items"]}
        exported = {int(x["id"]) for x in rows(client.get("/api/jobs/export.csv", params=params).text)}
        assert exported == table, params

    b = next(x for x in got if x["id"] == str(seeded["b"].id))
    assert b["company"] == "Export Firma GmbH" and b["skills"] == "Kubernetes; Linux"
    assert b["date_posted"] == "2026-11-01" and b["employment_types"] == "full_time"
    assert "description" not in b  # the CSV is the table; descriptions are in the YAML


def test_csv_neutralises_spreadsheet_formulas_and_keeps_umlauts(client, seeded):
    got = {
        x["id"]: x for x in rows(client.get("/api/jobs/export.csv", params={"company_id": seeded["company"].id}).text)
    }
    assert got[str(seeded["a"].id)]["title"].startswith("'=HYPERLINK")  # no live formula in Excel/Calc
    assert got[str(seeded["a"].id)]["city"] == "München"


def test_csv_semicolon_delimiter_and_validation(client, seeded):
    r = client.get("/api/jobs/export.csv", params={"delimiter": ";", "company_id": seeded["company"].id})
    assert r.text.lstrip("﻿").splitlines()[0].startswith("id;company;")
    assert client.get("/api/jobs/export.csv", params={"delimiter": "\t"}).status_code == 422
    assert client.get("/api/jobs/export.csv", params={"limit": 10_001}).status_code == 422


def test_yaml_has_every_field_and_the_full_description(client, seeded):
    r = client.get("/api/jobs/export.yaml", params={"company_id": seeded["company"].id, "status": "all"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/yaml")
    doc = yaml.safe_load(r.text)  # valid YAML that round-trips
    assert doc["total_matching"] == 3 and doc["exported"] == 3 and doc["truncated"] is False
    assert doc["filters"]["status"] == "all" and doc["filters"]["company_id"] == seeded["company"].id
    by_id = {j["id"]: j for j in doc["jobs"]}
    b = by_id[seeded["b"].id]
    assert b["description"] == LONG  # complete, untouched: newlines, umlauts, symbols
    assert b["requirements"] == ["Linux", "Python"] and b["languages"] == [{"lang": "de", "level": "B2"}]
    assert b["source_values"] == {"raw": {"title": "Job 2"}}
    assert b["skills"] == [{"skill": "Kubernetes", "category": "devops"}, {"skill": "Linux", "category": "os"}]
    assert b["company"]["name"] == "Export Firma GmbH" and b["company"]["domain"] == "export-firma.de"
    assert by_id[seeded["z"].id]["description"] is None and by_id[seeded["z"].id]["status"] == "closed"
    # all stored columns are present (only the generated search vector is left out)
    assert set(b) - {"company", "skills"} == {c.key for c in Job.__table__.columns} - {"search"}
    assert "|" in r.text  # long text is written as a readable block, not an escaped one-liner


def test_yaml_for_one_vacancy_and_404(client, seeded):
    r = client.get(f"/api/jobs/{seeded['b'].id}/export.yaml")
    assert r.status_code == 200
    one = yaml.safe_load(r.text)
    assert one["id"] == seeded["b"].id and one["description"] == LONG
    assert r.headers["content-disposition"] == f'attachment; filename="vacancy-{seeded["b"].id}.yaml"'
    assert client.get("/api/jobs/999999999/export.yaml").status_code == 404


def test_yaml_truncation_is_declared(client, seeded):
    doc = yaml.safe_load(
        client.get(
            "/api/jobs/export.yaml", params={"company_id": seeded["company"].id, "status": "all", "limit": 2}
        ).text
    )
    assert doc["total_matching"] == 3 and doc["exported"] == 2 and len(doc["jobs"]) == 2 and doc["truncated"] is True


def test_empty_result_is_still_valid(client, seeded):
    doc = yaml.safe_load(client.get("/api/jobs/export.yaml", params={"q": "gibtesnicht"}).text)
    assert doc["jobs"] == [] and doc["exported"] == 0
    assert len(rows(client.get("/api/jobs/export.csv", params={"q": "gibtesnicht"}).text)) == 0


def test_exports_require_login(seeded):
    from gcc_core.app import create_app

    anon = TestClient(create_app())
    for url in ("/api/jobs/export.csv", "/api/jobs/export.yaml", f"/api/jobs/{seeded['b'].id}/export.yaml"):
        assert anon.get(url).status_code == 401, url
