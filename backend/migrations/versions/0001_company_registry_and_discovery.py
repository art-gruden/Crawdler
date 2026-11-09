"""company registry and discovery

Revision ID: 0001
Revises:
Create Date: 2026-11-09 11:44:44.901552
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_key", sa.Text(), nullable=False),
        sa.Column("alt_names", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("website_url", sa.Text(), nullable=True),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("website_raw", sa.Text(), nullable=True),
        sa.Column("website_rejected_reason", sa.String(length=40), nullable=True),
        sa.Column("verification_status", sa.String(length=32), server_default="unverified", nullable=False),
        sa.Column("verification_confidence", sa.Float(), nullable=True),
        sa.Column(
            "verification_detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("verification_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_verify_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("street", sa.Text(), nullable=True),
        sa.Column("postcode", sa.String(length=10), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("district_key", sa.String(length=12), nullable=True),
        sa.Column("district_name", sa.Text(), nullable=True),
        sa.Column("state_code", sa.String(length=6), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("industries", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("is_manual", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("scan_status", sa.String(length=16), server_default="not_queued", nullable=False),
        sa.Column("queue_after_verify", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "scan_status IN ('not_queued', 'queued', 'scanning', 'done', 'failed')",
            name=op.f("ck_companies_scan_status"),
        ),
        sa.CheckConstraint(
            "verification_status IN ('unverified', 'verified', 'redirected', 'robots_disallowed', 'access_restricted', 'parked', 'http_error', 'rate_limited', 'unreachable', 'dns_error', 'blocked_address', 'robots_unavailable', 'no_website')",
            name=op.f("ck_companies_verification_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companies")),
        sa.UniqueConstraint("domain", name=op.f("uq_companies_domain")),
    )
    op.create_index(op.f("ix_companies_district_key"), "companies", ["district_key"], unique=False)
    op.create_index("ix_companies_first_seen_at", "companies", ["first_seen_at"], unique=False)
    op.create_index("ix_companies_industries", "companies", ["industries"], unique=False, postgresql_using="gin")
    op.create_index(op.f("ix_companies_name_key"), "companies", ["name_key"], unique=False)
    op.create_index("ix_companies_next_verify_at", "companies", ["next_verify_at"], unique=False)
    op.create_index(op.f("ix_companies_state_code"), "companies", ["state_code"], unique=False)
    op.create_table(
        "discovery_jobs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column(
            "params", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "stats", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "errors", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("license", sa.String(length=64), nullable=True),
        sa.Column("attribution", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("data_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column("file_sha256", sa.String(length=64), nullable=True),
        sa.Column("requested_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('osm','csv')", name=op.f("ck_discovery_jobs_kind")),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')", name=op.f("ck_discovery_jobs_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_jobs")),
    )
    op.create_index("ix_discovery_jobs_created_at", "discovery_jobs", ["created_at"], unique=False)
    op.create_table(
        "external_api_usage",
        sa.Column("service", sa.String(length=32), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("requests", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_request_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("service", "day", name=op.f("pk_external_api_usage")),
    )
    op.create_table(
        "company_sources",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_record_id", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("license", sa.String(length=64), nullable=True),
        sa.Column("attribution", sa.Text(), nullable=True),
        sa.Column(
            "data", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("data_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discovery_job_id", sa.BigInteger(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], name=op.f("fk_company_sources_company_id_companies"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_sources")),
        sa.UniqueConstraint("source", "source_record_id", name="uq_company_sources_record"),
    )
    op.create_index(op.f("ix_company_sources_company_id"), "company_sources", ["company_id"], unique=False)
    op.create_index(op.f("ix_company_sources_discovery_job_id"), "company_sources", ["discovery_job_id"], unique=False)
    op.create_table(
        "discovery_job_items",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=True),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("website_raw", sa.Text(), nullable=True),
        sa.Column("source_record_id", sa.Text(), nullable=True),
        sa.Column("region_code", sa.String(length=12), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('new','updated','duplicate_existing','duplicate_in_job','invalid_url','no_website','error')",
            name=op.f("ck_discovery_job_items_outcome"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_discovery_job_items_company_id_companies"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["discovery_jobs.id"],
            name=op.f("fk_discovery_job_items_job_id_discovery_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_job_items")),
    )
    op.create_index("ix_discovery_job_items_company_id", "discovery_job_items", ["company_id"], unique=False)
    op.create_index("ix_discovery_job_items_job_outcome", "discovery_job_items", ["job_id", "outcome"], unique=False)
    op.create_table(
        "scan_requests",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'done', 'failed', 'cancelled')", name=op.f("ck_scan_requests_status")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], name=op.f("fk_scan_requests_company_id_companies"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scan_requests")),
    )
    op.create_index(op.f("ix_scan_requests_company_id"), "scan_requests", ["company_id"], unique=False)
    op.create_index(
        "ix_scan_requests_queue",
        "scan_requests",
        ["status", sa.literal_column("priority DESC"), "requested_at"],
        unique=False,
    )
    op.create_index(
        "uq_scan_requests_open",
        "scan_requests",
        ["company_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'claimed')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_scan_requests_open",
        table_name="scan_requests",
        postgresql_where=sa.text("status IN ('pending', 'claimed')"),
    )
    op.drop_index("ix_scan_requests_queue", table_name="scan_requests")
    op.drop_index(op.f("ix_scan_requests_company_id"), table_name="scan_requests")
    op.drop_table("scan_requests")
    op.drop_index("ix_discovery_job_items_job_outcome", table_name="discovery_job_items")
    op.drop_index("ix_discovery_job_items_company_id", table_name="discovery_job_items")
    op.drop_table("discovery_job_items")
    op.drop_index(op.f("ix_company_sources_discovery_job_id"), table_name="company_sources")
    op.drop_index(op.f("ix_company_sources_company_id"), table_name="company_sources")
    op.drop_table("company_sources")
    op.drop_table("external_api_usage")
    op.drop_index("ix_discovery_jobs_created_at", table_name="discovery_jobs")
    op.drop_table("discovery_jobs")
    op.drop_index(op.f("ix_companies_state_code"), table_name="companies")
    op.drop_index("ix_companies_next_verify_at", table_name="companies")
    op.drop_index(op.f("ix_companies_name_key"), table_name="companies")
    op.drop_index("ix_companies_industries", table_name="companies", postgresql_using="gin")
    op.drop_index("ix_companies_first_seen_at", table_name="companies")
    op.drop_index(op.f("ix_companies_district_key"), table_name="companies")
    op.drop_table("companies")
