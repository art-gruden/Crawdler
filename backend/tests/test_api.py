"""HTTP API tests (FastAPI TestClient, Celery eager, fake Overpass, fake verification)."""

from __future__ import annotations

import csv
import io

import httpx
import pytest
from fastapi.testclient import TestClient

from company_discovery import jobs
from company_discovery.overpass import OverpassClient
from company_discovery.verify import Verification, apply_result
from gcc_core import scan_queue
from gcc_core.config import get_settings
from gcc_core.db import SessionLocal, session_scope
from gcc_core.models import Company

from .conftest import FIXTURES
from .test_discovery_jobs import fake_overpass

H = {"X-Requested-With": "gcc-ui"}


@pytest.fixture
def app_client(monkeypatch):
    from gcc_core.app import create_app

    handler, calls = fake_overpass({"14612": "overpass_dresden.json", "14713": "overpass_leipzig.json"})

    def client_factory(settings, session_factory, **kw):
        return OverpassClient(
            settings,
            session_factory,
            http=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda s: None,
            **kw,
        )

    monkeypatch.setattr(jobs, "OverpassClient", client_factory)
    verified: list[int] = []

    def fake_verify(ids, **_):  # every site "works" except one
        settings = get_settings()
        with session_scope() as s:
            for cid in ids:
                c = s.get(Company, cid)
                status = "access_restricted" if c.domain == "neustadt-software.de" else "verified"
                apply_result(s, c, Verification(status, c.website_url, c.domain, 200, 0.9, {}), settings=settings)
                verified.append(cid)
        return {}

    monkeypatch.setattr(jobs, "verify_company_ids", fake_verify)
    client = TestClient(create_app())
    client.verified = verified
    client.overpass_calls = calls
    return client


def login(c: TestClient):
    r = c.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"}, headers=H)
    assert r.status_code == 200, r.text
    return c


def test_auth_required_and_csrf(app_client):
    c = app_client
    assert c.get("/api/companies").status_code == 401
    assert c.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"}).status_code == 403
    assert c.post("/api/auth/login", json={"username": "admin", "password": "wrong"}, headers=H).status_code == 401
    login(c)
    assert c.get("/api/auth/me").json() == {"username": "admin"}
    assert c.post("/api/discovery/osm", json={"regions": ["14612"]}).status_code == 403  # no CSRF header
    assert c.get("/api/health").json()["database"] == "ok"


def test_osm_discovery_flow(app_client):
    c = login(app_client)
    regions = c.get("/api/discovery/regions").json()
    assert regions[0]["code"] == "DE-SN" and len(regions[0]["children"]) == 13
    assert {i["id"] for i in c.get("/api/discovery/industries").json()} >= {"it", "industrial", "engineering"}

    r = c.post(
        "/api/discovery/osm",
        json={"regions": ["14612", "14713"], "industries": ["it", "industrial", "engineering", "technology"]},
        headers=H,
    )
    assert r.status_code == 202, r.text
    job = c.get(f"/api/discovery/jobs/{r.json()['id']}").json()
    assert job["status"] == "completed"
    assert job["license"] == "ODbL-1.0" and "OpenStreetMap" in job["attribution"]
    assert job["stats"]["new"] == 6 and job["stats"]["verification_queued"] == 6

    items = c.get(f"/api/discovery/jobs/{job['id']}/items", params={"outcome": "duplicate"}).json()
    assert items["total"] == 2 and {i["domain"] for i in items["items"]} == {"elbtal-cloud.de"}
    assert items["counts"] == {"new": 6, "duplicate_in_job": 2, "invalid_url": 1}
    assert c.get(f"/api/discovery/jobs/{job['id']}/items", params={"outcome": "bogus"}).status_code == 422

    # verification ran and auto-queued the verified sites for the crawler
    page = c.get("/api/companies", params={"scan_status": "queued"}).json()
    assert page["total"] == 5
    restricted = c.get("/api/companies", params={"verification": "access_restricted"}).json()["items"]
    assert [x["domain"] for x in restricted] == ["neustadt-software.de"]

    usage = c.get("/api/discovery/overpass/usage").json()
    assert usage["requests"] == 2 and usage["max_requests"] == 50


