"""Recovery, recrawl scheduling, retention and the optional LLM pass."""

from __future__ import annotations

import asyncio
import datetime as dt
import shutil
from pathlib import Path

from sqlalchemy import and_, delete, exists, func, select, update

from gcc_core import scan_queue
from gcc_core.config import get_settings
from gcc_core.db import session_scope
from gcc_core.log import get_logger
from gcc_core.models import Company, ScanRequest

from .config import CrawlerSettings, get_crawler_settings
from .engine import CompanyInfo, ScanResult, SourceFound
from .models import CrawlMetric, CrawlPage, CrawlRun, ExtractionError, Job, SiteFingerprint
from .store import abandon_stale_runs, persist

log = get_logger(__name__)


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def recover(settings: CrawlerSettings | None = None) -> dict:
    """Crashed workers: abandon runs without heartbeat, re-queue (bounded) or fail their requests."""
    s = settings or get_crawler_settings()
    stale = now() - dt.timedelta(minutes=s.claim_stale_minutes)
    with session_scope() as db:
        abandoned = abandon_stale_runs(db, minutes=s.claim_stale_minutes)
        reqs = (
            db.execute(
                select(ScanRequest)
                .where(ScanRequest.status == "claimed", ScanRequest.claimed_at < stale)
                .where(
                    ~exists().where(and_(CrawlRun.company_id == ScanRequest.company_id, CrawlRun.status == "running"))
                )
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        requeued = failed = 0
        for r in reqs:
            if r.attempts < s.max_scan_attempts:
                r.status, r.claimed_by, r.error = "pending", None, "requeued after worker loss"
                db.execute(update(Company).where(Company.id == r.company_id).values(scan_status="queued"))
                requeued += 1
            else:
                scan_queue.finish(db, r.id, ok=False, error="worker lost too often")
                failed += 1
    return {"abandoned_runs": len(abandoned), "requeued": requeued, "failed": failed}


def schedule_due(settings: CrawlerSettings | None = None) -> int:
    """Queue verified companies whose adaptive recrawl time has come."""
    s = settings or get_crawler_settings()
    queued = 0
    with session_scope() as db:
        rows = (
            db.execute(
                select(Company)
                .join(SiteFingerprint, SiteFingerprint.company_id == Company.id)
                .where(SiteFingerprint.next_scan_at <= now(), Company.verification_status == "verified")
                .where(Company.scan_status.not_in(("queued", "scanning")))
                .order_by(SiteFingerprint.next_scan_at)
                .limit(s.schedule_batch)
            )
            .scalars()
            .all()
        )
        for c in rows:
            try:
                if scan_queue.request_scan(db, c, source="schedule") == "queued":
                    queued += 1
            except scan_queue.NotScannable:
                continue
    return queued


def retention(settings: CrawlerSettings | None = None) -> dict:
    s = settings or get_crawler_settings()
    t = now()
    out = {}
    with session_scope() as db:
        out["errors"] = db.execute(
            delete(ExtractionError).where(ExtractionError.created_at < t - dt.timedelta(days=s.error_retention_days))
        ).rowcount
        out["metrics"] = db.execute(
            delete(CrawlMetric).where(CrawlMetric.bucket < t - dt.timedelta(days=s.metrics_retention_days))
        ).rowcount
        out["pages"] = db.execute(
            delete(CrawlPage).where(CrawlPage.fetched_at < t - dt.timedelta(days=s.page_retention_days))
        ).rowcount
        out["closed_jobs"] = db.execute(
            delete(Job).where(Job.status != "open", Job.closed_at < t - dt.timedelta(days=s.closed_job_retention_days))
        ).rowcount
        # keep the last 20 runs per company
        ranked = select(
            CrawlRun.id,
            func.row_number().over(partition_by=CrawlRun.company_id, order_by=CrawlRun.id.desc()).label("rn"),
        ).subquery()
        out["runs"] = db.execute(
            delete(CrawlRun).where(
                CrawlRun.id.in_(select(ranked.c.id).where(ranked.c.rn > 20)),
                CrawlRun.status != "running",
                CrawlRun.browser_pending.is_(False),
            )
        ).rowcount
        expired = db.execute(
            select(CrawlPage.id, CrawlPage.raw_path).where(
                CrawlPage.raw_path.is_not(None), CrawlPage.raw_expires_at < t
            )
        ).all()
        for _pid, path in expired:
            Path(path).unlink(missing_ok=True)
        if expired:
            db.execute(
                update(CrawlPage)
                .where(CrawlPage.id.in_([p for p, _ in expired]))
                .values(raw_path=None, raw_expires_at=None)
            )
        out["raw_files_expired"] = len(expired)
    out["raw_files_trimmed"] = _trim_raw_dir(s.raw_html_max_mb)
    return out


def _trim_raw_dir(max_mb: int) -> int:
    root = Path(get_settings().data_dir) / "crawl-raw"
    if not root.exists():
        return 0
    files = sorted((p for p in root.rglob("*.html")), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    removed = 0
    while files and total > max_mb * 1024 * 1024:
        f = files.pop(0)
        total -= f.stat().st_size
        f.unlink(missing_ok=True)
        removed += 1
    for d in root.iterdir():
        if d.is_dir() and not any(d.iterdir()):
            shutil.rmtree(d, ignore_errors=True)
    return removed


async def llm_pass(settings: CrawlerSettings | None = None, *, limit: int = 5, fetcher=None, client=None) -> int:
    """Optional: LLM extraction for career pages where every other strategy found nothing."""
    from .extract.html import content_text
    from .llm import extract_with_llm
    from .runner import build_fetcher

    s = settings or get_crawler_settings()
    if not (s.llm_enabled and s.llm_url):
        return 0

    def claim():
        with session_scope() as db:
            runs = (
                db.execute(
                    select(CrawlRun)
                    .where(func.cardinality(CrawlRun.llm_urls) > 0)
                    .order_by(CrawlRun.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            out = [(r.id, r.company_id, list(r.llm_urls)) for r in runs]
            for r in runs:
                r.llm_urls = []
            return out

    work = await asyncio.to_thread(claim)
    if not work:
        return 0
    own = fetcher is None
    f = fetcher or build_fetcher(s)
    n = 0
    try:
        for _run_id, company_id, urls in work:

            def info(cid=company_id):
                with session_scope() as db:
                    c = db.get(Company, cid)
                    return CompanyInfo(c.id, c.name, c.domain, c.website_url) if c and c.domain else None

            ci = await asyncio.to_thread(info)
            if ci is None:
                continue
            result = ScanResult(outcome="llm")
            for url in urls[:3]:
                try:
                    page = await f.get(url)
                    text = content_text(page.text(), url) or ""
                    raws = await extract_with_llm(s, text, url, client=client)
                except Exception as exc:
                    from .engine import ErrorRecord

                    result.errors.append(ErrorRecord("llm", "llm_error", url, repr(exc)[:300], strategy="llm"))
                    continue
                from .extract.normalize import normalize

                for raw in raws:
                    job = normalize(raw)
                    if job:
                        from .engine import FoundJob

                        result.jobs.append(FoundJob(job, url, "career_page"))
                result.sources[url] = SourceFound(url, "career_page", job_count=len(raws))
                result.note("llm", ok=bool(raws), jobs=len(raws))
            result.complete = False  # LLM output never closes postings

            def save(ci=ci, result=result):
                with session_scope() as db:
                    return persist(db, ci, None, result, s, full_discovery=False)

            n += (await asyncio.to_thread(save))["jobs_new"]
    finally:
        if own:
            await f.aclose()
    return n
