"""Admin API for the vacancy statistics dashboard and the Telegram reports.

Mounted under ``/api/crawler/stats/jobs`` next to the other admin (crawler dashboard) routes, with the same
session-cookie authentication (``current_user``). It is deliberately *not* part of the public Bot API
(``/api/bot/v1``), and Telegram credentials never leave the server.

Every endpoint takes the same query parameters (see ``StatQuery``) and goes through ``jobstats``, so the
summary, the charts, the tables and the Telegram post count identically. ``as_of`` pins the snapshot time:
the dashboard sends the ``snapshot_at`` of its first response with every following request.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from gcc_core.auth import current_user
from gcc_core.db import get_db

from . import jobstats, statspublish, statsreport
from .config import CrawlerSettings, get_crawler_settings
from .jobstats import Period, StatFilters, StatsError

stats_router = APIRouter(
    prefix="/api/crawler/stats/jobs", tags=["job-statistics"], dependencies=[Depends(current_user)]
)


def _bad(exc: StatsError) -> HTTPException:
    return HTTPException(422, str(exc))


class StatQuery:
    """Period, time zone, snapshot and filters: identical for every statistics endpoint."""

    def __init__(
        self,
        period: str = Query("today", description="today | yesterday | last_24h | 7d | 30d | custom"),
        date_from: str | None = Query(None, alias="from", description="custom period: first local date (YYYY-MM-DD)"),
        date_to: str | None = Query(None, alias="to", description="custom period: last local date, inclusive"),
        timezone: str | None = Query(None, max_length=64, description="IANA zone; default Europe/Berlin"),
        country: list[str] | None = Query(None, description="DE | AT | CH (several = OR); none = all DACH"),
        region: list[str] | None = Query(None, description="ISO 3166-2, e.g. DE-BY, AT-9, CH-ZH"),
        city: list[str] | None = Query(None, description="exact city name (combine with country for same-named cities)"),
        specialization: list[str] | None = Query(None, description="taxonomy slug/id (group includes members) or 'unclassified'"),
        exclude_initial_load: bool = Query(False, description="leave out vacancies of companies first crawled in the period"),
        as_of: str | None = Query(None, description="snapshot time (ISO with offset); default now"),
    ):
        self._raw = (period, date_from, date_to, timezone, country, region, city, specialization, exclude_initial_load, as_of)

    def resolve(self, settings: CrawlerSettings) -> tuple[Period, StatFilters, dt.datetime]:
        period, d_from, d_to, tz, country, region, city, spec, excl, as_of = self._raw
        try:
            snap = jobstats.parse_as_of(as_of)
            p = jobstats.resolve_period(period, tz, snap, d_from, d_to, default_tz=settings.stats_timezone)
            f = jobstats.parse_filters(country, region, city, spec, excl)
        except StatsError as exc:
            raise _bad(exc) from exc
        return p, f, snap


def _run(fn, *a, **kw) -> Any:
    try:
        return fn(*a, **kw)
    except StatsError as exc:
        raise _bad(exc) from exc


@stats_router.get("/config", summary="Dashboard options: taxonomy, countries, regions, time zone, Telegram status")
def stats_config(db: Session = Depends(get_db), settings: CrawlerSettings = Depends(get_crawler_settings)):
    return {
        **jobstats.filter_options(db),
        "timezone": settings.stats_timezone,
        "telegram": statspublish.status(settings),
        "languages": list(statsreport.LANGUAGES),
        "formats": list(statsreport.FORMATS),
    }


@stats_router.get("/summary", summary="KPI cards: newly discovered, active, updated/closed/reopened, data quality")
def stats_summary(
    q: StatQuery = Depends(),
    db: Session = Depends(get_db),
    settings: CrawlerSettings = Depends(get_crawler_settings),
):
    p, f, snap = q.resolve(settings)
    return jobstats.summary(db, p, f, snap)


@stats_router.get("/trends", summary="Newly discovered vacancies per local day (hourly for periods up to 48 h)")
def stats_trends(
    q: StatQuery = Depends(),
    db: Session = Depends(get_db),
    settings: CrawlerSettings = Depends(get_crawler_settings),
):
    p, f, _ = q.resolve(settings)
    return jobstats.trends(db, p, f)


@stats_router.get("/specializations", summary="Specialization table (primary specialization, additive)")
def stats_specializations(
    q: StatQuery = Depends(),
    level: str = Query("specialization", pattern="^(specialization|group)$"),
    sort: str = Query("sel", pattern="^(sel|prev|change|today|7d|30d|active|name)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(10, ge=0, le=200, description="top N rows; 0 = all"),
    db: Session = Depends(get_db),
    settings: CrawlerSettings = Depends(get_crawler_settings),
):
    p, f, snap = q.resolve(settings)
    return _run(jobstats.specializations, db, p, f, snap, level=level, sort=sort, order=order, limit=limit or None)


@stats_router.get("/cities", summary="City / region / country ranking (unique job breakdown or overlapping coverage)")
def stats_cities(
    q: StatQuery = Depends(),
    mode: str = Query("unique", pattern="^(unique|coverage)$"),
    level: str = Query("city", pattern="^(city|region|country)$"),
    sort: str = Query("sel", pattern="^(sel|prev|change|today|7d|30d|active|name)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(20, ge=0, le=500, description="top N rows; 0 = all"),
    db: Session = Depends(get_db),
    settings: CrawlerSettings = Depends(get_crawler_settings),
):
    p, f, snap = q.resolve(settings)
    return _run(jobstats.cities, db, p, f, snap, mode=mode, level=level, sort=sort, order=order, limit=limit or None)


@stats_router.get("/countries", summary="Newly discovered vacancies per country (DE / AT / CH, several, unknown)")
def stats_countries(
    q: StatQuery = Depends(),
    db: Session = Depends(get_db),
    settings: CrawlerSettings = Depends(get_crawler_settings),
):
    p, f, snap = q.resolve(settings)
    return jobstats.countries(db, p, f, snap)


@stats_router.get("/specialization-city", summary="Specialization x city matrix and top combinations")
def stats_specialization_city(
    q: StatQuery = Depends(),
    level: str = Query("group", pattern="^(specialization|group)$"),
    rows: int = Query(8, ge=1, le=50),
    cols: int = Query(8, ge=1, le=50),
    combos: int = Query(20, ge=1, le=200),
    sort: str = Query("total", pattern="^(total|name)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
    settings: CrawlerSettings = Depends(get_crawler_settings),
):
    p, f, snap = q.resolve(settings)
    return _run(
        jobstats.specialization_city, db, p, f, snap, level=level, rows=rows, cols=cols, combos=combos, sort=sort, order=order
    )


# --- Telegram -----------------------------------------------------------------------------------------------


class ReportIn(BaseModel):
    """Period/filters exactly as the dashboard sends them, plus the post options."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    period: str = "today"
    date_from: str | None = Field(None, alias="from")
    date_to: str | None = Field(None, alias="to")
    timezone: str | None = Field(None, max_length=64)
    country: list[str] = Field(default_factory=list, max_length=3)
    region: list[str] = Field(default_factory=list, max_length=60)
    city: list[str] = Field(default_factory=list, max_length=20)
    specialization: list[str] = Field(default_factory=list, max_length=80)
    exclude_initial_load: bool = False
    as_of: str | None = None
    language: str = "en"
    top_specializations: int = 5
    top_cities: int = 5
    include_bot_link: bool = True
    include_countries: bool = True
    format: str = "plain"
    bot_link: str | None = Field(None, max_length=120, description="https://t.me/<bot>; default: server setting")


