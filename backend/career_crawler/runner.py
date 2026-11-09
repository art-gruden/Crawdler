"""HTML worker: claims scan requests from PostgreSQL and scans companies concurrently.

One ``career_scan`` task runs a bounded batch (time budget), then returns; the beat
starts the next one. Within the batch:

* companies are claimed from ``scan_requests`` (SKIP LOCKED) only while there are free
  slots — no unbounded in-memory queue;
* ``crawl_runs`` holds a unique "running" row per company, so the same company is never
  scanned twice at once, even across workers;
* every few pages the run heartbeats a checkpoint; a crashed worker's run is marked
  ``abandoned`` by maintenance and resumed (visited URLs skipped) on the next claim;
* failures are retried a bounded number of times, then the request is failed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import socket
import time

from sqlalchemy import select, update

from gcc_core import scan_queue
from gcc_core.config import get_settings
from gcc_core.db import session_scope
from gcc_core.log import get_logger
from gcc_core.models import Company, ScanRequest
from gcc_core.netsafe import NetPolicy

from .config import CrawlerSettings, get_crawler_settings
from .engine import ScanResult, SiteScan
from .fetch import AdjustableLimiter, Fetcher, MemoryThrottle, RedisThrottle
from .metrics import Metrics
from .models import CrawlRun, ExtractionError, SiteFingerprint
from .resources import ResourceController
from .store import load_context, persist, start_run

log = get_logger(__name__)


def worker_name(kind: str) -> str:
    return f"{kind}@{socket.gethostname()}:{os.getpid()}"


def build_fetcher(settings: CrawlerSettings, *, limiter: AdjustableLimiter | None = None, on_stat=None) -> Fetcher:
    app = get_settings()
    policy = NetPolicy.from_settings(app)
    if settings.test_mode:
        from .testing import FixtureTransport, fixture_resolver

        if not settings.fixtures_dir:
            raise RuntimeError("CRAWLER_TEST_MODE requires CRAWLER_FIXTURES_DIR")
        transport = FixtureTransport(settings.fixtures_dir)
        return Fetcher(
            settings,
            policy=policy,
            user_agent=app.crawler_user_agent,
            throttle=MemoryThrottle(),
            limiter=limiter,
            resolver=fixture_resolver(transport),
            transport=transport,
            on_stat=on_stat,
        )
    try:
        throttle = RedisThrottle(app.redis_url)
    except Exception:  # pragma: no cover - redis library missing
        throttle = MemoryThrottle()
    return Fetcher(
        settings, policy=policy, user_agent=app.crawler_user_agent, throttle=throttle, limiter=limiter, on_stat=on_stat
    )


def _pending_count() -> int:
    from sqlalchemy import func

    with session_scope() as s:
        return s.execute(
            select(func.count()).select_from(ScanRequest).where(ScanRequest.status == "pending")
        ).scalar_one()


def _claim(worker: str, n: int) -> list[tuple[int, int]]:
    with session_scope() as s:
        return [(r.id, r.company_id) for r in scan_queue.claim(s, worker=worker, limit=n)]


def finish_request(request_id: int, *, ok: bool, error: str | None, max_attempts: int) -> None:
    """Bounded retries: a failed request goes back to pending until ``max_attempts``."""
    with session_scope() as s:
        req = s.get(ScanRequest, request_id)
        if req is None:
            return
        if not ok and req.attempts < max_attempts:
            req.status, req.error, req.claimed_by = "pending", error, None
            s.execute(update(Company).where(Company.id == req.company_id).values(scan_status="queued"))
            return
        scan_queue.finish(s, request_id, ok=ok, error=error)


class BatchRunner:
    def __init__(
        self,
        settings: CrawlerSettings | None = None,
        *,
        worker: str | None = None,
        fetcher: Fetcher | None = None,
        controller: ResourceController | None = None,
        enqueue_browser=None,
        enqueue_llm=None,
    ):
        self.s = settings or get_crawler_settings()
        self.worker = worker or worker_name("html")
        self.ctrl = controller or ResourceController(self.s)
        self.limiter = AdjustableLimiter(self.ctrl.http_limit)
        self.metrics = Metrics(self.worker)
        self._own_fetcher = fetcher is None
        self.fetcher = fetcher or build_fetcher(self.s, limiter=self.limiter, on_stat=self._on_stat)
        if fetcher is not None:
            fetcher.limiter = self.limiter
            fetcher.on_stat = self._on_stat
        self.enqueue_browser = enqueue_browser
        self.enqueue_llm = enqueue_llm
        self.results: dict[int, ScanResult] = {}

    def _on_stat(self, st) -> None:
        self.metrics.on_fetch(st)
        self.ctrl.observe(st.status, st.elapsed_ms)

    async def _tick(self) -> None:
        pending = await asyncio.to_thread(_pending_count)
        self.ctrl.http_active = self.limiter.active
        self.ctrl.sample(queue_pending=pending)
        decision = self.ctrl.decide(self.ctrl.last)
        await self.limiter.set_limit(decision.http_limit)
        snap = self.ctrl.snapshot()
        await asyncio.to_thread(self._flush_metrics, snap)

    def _flush_metrics(self, snap: dict) -> None:
        with session_scope() as s:
            self.metrics.flush(s, snap)

    async def run(self, *, time_budget_s: float | None = None, max_companies: int | None = None) -> dict:
        budget = time_budget_s or self.s.batch_time_budget_s
        deadline = time.monotonic() + budget
        running: dict[asyncio.Task, int] = {}
        done_count = 0
        last_tick = 0.0
        claimed_total = 0
        try:
            while True:
                now = time.monotonic()
                if now - last_tick > 5:
                    await self._tick()
                    last_tick = now
                can_claim = now < deadline - 30 and (max_companies is None or claimed_total < max_companies)
                slots = min(self.s.companies_in_parallel, max(1, self.ctrl.http_limit)) - len(running)
                if can_claim and slots > 0:
                    n = slots if max_companies is None else min(slots, max_companies - claimed_total)
                    claimed = await asyncio.to_thread(_claim, self.worker, n)
                    claimed_total += len(claimed)
                    for req_id, company_id in claimed:
                        running[asyncio.create_task(self.scan_one(req_id, company_id))] = company_id
                    if not claimed and not running:
                        break
                elif not running:
                    break
                done, _ = await asyncio.wait(running.keys(), timeout=5, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    running.pop(t, None)
                    done_count += 1
                    if t.exception() is not None:  # scan_one handles its own errors; this is a bug guard
                        log.error("scan task crashed", error=repr(t.exception()))
        finally:
            if running:
                await asyncio.gather(*running.keys(), return_exceptions=True)
            await self._tick()
            if self._own_fetcher:
                await self.fetcher.aclose()
        return {"companies": done_count, "http_limit": self.ctrl.http_limit}

    async def scan_one(self, request_id: int, company_id: int) -> ScanResult | None:
        s = self.s

        def begin():
            with session_scope() as db:
                fp = db.get(SiteFingerprint, company_id)
                if fp is not None and fp.rate_limited_until and fp.rate_limited_until > dt.datetime.now(dt.UTC):
                    return "deferred", None
                run_id = start_run(db, company_id, request_id=request_id, worker=self.worker)
                if run_id is None:
                    return "duplicate", None
                return "ok", (run_id, *load_context(db, company_id, s))

        try:
            status, ctx = await asyncio.to_thread(begin)
        except LookupError as exc:
            await asyncio.to_thread(finish_request, request_id, ok=False, error=str(exc), max_attempts=0)
            return None
        if status != "ok":
            await asyncio.to_thread(
                finish_request,
                request_id,
                ok=True,
                error=f"skipped: {'rate limited' if status == 'deferred' else 'already running'}",
                max_attempts=s.max_scan_attempts,
            )
            return None
        run_id, info, view, known, resume = ctx

        async def checkpoint(state: dict) -> None:
            from .store import heartbeat

            def hb():
                with session_scope() as db:
                    heartbeat(db, run_id, state, pages=state.get("pages"))

            await asyncio.to_thread(hb)

        scan = SiteScan(
            self.fetcher, s, info, fingerprint=view, known_pages=known, resume=resume, checkpoint=checkpoint
        )
        try:
            result = await asyncio.wait_for(scan.run(), timeout=s.scan_time_budget_s + 60)
        except Exception as exc:  # engine bug or timeout: record, retry boundedly
            log.exception("scan failed", company_id=company_id)
            await asyncio.to_thread(self._fail_run, run_id, company_id, exc)
            await asyncio.to_thread(
                finish_request,
                request_id,
                ok=False,
                error=f"{type(exc).__name__}: {exc}"[:500],
                max_attempts=s.max_scan_attempts,
            )
            return None

        def save():
            with session_scope() as db:
                stats = persist(db, info, run_id, result, s, full_discovery=view.full_discovery_due)
                if result.llm_candidates and s.llm_enabled:
                    db.execute(update(CrawlRun).where(CrawlRun.id == run_id).values(llm_urls=result.llm_candidates[:3]))
                return stats

        try:
            stats = await asyncio.to_thread(save)
        except Exception as exc:  # persistence bug: keep the request bounded and the run inspectable
            log.exception("persist failed", company_id=company_id)
            await asyncio.to_thread(self._fail_run, run_id, company_id, exc)
            await asyncio.to_thread(
                finish_request,
                request_id,
                ok=False,
                error=f"persist: {type(exc).__name__}"[:500],
                max_attempts=s.max_scan_attempts,
            )
            return None
        self.metrics.company_done(result.strategy_stats, len(result.jobs), stats["jobs_new"])
        ok = result.stop is None or bool(result.jobs)
        await asyncio.to_thread(
            finish_request,
            request_id,
            ok=ok,
            error=None if ok else f"{result.outcome}: {result.stop}"[:500],
            max_attempts=1,
        )  # site-side stops are scheduled, not retried immediately
        if result.browser_urls and s.browser_enabled and self.enqueue_browser:
            self.enqueue_browser()
        if result.llm_candidates and s.llm_enabled and self.enqueue_llm:
            self.enqueue_llm(company_id, run_id)
        self.results[company_id] = result
        log.info(
            "company scanned",
            company_id=company_id,
            outcome=result.outcome,
            jobs=len(result.jobs),
            new=stats["jobs_new"],
            pages=result.pages_fetched,
            strategy=result.best_strategy,
        )
        return result

    def _fail_run(self, run_id: int, company_id: int, exc: Exception) -> None:
        with session_scope() as db:
            db.execute(
                update(CrawlRun)
                .where(CrawlRun.id == run_id)
                .values(status="failed", finished_at=dt.datetime.now(dt.UTC), outcome="internal_error")
            )
            db.add(
                ExtractionError(
                    company_id=company_id,
                    run_id=run_id,
                    stage="engine",
                    kind="internal_error",
                    message=f"{type(exc).__name__}: {exc}"[:1000],
                )
            )


def run_batch_sync(**kwargs) -> dict:
    return asyncio.run(
        BatchRunner(
            **{
                k: v
                for k, v in kwargs.items()
                if k in ("settings", "worker", "fetcher", "controller", "enqueue_browser", "enqueue_llm")
            }
        ).run(time_budget_s=kwargs.get("time_budget_s"), max_companies=kwargs.get("max_companies"))
    )
