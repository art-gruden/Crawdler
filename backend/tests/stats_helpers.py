"""Seeding for the statistics tests: vacancies with exact discovery times, places and specializations."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from career_crawler.models import Job, JobEvent, JobLocation, JobSpecialization, Specialization
from gcc_core.models import Company, GeoLocation

UTC = dt.UTC


def utc(y, m, d, h=0, mi=0) -> dt.datetime:
    return dt.datetime(y, m, d, h, mi, tzinfo=UTC)


def spec_id(db, slug: str) -> int:
    return db.execute(select(Specialization.id).where(Specialization.slug == slug)).scalar_one()


def place(db, city: str, country: str | None = None) -> GeoLocation:
    q = select(GeoLocation).where(GeoLocation.city == city)
    if country:
        q = q.where(GeoLocation.country_code == country)
    return db.execute(q).scalars().first()


def add_job(
    db,
    co: Company,
    n: int,
    *,
    at: dt.datetime,
    cities: tuple[str, ...] = (),
    country: str | None = None,
    spec: str | None = None,
    secondary: tuple[str, ...] = (),
    status: str = "open",
    work_model: str = "ONSITE",
    event: bool = True,
    posted: dt.date | None = None,
    dedup_key: str | None = None,
    countries: tuple[str, ...] | None = None,
) -> Job:
    """Insert one canonical vacancy discovered at ``at``. ``cities`` are gazetteer city names."""
    places = [place(db, c, country) for c in cities]
    assert all(places), f"gazetteer has no {cities}"
    codes = list(countries) if countries is not None else sorted({p.country_code for p in places})
    if not places and countries is None and country:
        codes = [country]
    j = Job(
        company_id=co.id,
        dedup_key=dedup_key or f"{co.id}:test:{n}",
        title=f"Job {n}",
        title_norm=f"job {n}",
        source_url=f"https://{co.domain}/jobs/{n}",
        extraction_method="html",
        status=status,
        discovered_at=at,
        last_seen_at=at,
        date_posted=posted,
        country_codes=codes,
        region_codes=sorted({p.region_code for p in places if p.region_code}),
        location_ids=[p.id for p in places],
        work_model=work_model,
        specialization_id=spec_id(db, spec) if spec else None,
        geo_status="resolved" if places else "unknown",
    )
    db.add(j)
    db.flush()
    for i, p in enumerate(places):
        db.add(JobLocation(job_id=j.id, location_id=p.id, is_primary=(i == 0)))
    if spec:
        db.add(JobSpecialization(job_id=j.id, specialization_id=j.specialization_id, is_primary=True))
    for s in secondary:
        db.add(JobSpecialization(job_id=j.id, specialization_id=spec_id(db, s), is_primary=False))
    if event:
        db.add(JobEvent(event_type="job.created", job_id=j.id, occurred_at=at))
    db.flush()
    return j


def add_event(db, job: Job, kind: str, at: dt.datetime) -> None:
    db.add(JobEvent(event_type=f"job.{kind}", job_id=job.id, occurred_at=at))
    db.flush()
