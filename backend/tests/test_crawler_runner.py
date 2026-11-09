"""Batch runner + persistence against PostgreSQL, websites from offline fixtures."""

from __future__ import annotations

import datetime as dt
import json
import shutil

import pytest
from sqlalchemy import func, select, update

from career_crawler import maintenance, store
from career_crawler.models import CareerSource, CrawlMetric, CrawlPage, CrawlRun, Job, JobSkill, SiteFingerprint
from career_crawler.resources import ResourceController
from career_crawler.runner import BatchRunner
from gcc_core import scan_queue
from gcc_core.db import session_scope
from gcc_core.models import Company, ScanRequest

from .crawler_helpers import SITES, crawler_settings, fixture_fetcher, make_company


class CalmProbe:
    def cpu_pct(self):
        return 10.0

    def mem_available_mb(self):
        return 3000

    def container(self):
        return (200, 1024)


def queue(hosts: list[str]) -> dict[str, int]:
    ids = {}
    with session_scope() as s:
        for h in hosts:
            c = make_company(s, h)
            scan_queue.request_scan(s, c, source="test")
            ids[h] = c.id
    return ids


async def run(settings=None, root=SITES, **kw):
    s = settings or crawler_settings()
    f, t = fixture_fetcher(s, root=root)
    runner = BatchRunner(s, worker="test-worker", fetcher=f, controller=ResourceController(s, probe=CalmProbe()), **kw)
    try:
        out = await runner.run(time_budget_s=120)
    finally:
        await f.aclose()
    return runner, out, t


def requeue(company_id: int) -> None:
    with session_scope() as s:
        scan_queue.request_scan(s, s.get(Company, company_id), source="test")


async def test_batch_persists_jobs_sources_fingerprints(db):
    hosts = [
        "gcc-fixture-static.de",
        "gcc-fixture-personio.de",
        "gcc-fixture-jsonld.de",
        "gcc-fixture-jsapp.de",
        "gcc-fixture-novacancy.de",
        "gcc-fixture-ratelimit.de",
    ]
    ids = queue(hosts)
    kicked = []
    runner, out, _ = await run(
        settings=crawler_settings(browser_enabled=True), enqueue_browser=lambda: kicked.append(1)
    )
    assert out["companies"] == len(hosts)
    jobs = db.execute(select(Job)).scalars().all()
    assert len(jobs) == 3 + 3 + 2
    assert all(j.description is None or "<" not in j.description for j in jobs)  # plain text only
    devops = next(j for j in jobs if j.title.startswith("DevOps"))
    assert devops.date_posted == dt.date(2026, 10, 15) and devops.city == "Dresden" and devops.remote_type == "hybrid"
    assert {s for (s,) in db.execute(select(JobSkill.skill).where(JobSkill.job_id == devops.id))} >= {
        "Kubernetes",
        "Terraform",
    }
    personio = db.execute(select(Job).where(Job.ats_type == "personio")).scalars().all()
    assert {j.ats_job_id for j in personio} == {"2001", "2002", "2003"}
    assert all(j.date_posted is not None for j in personio)
    fp = db.get(SiteFingerprint, ids["gcc-fixture-personio.de"])
    assert (fp.best_strategy, fp.ats_type, fp.ats_account) == ("ats", "personio", "fixturecorp")
    assert fp.next_scan_at is not None and fp.interval_h <= 24 * 1.1  # new postings -> short interval
    nov = db.get(SiteFingerprint, ids["gcc-fixture-novacancy.de"])
    assert nov.interval_h >= 168 * 0.9
    rl = db.get(SiteFingerprint, ids["gcc-fixture-ratelimit.de"])
    assert rl.rate_limited_until is not None and rl.error_streak == 1
    js_run = db.execute(select(CrawlRun).where(CrawlRun.company_id == ids["gcc-fixture-jsapp.de"])).scalar_one()
    assert js_run.browser_pending and js_run.browser_urls == ["https://gcc-fixture-jsapp.de/karriere"] and kicked
    src = db.execute(select(CareerSource).where(CareerSource.company_id == ids["gcc-fixture-personio.de"])).scalar_one()
    assert src.kind == "ats_feed" and src.evidence["evidence_url"].startswith("https://fixturecorp.jobs.personio.de")
    reqs = dict(db.execute(select(ScanRequest.company_id, ScanRequest.status)).all())
    assert reqs[ids["gcc-fixture-static.de"]] == "done" and reqs[ids["gcc-fixture-ratelimit.de"]] == "failed"
    assert db.execute(select(func.sum(CrawlMetric.companies_done))).scalar() == len(hosts)
    assert db.execute(select(func.count()).select_from(CrawlPage)).scalar() > 5


