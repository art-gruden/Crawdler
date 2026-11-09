"""Resource controller (hysteresis), crawler/jobs API, browser fallback (if Chromium is available)."""

from __future__ import annotations

import os
import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from career_crawler.models import CrawlRun, Job
from career_crawler.resources import ResourceController

from .crawler_helpers import SITES, crawler_settings, fixture_fetcher
from .test_crawler_runner import queue, run

H = {"X-Requested-With": "gcc-ui"}


class FakeProbe:
    def __init__(self):
        self.cpu, self.mem, self.cont = 20.0, 3000, (300, 1024)

    def cpu_pct(self):
        return self.cpu

    def mem_available_mb(self):
        return self.mem

    def container(self):
        return self.cont


def controller():
    clock = {"t": 0.0}
    probe = FakeProbe()
    c = ResourceController(
        crawler_settings(http_concurrency=16, http_concurrency_min=2),
        probe=probe,
        clock=lambda: clock["t"],
        cooldown_s=10,
        healthy_samples=3,
    )
    return c, probe, clock


def step(c, clock, n=1, dt=11):
    for _ in range(n):
        clock["t"] += dt
        c.decide()


def test_controller_increases_gradually_and_decreases_under_pressure():
    c, probe, clock = controller()
    start = c.http_limit
    step(c, clock, 2)
    assert c.http_limit == start  # needs 3 healthy samples in a row
    step(c, clock, 1)
    assert c.http_limit == start + 1
    probe.cpu = 95
    step(c, clock, 1)
    assert c.http_limit == int((start + 1) * 0.7)
    low = c.http_limit
    clock["t"] += 1
    c.decide()  # still pressure but inside the cooldown: no second cut
    assert c.http_limit == low
    probe.cpu = 70  # hysteresis band: neither pressure nor clearly healthy -> hold
    step(c, clock, 5)
    assert c.http_limit == low
    for _ in range(100):
        c.observe(429, 100)
    probe.cpu = 20
    step(c, clock, 1)
    assert c.http_limit < low  # 429/503 share > 5 %
    for _ in range(30):
        step(c, clock, 1)
    assert c.http_limit >= c.s.http_concurrency_min


def test_browser_pause_resume_hysteresis():
    c, probe, clock = controller()
    probe.mem = 550  # below pause (600)
    step(c, clock)
    assert not c.browser_allowed
    probe.mem = 700  # between pause and resume: stays paused
    step(c, clock)
    assert not c.browser_allowed
    probe.mem = 1000
    step(c, clock)
    assert c.browser_allowed
    probe.cont = (950, 1024)  # container close to its limit
    step(c, clock)
    assert not c.browser_allowed


@pytest.fixture
def client():
    from gcc_core.app import create_app

    c = TestClient(create_app())
    r = c.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"}, headers=H)
    assert r.status_code == 200
    return c


async def test_jobs_and_crawler_api(client, db):
    ids = queue(["gcc-fixture-static.de", "gcc-fixture-personio.de", "gcc-fixture-ratelimit.de"])
    await run()
    r = client.get("/api/jobs").json()
    assert r["total"] == 6
    assert client.get("/api/jobs", params={"q": "devops"}).json()["total"] == 2  # DevOps Engineer + Werkstudent DevOps
    assert client.get("/api/jobs", params={"skill": "Kubernetes", "city": "Dresden"}).json()["total"] >= 1
    assert client.get("/api/jobs", params={"method": "ats"}).json()["total"] == 3
    assert client.get("/api/jobs", params={"employment_type": "working_student"}).json()["total"] >= 1
    assert client.get("/api/jobs", params={"remote_type": "nonsense"}).status_code == 422
    jid = r["items"][0]["id"]
    d = client.get(f"/api/jobs/{jid}").json()
    assert "description" in d and d["company_name"]
    assert client.get("/api/jobs/skills").json()[0]["jobs"] >= 1
    st = client.get("/api/crawler/stats").json()
    assert st["jobs_open"] == 6 and st["completed_scans"] == 3 and "workers" in st and st["queues"]["scan_pending"] == 0
    assert client.get("/api/crawler/timeseries", params={"hours": 1, "bucket_minutes": 5}).status_code == 200
    assert len(client.get("/api/crawler/runs").json()) == 3
    prob = client.get("/api/crawler/problem-domains").json()
    assert [p["domain"] for p in prob] == ["gcc-fixture-ratelimit.de"]
    diag = client.get(f"/api/crawler/companies/{ids['gcc-fixture-ratelimit.de']}").json()
    assert diag["diagnosis"]["code"] == "rate_limited" and "Retry-After" in diag["diagnosis"]["text"]
    diag = client.get(f"/api/crawler/companies/{ids['gcc-fixture-personio.de']}").json()
    assert diag["fingerprint"]["ats_type"] == "personio" and diag["sources"][0]["kind"] == "ats_feed"
    # rescan: CSRF header required; full rescan forgets the cached strategy
    assert client.post(f"/api/crawler/companies/{ids['gcc-fixture-personio.de']}/rescan").status_code == 403
    r = client.post(f"/api/crawler/companies/{ids['gcc-fixture-personio.de']}/rescan", json={"full": True}, headers=H)
    assert r.status_code == 202 and r.json()["status"] == "queued"