def test_validation_errors(app_client):
    c = login(app_client)
    assert c.post("/api/discovery/osm", json={"regions": ["99999"]}, headers=H).status_code == 422
    assert (
        c.post("/api/discovery/osm", json={"regions": ["14612"], "industries": ["bakery"]}, headers=H).status_code
        == 422
    )
    assert c.post("/api/discovery/osm", json={"regions": []}, headers=H).status_code == 422


def test_company_list_filters_and_export(app_client):
    c = login(app_client)
    job_id = c.post("/api/discovery/osm", json={"regions": ["14612", "14713"]}, headers=H).json()["id"]

    all_ = c.get("/api/companies", params={"page_size": 3, "sort": "name", "order": "asc"}).json()
    assert all_["total"] == 7 and len(all_["items"]) == 3
    assert all_["items"][0]["name"] == "Elbtal Cloud Systems GmbH"
    assert all_["items"][0]["sources"] == ["osm"]
    page2 = c.get("/api/companies", params={"page_size": 3, "page": 3, "sort": "name", "order": "asc"}).json()
    assert len(page2["items"]) == 1

    assert c.get("/api/companies", params={"region": "14713"}).json()["total"] == 1
    assert c.get("/api/companies", params={"region": "DE-SN"}).json()["total"] == 7
    assert c.get("/api/companies", params={"industry": "industrial"}).json()["total"] == 2
    assert c.get("/api/companies", params={"q": "elbtal"}).json()["total"] == 1
    mueller = c.get("/api/companies", params={"q": "müller"}).json()["items"][0]
    assert mueller["domain"] == "xn--mller-przision-dib09a.de" and mueller["domain_display"] == "müller-präzision.de"
    assert c.get("/api/companies", params={"q": "pleisse-dc"}).json()["total"] == 1
    assert c.get("/api/companies", params={"source": "osm"}).json()["total"] == 7
    assert c.get("/api/companies", params={"source": "csv:"}).json()["total"] == 0
    assert c.get("/api/companies", params={"job_id": job_id, "new_in_job": True}).json()["total"] == 6
    assert c.get("/api/companies", params={"sort": "bogus"}).status_code == 422

    detail = c.get(f"/api/companies/{all_['items'][0]['id']}").json()
    assert len(detail["source_records"]) == 3
    assert detail["source_records"][0]["license"] == "ODbL-1.0"

    r = c.get("/api/companies/export.csv", params={"region": "14612"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "OpenStreetMap" in r.headers["x-data-attribution"]
    rows = list(csv.DictReader(io.StringIO(r.text.lstrip("﻿"))))
    assert len(rows) == 6
    assert all(row["licenses"] == "ODbL-1.0" and "OpenStreetMap" in row["attribution"] for row in rows)
    assert c.get("/api/companies/999999").status_code == 404


def test_csv_import_endpoint(app_client):
    c = login(app_client)
    files = {"file": ("wfs.csv", (FIXTURES / "standort_sachsen_sample.csv").read_bytes(), "text/csv")}
    r = c.post("/api/companies/import", files=files, data={"profile": "standort_sachsen"}, headers=H)
    assert r.status_code == 202, r.text
    job = c.get(f"/api/discovery/jobs/{r.json()['id']}").json()
    assert job["status"] == "completed"
    assert job["source"] == "csv:standort_sachsen" and job["params"]["use_source_ids"] is True
    assert job["attribution"].startswith("Quelle: Wirtschaftsförderung Sachsen")
    assert len(job["file_sha256"]) == 64 and job["file_name"] == "wfs.csv"
    assert job["stats"]["new"] == 4
    assert c.get("/api/companies", params={"industry": "life_sciences"}).json()["total"] == 1

    bad = c.post("/api/companies/import", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")}, headers=H)
    assert bad.status_code == 422 and "company-name column" in bad.json()["detail"]
    named = c.post(
        "/api/companies/import",
        files={"file": ("ihk.csv", b"name,website\nTest Firma,test-firma-beispiel.de\n", "text/csv")},
        data={"profile": "generic", "source_name": "IHK Dresden Liste", "license": "CC-BY-4.0"},
        headers=H,
    ).json()
    assert named["source"] == "csv:ihk-dresden-liste" and named["license"] == "CC-BY-4.0"


def test_import_size_limit(app_client, monkeypatch):
    c = login(app_client)
    monkeypatch.setattr(get_settings(), "import_max_mb", 0)
    r = c.post("/api/companies/import", files={"file": ("big.csv", b"name\nx\n", "text/csv")}, headers=H)
    assert r.status_code == 413


def test_scan_endpoints(app_client):
    c = login(app_client)
    with session_scope() as s:
        s.add_all(
            [
                Company(
                    name="Verified",
                    name_key="verified",
                    domain="verified-beispiel.de",
                    website_url="https://verified-beispiel.de/",
                    verification_status="verified",
                ),
                Company(
                    name="Pending",
                    name_key="pending",
                    domain="pending-beispiel.de",
                    website_url="https://pending-beispiel.de/",
                ),
                Company(
                    name="Robots",
                    name_key="robots",
                    domain="robots-beispiel.de",
                    website_url="https://robots-beispiel.de/",
                    verification_status="robots_disallowed",
                ),
                Company(name="No site", name_key="no site", verification_status="no_website"),
            ]
        )
    ids = {c_["name"]: c_["id"] for c_ in c.get("/api/companies").json()["items"]}

    assert c.post(f"/api/companies/{ids['Verified']}/scan", headers=H).json()["status"] == "queued"
    assert c.post(f"/api/companies/{ids['Verified']}/scan", headers=H).json()["status"] == "already_queued"
    r = c.post(f"/api/companies/{ids['Pending']}/scan", headers=H)
    assert r.status_code == 202 and r.json()["status"] == "verifying"
    assert ids["Pending"] in app_client.verified
    assert c.get(f"/api/companies/{ids['Pending']}").json()["scan_status"] == "queued"  # queued after verification
    r = c.post(f"/api/companies/{ids['Robots']}/scan", headers=H)
    assert r.status_code == 409 and "robots_disallowed" in r.json()["detail"]
    assert c.post("/api/companies/424242/scan", headers=H).status_code == 404

    bulk = c.post("/api/companies/scan", json={"ids": list(ids.values()) + [999]}, headers=H).json()
    assert bulk == {"queued": 0, "already_queued": 2, "verifying": 0, "not_scannable": 2, "not_found": 1}

    # the crawler side of the queue
    with SessionLocal() as s:
        claimed = scan_queue.claim(s, worker="crawler-1", limit=10)
        s.commit()
        assert {r.company_id for r in claimed} == {ids["Verified"], ids["Pending"]}
        assert claimed[0].priority == 10
        scan_queue.finish(s, claimed[0].id, ok=True)
        s.commit()
    assert c.get(f"/api/companies/{claimed[0].company_id}").json()["scan_status"] == "done"


def test_cancel_pending_job(app_client, monkeypatch):
    c = login(app_client)
    from company_discovery import tasks

    monkeypatch.setattr(tasks.run_osm_job, "delay", lambda job_id: None)  # leave it pending
    job = c.post("/api/discovery/osm", json={"regions": ["14612"]}, headers=H).json()
    assert c.post("/api/discovery/osm", json={"regions": ["14713"]}, headers=H).status_code == 409  # one at a time
    cancelled = c.post(f"/api/discovery/jobs/{job['id']}/cancel", headers=H).json()
    assert cancelled["status"] == "cancelled"
    assert c.post("/api/discovery/osm", json={"regions": ["14713"]}, headers=H).status_code == 202


def test_openapi_lists_required_endpoints(app_client):
    paths = app_client.get("/api/openapi.json").json()["paths"]
    assert "post" in paths["/api/discovery/osm"]
    assert "post" in paths["/api/companies/import"]
    assert "get" in paths["/api/companies"]
    assert "post" in paths["/api/companies/{company_id}/scan"]
