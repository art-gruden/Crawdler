"""Shared tables used by every module.

The ``companies`` table is the hand-off point between modules: discovery writes it,
the career crawler reads it. ``scan_requests`` is the queue (outbox) the crawler
consumes, so discovery never imports crawler code.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

VERIFICATION_STATUSES = (
    "unverified",  # not checked yet
    "verified",  # homepage reachable, allowed by robots.txt, same (or adopted) domain
    "redirected",  # redirects to another company's domain or to a non-company site (social network …)
    "robots_disallowed",  # robots.txt forbids our user agent; never crawl
    "access_restricted",  # 401/403, CAPTCHA or bot challenge; never bypassed
    "parked",  # domain parking / for-sale page
    "http_error",  # 4xx/5xx other than the above
    "rate_limited",  # 429; retried later
    "unreachable",  # timeout, TLS or connection error
    "dns_error",  # host does not resolve
    "blocked_address",  # resolves to a private/reserved address (SSRF guard)
    "robots_unavailable",  # robots.txt answered 5xx; retried later
    "no_website",  # company has no usable website
)
RETRYABLE_VERIFICATION = ("rate_limited", "unreachable", "robots_unavailable", "http_error", "dns_error")
SCAN_STATUSES = ("not_queued", "queued", "scanning", "done", "failed")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    name_key: Mapped[str] = mapped_column(Text, index=True)  # normalized for fallback dedup
    alt_names: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))

    website_url: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(Text, unique=True)  # registrable domain = dedup key
    website_raw: Mapped[str | None] = mapped_column(Text)  # as delivered by the first source
    website_rejected_reason: Mapped[str | None] = mapped_column(String(40))

    verification_status: Mapped[str] = mapped_column(String(32), server_default="unverified")
    verification_confidence: Mapped[float | None] = mapped_column(Float)
    verification_detail: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    verification_attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    next_verify_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    street: Mapped[str | None] = mapped_column(Text)
    postcode: Mapped[str | None] = mapped_column(String(10))
    city: Mapped[str | None] = mapped_column(Text)
    district_key: Mapped[str | None] = mapped_column(String(12), index=True)  # Kreisschlüssel, e.g. 14612
    district_name: Mapped[str | None] = mapped_column(Text)
    state_code: Mapped[str | None] = mapped_column(String(6), index=True)  # ISO 3166-2, e.g. DE-SN
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)

    industries: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    is_manual: Mapped[bool] = mapped_column(Boolean, server_default="false")
    notes: Mapped[str | None] = mapped_column(Text)

    scan_status: Mapped[str] = mapped_column(String(16), server_default="not_queued")
    queue_after_verify: Mapped[bool] = mapped_column(Boolean, server_default="false")
    last_scan_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    sources: Mapped[list[CompanySource]] = relationship(back_populates="company", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(_in("verification_status", VERIFICATION_STATUSES), name="verification_status"),
        CheckConstraint(_in("scan_status", SCAN_STATUSES), name="scan_status"),
        Index("ix_companies_industries", "industries", postgresql_using="gin"),
        Index("ix_companies_next_verify_at", "next_verify_at"),
        Index("ix_companies_first_seen_at", "first_seen_at"),
    )


class CompanySource(Base):
    """Provenance: one row per (source, record) that contributed to a company."""

    __tablename__ = "company_sources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(64))  # osm | csv:generic | csv:standort_sachsen | manual
    source_record_id: Mapped[str] = mapped_column(Text)  # e.g. node/123456 or a stable row hash
    source_url: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)  # name as given by this source
    license: Mapped[str | None] = mapped_column(String(64))
    attribution: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))  # whitelisted fields only
    data_timestamp: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))  # e.g. OSM base time
    discovery_job_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    company: Mapped[Company] = relationship(back_populates="sources")

    __table_args__ = (UniqueConstraint("source", "source_record_id", name="uq_company_sources_record"),)


class ScanRequest(Base):
    """Outbox for the career crawler. Discovery and the dashboard insert, the crawler claims."""

    __tablename__ = "scan_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    priority: Mapped[int] = mapped_column(Integer, server_default="0")
    source: Mapped[str] = mapped_column(String(32))  # discovery | manual | schedule
    requested_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(_in("status", ("pending", "claimed", "done", "failed", "cancelled")), name="status"),
        # At most one open request per company.
        Index(
            "uq_scan_requests_open",
            "company_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'claimed')"),
        ),
        Index("ix_scan_requests_queue", "status", text("priority DESC"), "requested_at"),
    )


class ExternalApiUsage(Base):
    """Daily request/byte counters for rate-limited external services (Overpass)."""

    __tablename__ = "external_api_usage"

    service: Mapped[str] = mapped_column(String(32), primary_key=True)
    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    requests: Mapped[int] = mapped_column(Integer, server_default="0")
    bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    last_request_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
