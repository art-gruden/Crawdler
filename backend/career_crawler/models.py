"""Career-crawler tables. ``companies`` and ``scan_requests`` (gcc_core) are reused as-is."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from gcc_core.db import Base

JOB_STATUSES = ("open", "closed", "expired")
REMOTE_TYPES = ("onsite", "hybrid", "remote", "unknown")
WORK_MODELS = ("REMOTE", "HYBRID", "ONSITE", "UNKNOWN")
GEO_STATUSES = ("resolved", "partial", "ambiguous", "unknown")
SENIORITIES = ("INTERN", "JUNIOR", "MID", "SENIOR", "LEAD", "EXECUTIVE", "UNKNOWN")
EVENT_TYPES = ("job.created", "job.updated", "job.closed", "job.reopened")
RUN_STATUSES = ("running", "done", "partial", "failed", "abandoned")
SOURCE_KINDS = ("career_page", "job_list", "ats_feed", "sitemap")
STRATEGIES = ("ats", "jsonld", "html", "content", "pdf", "browser", "llm")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class CareerSource(Base):
    """A verified place where a company publishes vacancies (page or ATS feed)."""

    __tablename__ = "career_sources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text)  # canonical URL (feed URL for ATS)
    kind: Mapped[str] = mapped_column(String(16))
    ats_type: Mapped[str | None] = mapped_column(String(32))
    ats_account: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))  # where/how detected
    active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_success_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default="0")
    last_job_count: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("company_id", "url", name="uq_career_sources_company_url"),
        CheckConstraint(_in("kind", SOURCE_KINDS), name="kind"),
        Index("ix_career_sources_ats", "ats_type", "ats_account"),
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    career_source_id: Mapped[int | None] = mapped_column(ForeignKey("career_sources.id", ondelete="SET NULL"))
    # Dedup identity, strongest available: ats:<type>:<account>:<id> | url:<canonical> | content:<hash>
    dedup_key: Mapped[str] = mapped_column(Text, unique=True)
    ats_type: Mapped[str | None] = mapped_column(String(32))
    ats_job_id: Mapped[str | None] = mapped_column(Text)

    title: Mapped[str] = mapped_column(Text)
    title_norm: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)  # page the posting was read from
    canonical_url: Mapped[str | None] = mapped_column(Text)
    alt_urls: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    application_url: Mapped[str | None] = mapped_column(Text)
    career_page_url: Mapped[str | None] = mapped_column(Text)

    location_raw: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    postcode: Mapped[str | None] = mapped_column(String(10))
    region: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(2))
    remote_type: Mapped[str] = mapped_column(String(8), server_default="unknown")
    employment_types: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))

    date_posted: Mapped[dt.date | None] = mapped_column(Date)  # only from the source, never = discovery
    valid_through: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(8), server_default="open")
    miss_count: Mapped[int] = mapped_column(Integer, server_default="0")

    description: Mapped[str | None] = mapped_column(Text)  # normalized plain text, never HTML
    requirements: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    responsibilities: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    languages: Mapped[list[dict]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)
    salary_currency: Mapped[str | None] = mapped_column(String(3))
    salary_period: Mapped[str | None] = mapped_column(String(8))
    salary_raw: Mapped[str | None] = mapped_column(Text)
    salary_basis: Mapped[str] = mapped_column(String(7), server_default="unknown")  # gross | net | unknown
    salary_confidence: Mapped[float | None] = mapped_column(Float)

    # --- DACH platform: normalized fields (see docs/DACH.md). The legacy columns above stay as they were.
    geo_status: Mapped[str] = mapped_column(String(9), server_default="unknown")
    geo_detail: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    country_codes: Mapped[list[str]] = mapped_column(ARRAY(String(2)), server_default=text("'{}'"))
    region_codes: Mapped[list[str]] = mapped_column(ARRAY(String(8)), server_default=text("'{}'"))
    location_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), server_default=text("'{}'"))
    work_model: Mapped[str] = mapped_column(String(7), server_default="UNKNOWN")
    remote_scope: Mapped[str] = mapped_column(String(9), server_default="unknown")
    remote_countries: Mapped[list[str]] = mapped_column(ARRAY(String(2)), server_default=text("'{}'"))
    employment_kinds: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{UNKNOWN}'"))
    specialization_id: Mapped[int | None] = mapped_column(
        SmallInteger, ForeignKey("specializations.id", ondelete="SET NULL")
    )
    specialization_confidence: Mapped[float | None] = mapped_column(Float)
    seniority: Mapped[str] = mapped_column(String(9), server_default="UNKNOWN")
    last_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    description_hash: Mapped[str | None] = mapped_column(String(40))
    quality_score: Mapped[float | None] = mapped_column(Float)
    quality: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    norm_version: Mapped[int] = mapped_column(SmallInteger, server_default="1")

    confidence: Mapped[float] = mapped_column(Float, server_default="0")
    extraction_method: Mapped[str] = mapped_column(String(16))
    content_hash: Mapped[str | None] = mapped_column(String(40))
    source_values: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))  # original values
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    search: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('simple', coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('simple', coalesce(city, '') || ' ' || coalesce(location_raw, '')), 'B') || "
            "setweight(to_tsvector('simple', left(coalesce(description, ''), 20000)), 'C')",
            persisted=True,
        ),
    )

    __table_args__ = (
        CheckConstraint(_in("status", JOB_STATUSES), name="status"),
        CheckConstraint(_in("remote_type", REMOTE_TYPES), name="remote_type"),
        Index("ix_jobs_company_status", "company_id", "status"),
        Index("ix_jobs_status_seen", "status", "last_seen_at"),
        Index("ix_jobs_discovered_at", "discovered_at"),
        Index("ix_jobs_date_posted", "date_posted"),
        Index("ix_jobs_company_title", "company_id", "title_norm"),
        Index("ix_jobs_canonical_url", "canonical_url"),
        Index("ix_jobs_search", "search", postgresql_using="gin"),
        Index("ix_jobs_employment_types", "employment_types", postgresql_using="gin"),
        CheckConstraint(_in("work_model", WORK_MODELS), name="work_model"),
        CheckConstraint(_in("geo_status", GEO_STATUSES), name="geo_status"),
        CheckConstraint(_in("seniority", SENIORITIES), name="seniority"),
        Index("ix_jobs_country_codes", "country_codes", postgresql_using="gin"),
        Index("ix_jobs_region_codes", "region_codes", postgresql_using="gin"),
        Index("ix_jobs_location_ids", "location_ids", postgresql_using="gin"),
        Index("ix_jobs_employment_kinds", "employment_kinds", postgresql_using="gin"),
        Index("ix_jobs_remote_countries", "remote_countries", postgresql_using="gin"),
        Index(
            "ix_jobs_open_first_seen",
            text("discovered_at DESC"),
            text("id DESC"),
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_jobs_open_work_model", "work_model", postgresql_where=text("status = 'open'")),
        Index("ix_jobs_open_specialization", "specialization_id", postgresql_where=text("status = 'open'")),
        Index("ix_jobs_open_seniority", "seniority", postgresql_where=text("status = 'open'")),
        Index(
            "ix_jobs_salary",
            "salary_currency",
            "salary_period",
            "salary_max",
            postgresql_where=text("salary_max IS NOT NULL OR salary_min IS NOT NULL"),
        ),
        Index("ix_jobs_last_checked", "last_checked_at"),
    )


class JobSkill(Base):
    __tablename__ = "job_skills"

    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    skill: Mapped[str] = mapped_column(Text, primary_key=True)  # canonical name, e.g. "Kubernetes"
    category: Mapped[str] = mapped_column(String(24))  # cloud | language | devops | db | os | …

    __table_args__ = (Index("ix_job_skills_skill", "skill"), Index("ix_job_skills_skill_job", "skill", "job_id"))


class Specialization(Base):
    """Taxonomy (groups and leaves). Seeded from ``career_crawler.extract.taxonomy``; ids are stable."""

    __tablename__ = "specializations"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    slug: Mapped[str] = mapped_column(String(48), unique=True)
    name_en: Mapped[str] = mapped_column(Text)
    name_de: Mapped[str] = mapped_column(Text)
    parent_id: Mapped[int | None] = mapped_column(SmallInteger, ForeignKey("specializations.id"))
    is_it: Mapped[bool] = mapped_column(Boolean, server_default="false")
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")


class JobSpecialization(Base):
    __tablename__ = "job_specializations"

    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    specialization_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("specializations.id", ondelete="CASCADE"), primary_key=True
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, server_default="false")
    confidence: Mapped[float] = mapped_column(Float, server_default="0")

    __table_args__ = (Index("ix_job_specializations_spec", "specialization_id", "job_id"),)


class JobLocation(Base):
    """Work locations of a vacancy (several allowed, cross-border allowed)."""

    __tablename__ = "job_locations"

    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("geo_locations.id"), primary_key=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, server_default="false")
    postcode: Mapped[str | None] = mapped_column(String(10))
    confidence: Mapped[float] = mapped_column(Float, server_default="0.9")
    source: Mapped[str] = mapped_column(String(16), server_default="gazetteer")

    __table_args__ = (Index("ix_job_locations_location", "location_id", "job_id"),)


class JobLanguage(Base):
    """Language requirements. ``level`` is a CEFR level only when the posting wrote one."""

    __tablename__ = "job_languages"

    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    language: Mapped[str] = mapped_column(String(3), primary_key=True)
    level: Mapped[str | None] = mapped_column(String(2))
    level_rank: Mapped[int | None] = mapped_column(SmallInteger)  # A1=1 … C2=6
    requirement: Mapped[str] = mapped_column(String(9), server_default="unknown")
    descriptor: Mapped[str | None] = mapped_column(String(10))
    evidence: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, server_default="0.5")

    __table_args__ = (
        CheckConstraint(_in("requirement", ("required", "preferred", "unknown")), name="requirement"),
        Index("ix_job_languages_lang", "language", "requirement", "level_rank"),
    )


class JobEvent(Base):
    """Durable outbox of vacancy events, read through ``/api/bot/v1/changes``.

    ``id`` order equals commit order (see ``career_crawler.events``). There is deliberately no foreign
    key on ``job_id``: history survives a deleted vacancy.
    """

    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(12))
    job_id: Mapped[int] = mapped_column(BigInteger)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("clock_timestamp()"))
    payload: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))

    __table_args__ = (
        CheckConstraint(_in("event_type", EVENT_TYPES), name="event_type"),
        Index("ix_job_events_job", "job_id", "id"),
    )


class CrawlRun(Base):
    """One company scan: lock (one running per company), heartbeat and checkpoint."""

    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    scan_request_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(10), server_default="running")
    worker: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    heartbeat_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    pages_fetched: Mapped[int] = mapped_column(Integer, server_default="0")
    pages_unchanged: Mapped[int] = mapped_column(Integer, server_default="0")
    jobs_found: Mapped[int] = mapped_column(Integer, server_default="0")
    jobs_new: Mapped[int] = mapped_column(Integer, server_default="0")
    jobs_closed: Mapped[int] = mapped_column(Integer, server_default="0")
    strategy: Mapped[str | None] = mapped_column(String(16))
    complete: Mapped[bool] = mapped_column(Boolean, server_default="false")  # every source fully read
    outcome: Mapped[str | None] = mapped_column(String(32))  # machine code, e.g. jobs_found, no_career_page
    browser_pending: Mapped[bool] = mapped_column(Boolean, server_default="false")  # queue for the browser worker
    browser_urls: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    browser_claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    browser_attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    browser_done_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    llm_urls: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    checkpoint: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    stats: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))

    __table_args__ = (
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        Index("uq_crawl_runs_running", "company_id", unique=True, postgresql_where=text("status = 'running'")),
        Index("ix_crawl_runs_company_started", "company_id", text("started_at DESC")),
        Index("ix_crawl_runs_status_heartbeat", "status", "heartbeat_at"),
        Index("ix_crawl_runs_browser_queue", "id", postgresql_where=text("browser_pending")),
    )


class CrawlPage(Base):
    """Incremental-scan metadata per URL. Normalized text hash by default; raw HTML only for debugging."""

    __tablename__ = "crawl_pages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)  # canonical
    url_hash: Mapped[str] = mapped_column(String(40))
    page_type: Mapped[str | None] = mapped_column(String(24))
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(80))
    content_hash: Mapped[str | None] = mapped_column(String(40))  # sha1 of normalized text
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    strategy: Mapped[str | None] = mapped_column(String(16))
    jobs_found: Mapped[int] = mapped_column(Integer, server_default="0")
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    fetched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    changed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    change_count: Mapped[int] = mapped_column(Integer, server_default="0")
    fetch_ms: Mapped[int | None] = mapped_column(Integer)
    bytes: Mapped[int | None] = mapped_column(Integer)
    raw_path: Mapped[str | None] = mapped_column(Text)  # debugging copy under DATA_DIR/crawl-raw
    raw_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("company_id", "url_hash", name="uq_crawl_pages_company_url"),
        Index("ix_crawl_pages_fetched_at", "fetched_at"),
        Index("ix_crawl_pages_raw_expires", "raw_expires_at", postgresql_where=text("raw_path IS NOT NULL")),
    )


class CrawlMetric(Base):
    """Per-minute counters per worker (accumulated with upserts). Feeds the dashboard."""

    __tablename__ = "crawl_metrics"

    bucket: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    worker: Mapped[str] = mapped_column(String(64), primary_key=True)
    pages: Mapped[int] = mapped_column(Integer, server_default="0")
    bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    requests: Mapped[int] = mapped_column(Integer, server_default="0")
    status_429: Mapped[int] = mapped_column(Integer, server_default="0")
    status_503: Mapped[int] = mapped_column(Integer, server_default="0")
    errors: Mapped[int] = mapped_column(Integer, server_default="0")
    latency_ms_sum: Mapped[int] = mapped_column(BigInteger, server_default="0")
    companies_done: Mapped[int] = mapped_column(Integer, server_default="0")
    jobs_found: Mapped[int] = mapped_column(Integer, server_default="0")
    jobs_new: Mapped[int] = mapped_column(Integer, server_default="0")
    browser_pages: Mapped[int] = mapped_column(Integer, server_default="0")
    by_method: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))  # {"ats": {"ok":1,"fail":0}}
    # last resource sample in this minute
    cpu_pct: Mapped[float | None] = mapped_column(Float)
    mem_available_mb: Mapped[int | None] = mapped_column(Integer)
    container_mem_mb: Mapped[int | None] = mapped_column(Integer)
    http_limit: Mapped[int | None] = mapped_column(Integer)
    http_active: Mapped[int | None] = mapped_column(Integer)
    browser_active: Mapped[int | None] = mapped_column(Integer)
    browser_paused: Mapped[bool | None] = mapped_column(Boolean)


class SiteFingerprint(Base):
    """What we learned about a company website: best strategy, ATS, schedule, rate limits."""

    __tablename__ = "site_fingerprints"

    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    domain: Mapped[str] = mapped_column(Text, index=True)
    best_strategy: Mapped[str | None] = mapped_column(String(16))
    strategy_stats: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    ats_type: Mapped[str | None] = mapped_column(String(32))
    ats_account: Mapped[str | None] = mapped_column(Text)
    career_urls: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    needs_browser: Mapped[bool] = mapped_column(Boolean, server_default="false")
    technologies: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    robots_crawl_delay_s: Mapped[float | None] = mapped_column(Float)
    avg_latency_ms: Mapped[float | None] = mapped_column(Float)
    rate_limited_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_429_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    error_streak: Mapped[int] = mapped_column(Integer, server_default="0")
    scans: Mapped[int] = mapped_column(Integer, server_default="0")
    full_discovery_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_scan_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_change_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_new_job_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    open_jobs: Mapped[int] = mapped_column(Integer, server_default="0")
    interval_h: Mapped[float | None] = mapped_column(Float)
    next_scan_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_site_fingerprints_next_scan", "next_scan_at"),)


class ExtractionError(Base):
    __tablename__ = "extraction_errors"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    run_id: Mapped[int | None] = mapped_column(BigInteger)
    url: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(String(16))  # robots|fetch|discovery|ats|extract|browser|llm|pdf
    kind: Mapped[str] = mapped_column(String(40))  # machine code
    message: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(Integer)
    strategy: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_extraction_errors_company_created", "company_id", text("created_at DESC")),
        Index("ix_extraction_errors_created", "created_at"),
    )
