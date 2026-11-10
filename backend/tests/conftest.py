"""Test setup: a real PostgreSQL database (TEST_DATABASE_URL), migrated with Alembic."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://gcc:gcc@localhost:5432/gcc_test")
_DATA = tempfile.mkdtemp(prefix="gcc-test-")
os.environ.update(
    {
        "DATABASE_URL": TEST_DB,
        "SECRET_KEY": "test-secret-key-0123456789abcdef0123456789",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "correct horse battery",
        "COOKIE_SECURE": "false",
        "DATA_DIR": _DATA,
        "LOG_JSON": "false",
        "OVERPASS_MIN_INTERVAL_S": "0",
        "REDIS_URL": "redis://127.0.0.1:1/0",  # unreachable on purpose: tasks run eagerly
    }
)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402

from gcc_core.config import get_settings  # noqa: E402

get_settings.cache_clear()

FIXTURES = Path(__file__).parent / "fixtures"
BACKEND = Path(__file__).parent.parent


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    from gcc_core.db import get_engine

    with get_engine().begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    command.upgrade(cfg, "head")
    from gcc_core.celery_app import celery_app

    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    yield


@pytest.fixture(autouse=True)
def clean_db(migrated_db):
    from gcc_core.db import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            text(
                "TRUNCATE companies, company_sources, scan_requests, external_api_usage, "
                "discovery_jobs, discovery_job_items, career_sources, jobs, job_skills, crawl_runs, crawl_pages, "
                "crawl_metrics, site_fingerprints, extraction_errors, job_events, company_locations, stat_publications "
                "RESTART IDENTITY CASCADE"
            )
        )
    import shutil

    shutil.rmtree(Path(_DATA) / "overpass-cache", ignore_errors=True)
    yield


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def db():
    from gcc_core.db import SessionLocal

    s = SessionLocal()
    yield s
    s.close()
