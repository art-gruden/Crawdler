"""Tables owned by the company_discovery module."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from gcc_core.db import Base

JOB_STATUSES = ("pending", "running", "completed", "failed", "cancelled")
ITEM_OUTCOMES = ("new", "updated", "duplicate_existing", "duplicate_in_job", "invalid_url", "no_website", "error")


class DiscoveryJob(Base):
    __tablename__ = "discovery_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # osm | csv
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    params: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    stats: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    errors: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))  # first N row/step errors
    error: Mapped[str | None] = mapped_column(Text)  # fatal error
    cancel_requested: Mapped[bool] = mapped_column(server_default="false")
    # Provenance of the whole job
    source: Mapped[str] = mapped_column(String(64))
    license: Mapped[str | None] = mapped_column(String(64))
    attribution: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    data_timestamp: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    file_name: Mapped[str | None] = mapped_column(Text)
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    requested_by: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('pending','running','completed','failed','cancelled')", name="status"),
        CheckConstraint("kind IN ('osm','csv')", name="kind"),
        Index("ix_discovery_jobs_created_at", "created_at"),
    )


class DiscoveryJobItem(Base):
    """What a job did with each input record: lets the dashboard show new vs. duplicate domains."""

    __tablename__ = "discovery_job_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("discovery_jobs.id", ondelete="CASCADE"))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))
    outcome: Mapped[str] = mapped_column(String(24))
    name: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(Text)
    website_raw: Mapped[str | None] = mapped_column(Text)
    source_record_id: Mapped[str | None] = mapped_column(Text)
    region_code: Mapped[str | None] = mapped_column(String(12))
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('new','updated','duplicate_existing','duplicate_in_job','invalid_url','no_website','error')",
            name="outcome",
        ),
        Index("ix_discovery_job_items_job_outcome", "job_id", "outcome"),
        Index("ix_discovery_job_items_company_id", "company_id"),
    )
