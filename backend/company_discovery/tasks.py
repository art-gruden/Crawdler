"""Celery tasks (thin wrappers around jobs.py)."""

from __future__ import annotations

from gcc_core.celery_app import celery_app
from gcc_core.config import get_settings
from gcc_core.db import session_scope

from . import jobs


@celery_app.task(name="company_discovery.tasks.run_osm_job")
def run_osm_job(job_id: int) -> None:
    jobs.run_osm_job(job_id)


@celery_app.task(name="company_discovery.tasks.run_csv_job")
def run_csv_job(job_id: int) -> None:
    jobs.run_csv_job(job_id)


@celery_app.task(name="company_discovery.tasks.verify_companies", soft_time_limit=1800)
def verify_companies(company_ids: list[int]) -> dict:
    return dict(jobs.verify_company_ids(company_ids))


@celery_app.task(name="company_discovery.tasks.verify_due")
def verify_due() -> int:
    """Periodic: (re-)verify websites whose next_verify_at has passed (new, retry, recheck)."""
    settings = get_settings()
    with session_scope() as s:
        ids = jobs.lease_for_verification(s, limit=settings.verify_due_batch)
    for i in range(0, len(ids), settings.verify_batch_size):
        verify_companies.delay(ids[i : i + settings.verify_batch_size])
    return len(ids)
