"""FastAPI application: assembles the routers of all modules."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .auth import router as auth_router
from .companies_api import router as companies_router
from .config import get_settings
from .db import get_engine
from .log import configure_logging, get_logger

log = get_logger(__name__)


def create_app(*, check_secrets: bool = True) -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if check_secrets:
        settings.check_runtime_secrets()

    app = FastAPI(
        title="Germany Career Crawler API",
        version="0.1.0",
        description=(
            "Company discovery and career crawling for German employers. "
            "Company data from OpenStreetMap is © OpenStreetMap contributors, ODbL 1.0."
        ),
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    # Order matters: /api/companies/import (discovery) must be registered before /api/companies/{id}.
    from career_crawler.api import crawler_router, jobs_router
    from company_discovery.api import router as discovery_router

    app.include_router(auth_router)
    app.include_router(discovery_router)
    app.include_router(companies_router)
    app.include_router(jobs_router)
    app.include_router(crawler_router)

    @app.exception_handler(KeyError)
    async def _key_error(_request: Request, exc: KeyError):
        return JSONResponse(status_code=422, content={"detail": str(exc).strip("'\"")})

    @app.get("/api/health", tags=["health"])
    def health():
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:  # pragma: no cover - reported to the health checker
            raise HTTPException(503, f"database unavailable: {type(exc).__name__}") from exc
        try:
            import redis

            redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2).ping()
            broker = "ok"
        except Exception as exc:
            broker = f"unavailable: {type(exc).__name__}"
        return {"status": "ok", "database": "ok", "broker": broker}

    return app
