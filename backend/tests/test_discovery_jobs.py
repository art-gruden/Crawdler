"""End-to-end job runs against PostgreSQL with a fake Overpass server."""

import datetime as dt
import json

import httpx
from sqlalchemy import func, select

from company_discovery import jobs
from company_discovery.models import DiscoveryJob, DiscoveryJobItem
from company_discovery.overpass import OverpassClient
from company_discovery.service import name_key
from gcc_core.db import SessionLocal
from gcc_core.models import Company, CompanySource

from .conftest import FIXTURES, load_fixture


def fake_overpass(by_code: dict[str, str]):
    """Answer each query with the fixture registered for the Kreisschlüssel it contains."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = httpx.QueryParams(request.content.decode()).get("data", "")
        calls.append(body)
        for code, fixture in by_code.items():
            if f'"^{code}"' in body:
                return httpx.Response(200, json=load_fixture(fixture))
        return httpx.Response(200, json=load_fixture("overpass_noarea.json"))

    return handler, calls


def make_job(db, **params) -> int:
    job = DiscoveryJob(kind="osm", source="osm", params=params, license="ODbL-1.0", attribution="© OSM")
    db.add(job)
    db.commit()
    return job.id


def run(job_id, settings, handler, dispatched):
    client = OverpassClient(
        settings, SessionLocal, http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None
    )
    jobs.run_osm_job(job_id, client=client, dispatch=dispatched.extend)


def test_osm_job_dresden_leipzig(db, settings):
    handler, calls = fake_overpass({"14612": "overpass_dresden.json", "14713": "overpass_leipzig.json"})
    dispatched: list[int] = []
    job_id = make_job(
        db, regions=["14612", "14713"], industries=["it", "engineering", "industrial", "technology"], verify=True
    )
    run(job_id, settings, handler, dispatched)

    job = db.get(DiscoveryJob, job_id)
    assert job.status == "completed", job.error
    st = job.stats
    assert st["steps_total"] == 2 and st["steps_done"] == 2
    # Dresden: 7 named elements → elbtal(new), neustadt(new), works(new), IB facebook(invalid_url),
    # elbtal Süd (duplicate_in_job), institute(new), müller(new). Leipzig: pleisse(new), elbtal NL (duplicate_in_job)
    assert st["new"] == 6
    assert st["duplicate_in_job"] == 2
    assert st["invalid_url"] == 1
    assert st["records"] == 9
    assert st["overpass_requests"] == 2
    assert job.data_timestamp.astimezone(dt.UTC).isoformat().startswith("2026-11-08T21:15")
    assert len(calls) == 2

    companies = db.execute(select(Company).order_by(Company.id)).scalars().all()
    by_domain = {c.domain: c for c in companies}
    assert set(by_domain) == {
        "elbtal-cloud.de",
        "neustadt-software.de",
        "halbleiterwerk-beispiel.de",
        "institut-beispiel.de",
        "xn--mller-przision-dib09a.de",
        "pleisse-dc.de",
        None,
    }
    elbtal = by_domain["elbtal-cloud.de"]
    assert elbtal.website_url == "https://www.elbtal-cloud.de/"  # tracking parameter removed
    assert elbtal.street == "Musterstraße 12" and elbtal.postcode == "01067" and elbtal.city == "Dresden"
    assert elbtal.district_key == "14612" and elbtal.state_code == "DE-SN"
    assert "Elbtal Cloud Systems Niederlassung Leipzig" in elbtal.alt_names
    srcs = db.execute(select(CompanySource).where(CompanySource.company_id == elbtal.id)).scalars().all()
    assert {s.source_record_id for s in srcs} == {"node/9100000001", "node/9100000004", "node/9100000102"}
    assert all(s.license == "ODbL-1.0" and "OpenStreetMap" in s.attribution for s in srcs)
    assert all("phone" not in s.data for s in srcs)
    ib = by_domain[None]
    assert ib.name == "Ingenieurbüro Weißeritz" and ib.verification_status == "no_website"
    assert ib.website_rejected_reason == "not_company_website"
    assert by_domain["pleisse-dc.de"].city == "Leipzig" and by_domain["pleisse-dc.de"].district_key == "14713"
    assert by_domain["halbleiterwerk-beispiel.de"].city == "Dresden"  # kreisfreie Stadt fallback
    # all companies with a usable domain were handed to verification once
    assert sorted(dispatched) == sorted(c.id for c in companies if c.domain)

    # --- second run: idempotent, nothing new
    dispatched.clear()
    job2 = make_job(
        db, regions=["14612", "14713"], industries=["it", "engineering", "industrial", "technology"], verify=True
    )
    run(job2, settings, handler, dispatched)
    j2 = db.get(DiscoveryJob, job2)
    assert j2.status == "completed"
    assert j2.stats.get("new", 0) == 0
    assert j2.stats["updated"] == 8 and j2.stats["invalid_url"] == 1
    assert j2.stats["cache_hits"] == 2  # identical queries within OVERPASS_CACHE_HOURS
    assert db.execute(select(func.count()).select_from(Company)).scalar() == 7
    assert dispatched == []  # leased already (next_verify_at in the future) or not unverified


def test_region_without_boundary_is_reported(db, settings):
    handler, _ = fake_overpass({})
    job_id = make_job(db, regions=["14628"], industries=["it"], verify=False)
    run(job_id, settings, handler, [])
    job = db.get(DiscoveryJob, job_id)
    assert job.status == "failed"
    assert "boundary not found" in job.errors[0]["error"]


def test_cancel_before_start(db, settings):
    handler, calls = fake_overpass({})
    job_id = make_job(db, regions=["DE-SN"], industries=["it"])
    db.get(DiscoveryJob, job_id).cancel_requested = True
    db.commit()
    run(job_id, settings, handler, [])
    db.expire_all()
    assert db.get(DiscoveryJob, job_id).status == "cancelled"
    assert calls == []


def _csv_job(db, profile, source, use_ids=True) -> int:
    job = DiscoveryJob(
        kind="csv",
        source=source,
        params={"profile": profile, "verify": True, "use_source_ids": use_ids},
        license="see-provider-terms",
        attribution="Quelle: WFS",
    )
    db.add(job)
    db.commit()
    return job.id


def test_csv_job_merges_with_osm(db, settings):
    handler, _ = fake_overpass({"14612": "overpass_dresden.json"})
    run(make_job(db, regions=["14612"], industries=["it"], verify=False), settings, handler, [])

    job_id = _csv_job(db, "standort_sachsen", "csv:standort_sachsen")
    path = jobs.import_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((FIXTURES / "standort_sachsen_sample.csv").read_bytes())
    dispatched: list[int] = []
    jobs.run_csv_job(job_id, dispatch=dispatched.extend)

    job = db.get(DiscoveryJob, job_id)
    assert job.status == "completed", job.error
    assert job.stats["new"] == 3 and job.stats["duplicate_existing"] == 1
    assert not path.exists()  # upload removed after a successful import
    items = db.execute(select(DiscoveryJobItem).where(DiscoveryJobItem.job_id == job_id)).scalars().all()
    dup = next(i for i in items if i.outcome == "duplicate_existing")
    assert dup.domain == "elbtal-cloud.de"
    elbtal = db.get(Company, dup.company_id)
    assert sorted({s.source for s in elbtal.sources}) == ["csv:standort_sachsen", "osm"]
    assert "technology" in elbtal.industries  # merged from MICRO!
    # 3 new companies + elbtal, which the OSM run (verify=False) left unverified
    assert len(dispatched) == 4

    # re-import of the same file: matched by Firmen-ID, no duplicates
    job2 = _csv_job(db, "standort_sachsen", "csv:standort_sachsen")
    jobs.import_path(job2).write_bytes((FIXTURES / "standort_sachsen_sample.csv").read_bytes())
    jobs.run_csv_job(job2, dispatch=lambda ids: None)
    assert db.get(DiscoveryJob, job2).stats["updated"] == 4
    assert db.execute(select(func.count()).select_from(Company)).scalar() == 6 + 3


def test_csv_without_website_dedups_by_name_and_postcode(db, settings):
    raw = "Firma;Webseite;PLZ\nBäckerei Sonnenschein GmbH;;01445\n".encode()
    raw2 = "Firma;Webseite;PLZ\nBäckerei Sonnenschein;;01445\n".encode()
    for content in (raw, raw2):
        job_id = _csv_job(db, "generic", "csv:generic", use_ids=False)
        jobs.import_path(job_id).write_bytes(content)
        jobs.run_csv_job(job_id, dispatch=lambda ids: None)
    rows = db.execute(select(Company)).scalars().all()
    assert len(rows) == 1 and rows[0].name_key == name_key("Bäckerei Sonnenschein")
    last = db.execute(select(DiscoveryJob).order_by(DiscoveryJob.id.desc())).scalars().first()
    assert last.stats == {"no_website": 1, "records": 1}
    items = db.execute(select(DiscoveryJobItem).where(DiscoveryJobItem.job_id == last.id)).scalars().all()
    assert items[0].outcome == "no_website"


def test_name_key():
    assert name_key("Müller GmbH & Co. KG") == "muller"
    assert name_key("Elbtal Cloud Systems GmbH") == "elbtal cloud systems"
    assert name_key("Neustadt Software UG (haftungsbeschränkt)") == "neustadt software"


def test_stats_are_json(db, settings):
    handler, _ = fake_overpass({"14612": "overpass_dresden.json"})
    job_id = make_job(db, regions=["14612"], industries=["it"], verify=False)
    run(job_id, settings, handler, [])
    json.dumps(db.get(DiscoveryJob, job_id).stats)