CHROME = os.environ.get("CRAWLER_BROWSER_EXECUTABLE") or shutil.which("google-chrome") or shutil.which("chromium")


def _chromium_available() -> bool:
    if CHROME:
        return True
    try:
        from playwright._impl._driver import compute_driver_executable  # noqa: F401
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except Exception:
        return False


needs_browser = pytest.mark.skipif(not _chromium_available(), reason="no Chromium available")


@needs_browser
async def test_browser_pass_end_to_end(db):
    from career_crawler.browser import run_browser_batch

    ids = queue(["gcc-fixture-jsapp.de"])
    s = crawler_settings(browser_enabled=True, browser_executable=CHROME)
    await run(settings=s)
    f, t = fixture_fetcher(s)
    out = await run_browser_batch(s, fetcher=f, controller=ResourceController(s, probe=FakeProbe()), time_budget_s=120)
    await f.aclose()
    assert out["companies"] == 1
    jobs = db.execute(select(Job).where(Job.company_id == ids["gcc-fixture-jsapp.de"])).scalars().all()
    assert {j.title for j in jobs} == {"Embedded Software Entwickler (m/w/d)", "FPGA Ingenieur (m/w/d)"}
    assert all(j.extraction_method == "browser" for j in jobs)
    run_row = db.execute(select(CrawlRun)).scalar_one()
    db.refresh(run_row)
    assert not run_row.browser_pending and run_row.stats["browser"]["ok"]
    assert "gcc-fixture-jsapp.de/api/jobs.json" in t.requests  # XHR went through the SSRF-safe fetcher


@needs_browser
async def test_browser_subresources_cannot_reach_private_networks(tmp_path):
    from career_crawler.browser import BrowserRenderer

    root = tmp_path / "sites"
    shutil.copytree(SITES, root)
    host = root / "gcc-fixture-evil.de"
    host.mkdir()
    (host / "index.html").write_text(
        "<html><body><h1>Karriere</h1><div id=out>x</div>"
        "<script src='http://169.254.169.254/latest/meta-data/x.js'></script>"
        "<script>fetch('http://10.0.0.1/admin').then(r=>r.text()).then(t=>document.getElementById('out').textContent=t)"
        ".catch(e=>document.getElementById('out').textContent='blocked');"
        "fetch('http://rebind.gcc-fixture-evil.de/secret').catch(()=>{});</script></body></html>"
    )
    (root / "rebind.gcc-fixture-evil.de").mkdir()
    (root / "rebind.gcc-fixture-evil.de" / "secret").write_text("TOP SECRET")
    s = crawler_settings(browser_enabled=True, browser_executable=CHROME)
    f, t = fixture_fetcher(s, root=root, overrides={"rebind.gcc-fixture-evil.de": "192.168.1.10"})
    async with BrowserRenderer(s, f) as r:
        page = await r.render("https://gcc-fixture-evil.de/")
    await f.aclose()
    html = page.body.decode()
    assert "TOP SECRET" not in html and "blocked" in html
    assert not any(x.startswith(("169.254", "10.0.0.1", "rebind.")) for x in t.requests)