async def test_rescan_dedups_and_lifecycle_needs_complete_misses(db, tmp_path):
    root = tmp_path / "sites"
    shutil.copytree(SITES, root)
    ids = queue(["gcc-fixture-static.de"])
    cid = ids["gcc-fixture-static.de"]
    await run(root=root)
    first = {j.title: j.id for j in db.execute(select(Job)).scalars()}
    assert len(first) == 3

    # second scan: same postings -> no duplicates
    requeue(cid)
    await run(root=root)
    db.expire_all()
    assert {j.title: j.id for j in db.execute(select(Job)).scalars()} == first

    # one posting disappears, but the career page fails this time -> no miss counted
    site = root / "gcc-fixture-static.de"
    (site / "_routes.json").write_text(json.dumps({"/karriere": {"status": 500}}))
    requeue(cid)
    await run(root=root)
    db.expire_all()
    assert all(j.miss_count == 0 and j.status == "open" for j in db.execute(select(Job)).scalars())

    # page works again without the Werkstudent posting: misses accumulate, closing needs N misses + age
    (site / "_routes.json").unlink()
    html = (
        (site / "karriere.html")
        .read_text()
        .replace('<li><a href="/karriere/werkstudent-it">Werkstudent IT-Support (m/w/d)</a> – Dresden</li>', "")
    )
    (site / "karriere.html").write_text(html)
    for _ in range(3):
        requeue(cid)
        await run(root=root)
    db.expire_all()
    ws = db.get(Job, first["Werkstudent IT-Support (m/w/d)"])
    assert ws.miss_count == 3 and ws.status == "open"  # not seen for < close_min_days
    db.execute(update(Job).where(Job.id == ws.id).values(last_seen_at=Job.last_seen_at - dt.timedelta(days=5)))
    db.commit()
    requeue(cid)
    await run(root=root)
    db.expire_all()
    ws = db.get(Job, ws.id)
    assert ws.status == "closed" and ws.closed_at is not None
    assert db.get(Job, first["DevOps Engineer (m/w/d)"]).status == "open"

    # it comes back -> reopened, same row
    (site / "karriere.html").write_text((SITES / "gcc-fixture-static.de" / "karriere.html").read_text())
    requeue(cid)
    await run(root=root)
    db.expire_all()
    ws = db.get(Job, ws.id)
    assert ws.status == "open" and ws.miss_count == 0 and ws.closed_at is None


async def test_no_concurrent_runs_for_one_company(db):
    with session_scope() as s:
        c = make_company(s, "gcc-fixture-static.de")
        cid = c.id
    with session_scope() as s:
        assert store.start_run(s, cid, request_id=None, worker="a") is not None
    with session_scope() as s:
        assert store.start_run(s, cid, request_id=None, worker="b") is None


