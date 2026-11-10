"""ATS scan diagnostics on career_sources (strategy, completeness, provider total, details)

Additive only: six nullable columns; nothing is rewritten, no data migration. Downgrade drops them.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-10 14:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('career_sources', sa.Column('scan_strategy', sa.String(length=32), nullable=True))
    op.add_column('career_sources', sa.Column('scan_complete', sa.Boolean(), nullable=True))
    op.add_column('career_sources', sa.Column('reported_total', sa.Integer(), nullable=True))
    op.add_column('career_sources', sa.Column('scan_detail', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('career_sources', sa.Column('last_scan_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('career_sources', sa.Column('last_complete_scan_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for col in ('last_complete_scan_at', 'last_scan_at', 'scan_detail', 'reported_total', 'scan_complete', 'scan_strategy'):
        op.drop_column('career_sources', col)
