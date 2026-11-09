"""Celery application shared by all modules.

Queues:
  discovery  – Overpass and CSV import jobs (run with concurrency 1: one Overpass request at a time)
  verify     – website verification batches
"""

from __future__ import annotations

from celery import Celery
from celery.signals import setup_logging

from .config import get_settings
from .log import configure_logging

settings = get_settings()

celery_app = Celery("gcc", broker=settings.redis_url, backend=None, include=["company_discovery.tasks"])
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
    },
    broker_transport_options={"visibility_timeout": 4 * 3600},
    timezone="Europe/Berlin",
    beat_schedule={
        "verify-due-websites": {"task": "company_discovery.tasks.verify_due", "schedule": 600.0},
    },
)


@setup_logging.connect
def _logging(**_kwargs):
    configure_logging(settings.log_level, settings.log_json)
