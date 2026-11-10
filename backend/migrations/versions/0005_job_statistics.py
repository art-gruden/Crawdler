"""Vacancy statistics: indexes for the dashboard queries and the Telegram publication history

Additive only. Two indexes (job_events by type+time, jobs by company+discovered_at) and one new table
(stat_publications). No data is rewritten. Downgrade drops them again.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-10 18:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index('ix_job_events_type_time', 'job_events', ['event_type', 'occurred_at'])
    op.create_index('ix_jobs_company_discovered', 'jobs', ['company_id', 'discovered_at'])
    op.create_table(
        'stat_publications',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('dedup_key', sa.String(length=64), nullable=False),
        sa.Column('channel', sa.String(length=64), nullable=False),
        sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('period_end', sa.DateTime(timezone=True), nullable=False),
        sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('text_sha256', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=8), server_default='pending', nullable=False),
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=True),
        sa.Column('response', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'sent', 'failed')", name=op.f('ck_stat_publications_status')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_stat_publications')),
        sa.UniqueConstraint('dedup_key', name=op.f('uq_stat_publications_dedup_key')),
    )
    op.create_index('ix_stat_publications_created', 'stat_publications', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_stat_publications_created', table_name='stat_publications')
    op.drop_table('stat_publications')
    op.drop_index('ix_jobs_company_discovered', table_name='jobs')
    op.drop_index('ix_job_events_type_time', table_name='job_events')