async def test_recovery_requeues_and_resumes(db):
    ids = queue(["gcc-fixture-static.de"])
    cid = ids["gcc-fixture-static.de"]
    old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
    with session_scope() as s:
        scan_queue.claim(s, worker="dead", limit=1)
        run_id = store.start_run(s, cid, request_id=None, worker="dead")
        s.execute(
            update(CrawlRun)
            .where(CrawlRun.id == run_id)
            .values(
                heartbeat_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=50),
                checkpoint={"visited": ["https://gcc-fixture-static.de/karriere/devops-engineer"]},
            )
        )
        s.execute(update(ScanRequest).values(claimed_at=old))
    out = maintenance.recover(crawler_settings())
    assert out == {"abandoned_runs": 1, "requeued": 1, "failed": 0}
    assert db.execute(select(ScanRequest.status)).scalar_one() == "pending"
    with session_scope() as s:
        _, _, _, resume = store.load_context(s, cid, crawler_settings())
    assert resume["visited"] == ["https://gcc-fixture-static.de/karriere/devops-engineer"]
    runner, _, t = await run()
    assert "gcc-fixture-static.de/karriere/devops-engineer" not in t.requests  # resumed: not fetched again
    assert len(db.execute(select(Job)).scalars().all()) == 3  # still known from the list page


async def test_bounded_retries_on_internal_errors(db, monkeypatch):
    ids = queue(["gcc-fixture-static.de"])

    async def boom(self):
        raise RuntimeError("engine bug")

    monkeypatch.setattr("career_crawler.engine.SiteScan.run", boom)
    s = crawler_settings(max_scan_attempts=2)
    await run(settings=s)
    req = db.execute(select(ScanRequest)).scalar_one()
    db.refresh(req)
    assert req.status == "failed" and req.attempts == 2  # retried once, then failed
    errors = db.execute(select(func.count()).select_from(store.ExtractionError)).scalar()
    assert errors == 2 and ids


async def test_schedule_due_requeues_verified_companies(db):
    ids = queue(["gcc-fixture-jsonld.de"])
    await run()
    cid = ids["gcc-fixture-jsonld.de"]
    assert maintenance.schedule_due(crawler_settings()) == 0  # not due yet
    with session_scope() as s:
        s.execute(update(SiteFingerprint).values(next_scan_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)))
    assert maintenance.schedule_due(crawler_settings()) == 1
    assert db.execute(select(ScanRequest.source).where(ScanRequest.status == "pending")).scalar_one() == "schedule"
    assert cid


async def test_retention_limits_growth(db, settings, tmp_path):
    ids = queue(["gcc-fixture-static.de"])
    s = crawler_settings(keep_raw_html=True)
    await run(settings=s)
    raw = db.execute(select(CrawlPage.raw_path).where(CrawlPage.raw_path.is_not(None))).scalars().all()
    assert raw
    with session_scope() as x:
        x.execute(update(CrawlPage).values(raw_expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1)))
    out = maintenance.retention(s)
    assert out["raw_files_expired"] == len(raw)
    assert db.execute(select(func.count()).select_from(CrawlPage).where(CrawlPage.raw_path.is_not(None))).scalar() == 0
    assert ids


@pytest.mark.parametrize(
    "outcome,expected_min,expected_max",
    [
        ("jobs_found", 16, 20),
        ("no_career_page", 21 * 24 * 0.9, 21 * 24 * 1.1),
        ("no_vacancies", 151, 185),
        ("access_restricted", 30 * 24 * 0.9, 30 * 24 * 1.1),
    ],
)
def test_recrawl_intervals(outcome, expected_min, expected_max):
    from career_crawler.schedule import next_interval_h

    now = dt.datetime.now(dt.UTC)
    h = next_interval_h(
        crawler_settings(),
        outcome=outcome,
        new_jobs=2 if outcome == "jobs_found" else 0,
        open_jobs=2 if outcome == "jobs_found" else 0,
        changed=True,
        last_new_job_at=now,
        last_change_at=now,
        error_streak=0,
        retry_after_s=None,
        now=now,
    )
    assert expected_min <= h <= expected_max


def test_rate_limit_interval_honours_retry_after():
    from career_crawler.schedule import next_interval_h

    now = dt.datetime.now(dt.UTC)
    h = next_interval_h(
        crawler_settings(),
        outcome="rate_limited",
        new_jobs=0,
        open_jobs=0,
        changed=False,
        last_new_job_at=None,
        last_change_at=None,
        error_streak=1,
        retry_after_s=20 * 3600,
        now=now,
    )
    assert h >= 20 * 0.9
