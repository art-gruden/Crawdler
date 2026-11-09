"""Celery tasks of the career crawler.

Queues (routing in gcc_core.celery_app):
  career_scan  – HTML/ATS batch runner (claims scan_requests)
  browser_scan – Playwright pass for JS career pages (claims crawl_runs.browser_pending)
  extract      – optional LLM extraction
  maintenance  – recovery, adaptive scheduling, retention

Every task is idempotent: work is claimed in PostgreSQL (SKIP LOCKED), so a redelivered
or duplicated message simply finds nothing (or something else) to do. Time limits bound
each task; the beat entries expire so messages cannot pile up while a worker is busy.
"""

from __future__ import annotations

import asyncio
import time

from gcc_core.celery_app import celery_app
from gcc_core.db import session_scope

from .config import get_crawler_settings

_S = get_crawler_settings()
_LAST_RETENTION = {"t": 0.0}


def _kick_browser() -> None:
    browser_scan.apply_async(expires=300)


def _kick_llm(_company_id: int, _run_id: int) -> None:
    extract_llm.apply_async(expires=600)


@celery_app.task(
    name="career_crawler.tasks.career_scan",
    soft_time_limit=_S.batch_time_budget_s + _S.scan_time_budget_s + 120,
    time_limit=_S.batch_time_budget_s + _S.scan_time_budget_s + 180,
)
def career_scan(max_companies: int | None = None) -> dict:
    from .runner import BatchRunner

    if not _S.enabled:
        return {"skipped": "crawler disabled"}
    runner = BatchRunner(_S, enqueue_browser=_kick_browser, enqueue_llm=_kick_llm)
    return asyncio.run(runner.run(max_companies=max_companies))


@celery_app.task(name="career_crawler.tasks.browser_scan", soft_time_limit=900, time_limit=960)
def browser_scan() -> dict:
    from .browser import run_browser_batch
    from .metrics import Metrics
    from .runner import worker_name

    if not (_S.enabled and _S.browser_enabled):
        return {"skipped": "browser disabled"}
    metrics = Metrics(worker_name("browser"))
    try:
        return asyncio.run(run_browser_batch(_S, time_budget_s=780, metrics=metrics))
    finally:
        with session_scope() as s:
            metrics.flush(s)


@celery_app.task(name="career_crawler.tasks.extract_llm", soft_time_limit=900, time_limit=960)
def extract_llm() -> int:
    from .maintenance import llm_pass

    return asyncio.run(llm_pass(_S))


@celery_app.task(name="career_crawler.tasks.maintenance", soft_time_limit=600, time_limit=660)
def maintenance() -> dict:
    from . import maintenance as m

    out = {"recover": m.recover(_S), "scheduled": m.schedule_due(_S)}
    if time.monotonic() - _LAST_RETENTION["t"] > 3600:
        out["retention"] = m.retention(_S)
        _LAST_RETENTION["t"] = time.monotonic()
    return out
