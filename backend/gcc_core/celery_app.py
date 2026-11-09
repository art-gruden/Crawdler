"""Celery application shared by all modules.

Queues:
  discovery    – Overpass and CSV import jobs (run with concurrency 1: one Overpass request at a time)
  verify       – website verification batches
  career_scan  – career crawler HTML/ATS batches          (worker-crawl)
  extract      – optional LLM extraction                  (worker-crawl)
  maintenance  – crawler recovery, scheduling, retention  (worker-crawl)
  browser_scan – Playwright rendering for JS career pages (worker-browser, separate container)
"""

from __future__ import annotations

from celery import Celery
from celery.signals import setup_logging

from .config import get_settings
from .log import configure_logging

settings = get_settings()

celery_app = Celery(
    "gcc", broker=settings.redis_url, backend=None, include=["company_discovery.tasks", "career_crawler.tasks"]
)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    task_routes={
        "company_discovery.tasks.run_osm_job": {"queue": "discovery"},
        "company_discovery.tasks.run_csv_job": {"queue": "discovery"},
        "company_discovery.tasks.verify_companies": {"queue": "verify"},
        "company_discovery.tasks.verify_due": {"queue": "verify"},
        "career_crawler.tasks.career_scan": {"queue": "career_scan"},
        "career_crawler.tasks.browser_scan": {"queue": "browser_scan"},
        "career_crawler.tasks.extract_llm": {"queue": "extract"},
        "career_crawler.tasks.maintenance": {"queue": "maintenance"},
    },
    broker_transport_options={"visibility_timeout": 4 * 3600},
    timezone="Europe/Berlin",
    beat_schedule={
        "verify-due-websites": {"task": "company_discovery.tasks.verify_due", "schedule": 600.0},
        # Expiring messages: if a worker is still busy, the next tick is dropped instead of piling up.
        "career-scan": {"task": "career_crawler.tasks.career_scan", "schedule": 60.0, "options": {"expires": 55}},
        "browser-scan": {"task": "career_crawler.tasks.browser_scan", "schedule": 120.0, "options": {"expires": 110}},
        "crawler-maintenance": {"task": "career_crawler.tasks.maintenance", "schedule": 300.0,
                                "options": {"expires": 290}},
    },
)


@setup_logging.connect
def _logging(**_kwargs):
    configure_logging(settings.log_level, settings.log_json)
