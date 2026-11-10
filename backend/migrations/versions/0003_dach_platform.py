"""DACH job data platform: geography, taxonomy, normalized vacancy fields, event outbox

Adds only new tables/columns/indexes (existing columns and values are untouched) and then seeds the
reference data and derives the new fields for vacancies stored so far (idempotent, no events).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-10 09:09:45.801232
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('geo_countries',
    sa.Column('code', sa.String(length=2), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('name_local', sa.Text(), nullable=False),
    sa.Column('in_dach', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('aliases', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.PrimaryKeyConstraint('code', name=op.f('pk_geo_countries'))
    )
    op.create_table('job_events',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('event_type', sa.String(length=12), nullable=False),
    sa.Column('job_id', sa.BigInteger(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.CheckConstraint("event_type IN ('job.created', 'job.updated', 'job.closed', 'job.reopened')", name=op.f('ck_job_events_event_type')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_job_events'))
    )
    op.create_index('ix_job_events_job', 'job_events', ['job_id', 'id'], unique=False)
    op.create_table('specializations',
    sa.Column('id', sa.SmallInteger(), autoincrement=False, nullable=False),
    sa.Column('slug', sa.String(length=48), nullable=False),
    sa.Column('name_en', sa.Text(), nullable=False),
    sa.Column('name_de', sa.Text(), nullable=False),
    sa.Column('parent_id', sa.SmallInteger(), nullable=True),
    sa.Column('is_it', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['parent_id'], ['specializations.id'], name=op.f('fk_specializations_parent_id_specializations')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_specializations')),
    sa.UniqueConstraint('slug', name=op.f('uq_specializations_slug'))
    )
    op.create_table('geo_regions',
    sa.Column('code', sa.String(length=8), nullable=False),
    sa.Column('country_code', sa.String(length=2), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('name_local', sa.Text(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('aliases', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.ForeignKeyConstraint(['country_code'], ['geo_countries.code'], name=op.f('fk_geo_regions_country_code_geo_countries')),
    sa.PrimaryKeyConstraint('code', name=op.f('pk_geo_regions'))
    )
    op.create_index(op.f('ix_geo_regions_country_code'), 'geo_regions', ['country_code'], unique=False)
    op.create_table('geo_locations',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('loc_key', sa.Text(), nullable=False),
    sa.Column('country_code', sa.String(length=2), nullable=False),
    sa.Column('region_code', sa.String(length=8), nullable=True),
    sa.Column('city', sa.Text(), nullable=True),
    sa.Column('search_name', sa.Text(), server_default='', nullable=False),
    sa.Column('postcode', sa.String(length=10), nullable=True),
    sa.Column('lat', sa.Float(), nullable=True),
    sa.Column('lon', sa.Float(), nullable=True),
    sa.Column('aliases', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('source', sa.String(length=24), server_default='gazetteer', nullable=False),
    sa.Column('confidence', sa.Float(), server_default='1', nullable=False),
    sa.ForeignKeyConstraint(['country_code'], ['geo_countries.code'], name=op.f('fk_geo_locations_country_code_geo_countries')),
    sa.ForeignKeyConstraint(['region_code'], ['geo_regions.code'], name=op.f('fk_geo_locations_region_code_geo_regions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_geo_locations')),
    sa.UniqueConstraint('loc_key', name=op.f('uq_geo_locations_loc_key'))
    )
    op.create_index('ix_geo_locations_country_region', 'geo_locations', ['country_code', 'region_code'], unique=False)
    op.create_index('ix_geo_locations_search_name', 'geo_locations', ['search_name'], unique=False, postgresql_ops={'search_name': 'text_pattern_ops'})
    op.create_table('company_locations',
    sa.Column('company_id', sa.BigInteger(), nullable=False),
    sa.Column('location_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('source', sa.String(length=24), server_default='company', nullable=False),
    sa.Column('confidence', sa.Float(), server_default='0.9', nullable=False),
    sa.CheckConstraint("kind IN ('hq', 'office')", name=op.f('ck_company_locations_kind')),
    sa.ForeignKeyConstraint(['company_id'], ['companies.id'], name=op.f('fk_company_locations_company_id_companies'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['location_id'], ['geo_locations.id'], name=op.f('fk_company_locations_location_id_geo_locations')),
    sa.PrimaryKeyConstraint('company_id', 'location_id', 'kind', name=op.f('pk_company_locations'))
    )
    op.create_index('ix_company_locations_location', 'company_locations', ['location_id', 'company_id'], unique=False)
    op.create_table('job_languages',
    sa.Column('job_id', sa.BigInteger(), nullable=False),
    sa.Column('language', sa.String(length=3), nullable=False),
    sa.Column('level', sa.String(length=2), nullable=True),
    sa.Column('level_rank', sa.SmallInteger(), nullable=True),
    sa.Column('requirement', sa.String(length=9), server_default='unknown', nullable=False),
    sa.Column('descriptor', sa.String(length=10), nullable=True),
    sa.Column('evidence', sa.Text(), nullable=True),
    sa.Column('confidence', sa.Float(), server_default='0.5', nullable=False),
    sa.CheckConstraint("requirement IN ('required', 'preferred', 'unknown')", name=op.f('ck_job_languages_requirement')),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], name=op.f('fk_job_languages_job_id_jobs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('job_id', 'language', name=op.f('pk_job_languages'))
    )
    op.create_index('ix_job_languages_lang', 'job_languages', ['language', 'requirement', 'level_rank'], unique=False)
    op.create_table('job_locations',
    sa.Column('job_id', sa.BigInteger(), nullable=False),
    sa.Column('location_id', sa.BigInteger(), nullable=False),
    sa.Column('is_primary', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('postcode', sa.String(length=10), nullable=True),
    sa.Column('confidence', sa.Float(), server_default='0.9', nullable=False),
    sa.Column('source', sa.String(length=16), server_default='gazetteer', nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], name=op.f('fk_job_locations_job_id_jobs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['location_id'], ['geo_locations.id'], name=op.f('fk_job_locations_location_id_geo_locations')),
    sa.PrimaryKeyConstraint('job_id', 'location_id', name=op.f('pk_job_locations'))
    )
    op.create_index('ix_job_locations_location', 'job_locations', ['location_id', 'job_id'], unique=False)
    op.create_table('job_specializations',
    sa.Column('job_id', sa.BigInteger(), nullable=False),
    sa.Column('specialization_id', sa.SmallInteger(), nullable=False),
    sa.Column('is_primary', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('confidence', sa.Float(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], name=op.f('fk_job_specializations_job_id_jobs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['specialization_id'], ['specializations.id'], name=op.f('fk_job_specializations_specialization_id_specializations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('job_id', 'specialization_id', name=op.f('pk_job_specializations'))
    )
    op.create_index('ix_job_specializations_spec', 'job_specializations', ['specialization_id', 'job_id'], unique=False)
    op.add_column('companies', sa.Column('country_code', sa.String(length=2), nullable=True))
    op.create_index(op.f('ix_companies_country_code'), 'companies', ['country_code'], unique=False)
    op.create_index('ix_job_skills_skill_job', 'job_skills', ['skill', 'job_id'], unique=False)
    op.add_column('jobs', sa.Column('salary_basis', sa.String(length=7), server_default='unknown', nullable=False))
    op.add_column('jobs', sa.Column('salary_confidence', sa.Float(), nullable=True))
    op.add_column('jobs', sa.Column('geo_status', sa.String(length=9), server_default='unknown', nullable=False))
    op.add_column('jobs', sa.Column('geo_detail', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.add_column('jobs', sa.Column('country_codes', postgresql.ARRAY(sa.String(length=2)), server_default=sa.text("'{}'"), nullable=False))
    op.add_column('jobs', sa.Column('region_codes', postgresql.ARRAY(sa.String(length=8)), server_default=sa.text("'{}'"), nullable=False))
    op.add_column('jobs', sa.Column('location_ids', postgresql.ARRAY(sa.BigInteger()), server_default=sa.text("'{}'"), nullable=False))
    op.add_column('jobs', sa.Column('work_model', sa.String(length=7), server_default='UNKNOWN', nullable=False))
    op.add_column('jobs', sa.Column('remote_scope', sa.String(length=9), server_default='unknown', nullable=False))
    op.add_column('jobs', sa.Column('remote_countries', postgresql.ARRAY(sa.String(length=2)), server_default=sa.text("'{}'"), nullable=False))
    op.add_column('jobs', sa.Column('employment_kinds', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{UNKNOWN}'"), nullable=False))
    op.add_column('jobs', sa.Column('specialization_id', sa.SmallInteger(), nullable=True))
    op.add_column('jobs', sa.Column('specialization_confidence', sa.Float(), nullable=True))
    op.add_column('jobs', sa.Column('seniority', sa.String(length=9), server_default='UNKNOWN', nullable=False))
    op.add_column('jobs', sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('jobs', sa.Column('description_hash', sa.String(length=40), nullable=True))
    op.add_column('jobs', sa.Column('quality_score', sa.Float(), nullable=True))
    op.add_column('jobs', sa.Column('quality', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.add_column('jobs', sa.Column('norm_version', sa.SmallInteger(), server_default='1', nullable=False))
    op.create_index('ix_jobs_country_codes', 'jobs', ['country_codes'], unique=False, postgresql_using='gin')
    op.create_index('ix_jobs_employment_kinds', 'jobs', ['employment_kinds'], unique=False, postgresql_using='gin')
    op.create_index('ix_jobs_last_checked', 'jobs', ['last_checked_at'], unique=False)
    op.create_index('ix_jobs_location_ids', 'jobs', ['location_ids'], unique=False, postgresql_using='gin')
    op.create_index('ix_jobs_open_first_seen', 'jobs', [sa.literal_column('discovered_at DESC'), sa.literal_column('id DESC')], unique=False, postgresql_where=sa.text("status = 'open'"))
    op.create_index('ix_jobs_open_seniority', 'jobs', ['seniority'], unique=False, postgresql_where=sa.text("status = 'open'"))
    op.create_index('ix_jobs_open_specialization', 'jobs', ['specialization_id'], unique=False, postgresql_where=sa.text("status = 'open'"))
    op.create_index('ix_jobs_open_work_model', 'jobs', ['work_model'], unique=False, postgresql_where=sa.text("status = 'open'"))
    op.create_index('ix_jobs_region_codes', 'jobs', ['region_codes'], unique=False, postgresql_using='gin')
    op.create_index('ix_jobs_remote_countries', 'jobs', ['remote_countries'], unique=False, postgresql_using='gin')
    op.create_index('ix_jobs_salary', 'jobs', ['salary_currency', 'salary_period', 'salary_max'], unique=False, postgresql_where=sa.text('salary_max IS NOT NULL OR salary_min IS NOT NULL'))
    op.create_foreign_key(op.f('fk_jobs_specialization_id_specializations'), 'jobs', 'specializations', ['specialization_id'], ['id'], ondelete='SET NULL')
    op.create_check_constraint(op.f('ck_jobs_work_model'), 'jobs', "work_model IN ('REMOTE', 'HYBRID', 'ONSITE', 'UNKNOWN')")
    op.create_check_constraint(op.f('ck_jobs_geo_status'), 'jobs', "geo_status IN ('resolved', 'partial', 'ambiguous', 'unknown')")
    op.create_check_constraint(
        op.f('ck_jobs_seniority'), 'jobs', "seniority IN ('INTERN', 'JUNIOR', 'MID', 'SENIOR', 'LEAD', 'EXECUTIVE', 'UNKNOWN')"
    )

    # Reference data (countries, regions, gazetteer, taxonomy) and the backfill of existing rows.
    from career_crawler.backfill import run_all

    run_all(op.get_bind())


def downgrade() -> None:
    op.drop_constraint(op.f('ck_jobs_seniority'), 'jobs', type_='check')
    op.drop_constraint(op.f('ck_jobs_geo_status'), 'jobs', type_='check')
    op.drop_constraint(op.f('ck_jobs_work_model'), 'jobs', type_='check')
    op.drop_constraint(op.f('fk_jobs_specialization_id_specializations'), 'jobs', type_='foreignkey')
    op.drop_index('ix_jobs_salary', table_name='jobs', postgresql_where=sa.text('salary_max IS NOT NULL OR salary_min IS NOT NULL'))
    op.drop_index('ix_jobs_remote_countries', table_name='jobs', postgresql_using='gin')
    op.drop_index('ix_jobs_region_codes', table_name='jobs', postgresql_using='gin')
    op.drop_index('ix_jobs_open_work_model', table_name='jobs', postgresql_where=sa.text("status = 'open'"))
    op.drop_index('ix_jobs_open_specialization', table_name='jobs', postgresql_where=sa.text("status = 'open'"))
    op.drop_index('ix_jobs_open_seniority', table_name='jobs', postgresql_where=sa.text("status = 'open'"))
    op.drop_index('ix_jobs_open_first_seen', table_name='jobs', postgresql_where=sa.text("status = 'open'"))
    op.drop_index('ix_jobs_location_ids', table_name='jobs', postgresql_using='gin')
    op.drop_index('ix_jobs_last_checked', table_name='jobs')
    op.drop_index('ix_jobs_employment_kinds', table_name='jobs', postgresql_using='gin')
    op.drop_index('ix_jobs_country_codes', table_name='jobs', postgresql_using='gin')
    op.drop_column('jobs', 'norm_version')
    op.drop_column('jobs', 'quality')
    op.drop_column('jobs', 'quality_score')
    op.drop_column('jobs', 'description_hash')
    op.drop_column('jobs', 'last_checked_at')
    op.drop_column('jobs', 'seniority')
    op.drop_column('jobs', 'specialization_confidence')
    op.drop_column('jobs', 'specialization_id')
    op.drop_column('jobs', 'employment_kinds')
    op.drop_column('jobs', 'remote_countries')
    op.drop_column('jobs', 'remote_scope')
    op.drop_column('jobs', 'work_model')
    op.drop_column('jobs', 'location_ids')
    op.drop_column('jobs', 'region_codes')
    op.drop_column('jobs', 'country_codes')
    op.drop_column('jobs', 'geo_detail')
    op.drop_column('jobs', 'geo_status')
    op.drop_column('jobs', 'salary_confidence')
    op.drop_column('jobs', 'salary_basis')
    op.drop_index('ix_job_skills_skill_job', table_name='job_skills')
    op.drop_index(op.f('ix_companies_country_code'), table_name='companies')
    op.drop_column('companies', 'country_code')
    op.drop_index('ix_job_specializations_spec', table_name='job_specializations')
    op.drop_table('job_specializations')
    op.drop_index('ix_job_locations_location', table_name='job_locations')
    op.drop_table('job_locations')
    op.drop_index('ix_job_languages_lang', table_name='job_languages')
    op.drop_table('job_languages')
    op.drop_index('ix_company_locations_location', table_name='company_locations')
    op.drop_table('company_locations')
    op.drop_index('ix_geo_locations_search_name', table_name='geo_locations', postgresql_ops={'search_name': 'text_pattern_ops'})
    op.drop_index('ix_geo_locations_country_region', table_name='geo_locations')
    op.drop_table('geo_locations')
    op.drop_index(op.f('ix_geo_regions_country_code'), table_name='geo_regions')
    op.drop_table('geo_regions')
    op.drop_table('specializations')
    op.drop_index('ix_job_events_job', table_name='job_events')
    op.drop_table('job_events')
    op.drop_table('geo_countries')