class PublishIn(ReportIn):
    approved_sha256: str = Field(min_length=64, max_length=64, description="sha256 of the previewed text")
    confirm: bool = False


def _report(body: ReportIn, db: Session, settings: CrawlerSettings) -> tuple[dict[str, Any], Period]:
    try:
        snap = jobstats.parse_as_of(body.as_of)
        p = jobstats.resolve_period(
            body.period, body.timezone, snap, body.date_from, body.date_to, default_tz=settings.stats_timezone
        )
        f = jobstats.parse_filters(body.country, body.region, body.city, body.specialization, body.exclude_initial_load)
        link = statsreport.validate_bot_link(body.bot_link) or _settings_link(settings)
        opts = statsreport.ReportOptions(
            language=body.language,
            top_specializations=body.top_specializations,
            top_cities=body.top_cities,
            include_bot_link=body.include_bot_link,
            include_countries=body.include_countries,
            fmt=body.format,
            bot_link=link,
        )
        return statsreport.build(db, p, f, snap, opts), p
    except StatsError as exc:
        raise _bad(exc) from exc


def _settings_link(settings: CrawlerSettings) -> str | None:
    try:
        return statsreport.validate_bot_link(settings.stats_telegram_bot_link)
    except StatsError:
        return None  # a malformed server setting is ignored, not echoed


@stats_router.post("/telegram-preview", summary="Generate the Telegram post from live numbers (never publishes)")
def telegram_preview(
    body: ReportIn,
    db: Session = Depends(get_db),
    settings: CrawlerSettings = Depends(get_crawler_settings),
):
    report, _ = _report(body, db, settings)
    return {**report, "telegram": statspublish.status(settings)}


@stats_router.post("/telegram-publish", summary="Publish an approved post to the configured channel (Phase 4, off by default)")
def telegram_publish(
    body: PublishIn,
    db: Session = Depends(get_db),
    settings: CrawlerSettings = Depends(get_crawler_settings),
    user: str = Depends(current_user),
):
    if not body.confirm:
        raise HTTPException(422, "confirm must be true: publishing is a manual, approved action")
    report, p = _report(body, db, settings)
    try:
        return statspublish.publish(
            db,
            settings,
            user=user,
            report=report,
            approved_sha256=body.approved_sha256,
            filters=report["filters"],
            period_start=p.start,
            period_end=p.end,
        )
    except statspublish.PublishError as exc:
        detail: dict[str, Any] = {"message": exc.message, **exc.extra}
        raise HTTPException(exc.status_code, detail) from exc


@stats_router.get("/telegram-publications", summary="Publication history (what was posted, when, with Telegram's answer)")
def telegram_publications(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    return {"items": statspublish.history(db, limit)}
