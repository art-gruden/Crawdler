"""HTTP API of the company_discovery module."""

from __future__ import annotations

import datetime as dt
import hashlib
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field, computed_field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gcc_core.auth import current_user
from gcc_core.config import Settings, get_settings
from gcc_core.db import SessionLocal, get_db
from gcc_core.urls import display_domain

from . import tasks
from .csv_import import PROFILES, CsvFormatError, inspect_header
from .industries import DEFAULT_IDS, INDUSTRIES, get_industries
from .jobs import import_path
from .models import ITEM_OUTCOMES, DiscoveryJob, DiscoveryJobItem
from .overpass import OSM_ATTRIBUTION, OSM_LICENSE, OverpassClient
from .regions import STATES, expand_leaves, get_region

router = APIRouter(tags=["discovery"], dependencies=[Depends(current_user)])


class OsmJobIn(BaseModel):
    regions: list[str] = Field(default_factory=lambda: ["DE-SN"], min_length=1, max_length=50)
    industries: list[str] = Field(default_factory=lambda: list(DEFAULT_IDS), min_length=1)
    verify: bool = True

    @field_validator("regions")
    @classmethod
    def _regions(cls, v: list[str]) -> list[str]:
        for code in v:
            try:
                get_region(code)
            except KeyError as exc:
                raise ValueError(f"unknown region {code!r}") from exc
        return list(dict.fromkeys(v))


class JobOut(BaseModel):
    id: int
    kind: str
    status: str
    params: dict
    stats: dict
    errors: list
    error: str | None
    cancel_requested: bool
    source: str
    license: str | None
    attribution: str | None
    source_url: str | None
    data_timestamp: dt.datetime | None
    file_name: str | None
    file_sha256: str | None
    requested_by: str | None
    created_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None

    model_config = {"from_attributes": True}


class ItemOut(BaseModel):
    id: int
    company_id: int | None
    outcome: str
    name: str | None
    domain: str | None
    website_raw: str | None
    source_record_id: str | None
    region_code: str | None
    detail: str | None

    model_config = {"from_attributes": True}

    @computed_field
    @property
    def domain_display(self) -> str | None:
        return display_domain(self.domain)


def _active_osm_jobs(db: Session) -> int:
    return db.execute(
        select(func.count()).where(DiscoveryJob.kind == "osm", DiscoveryJob.status.in_(["pending", "running"]))
    ).scalar_one()


# ---------------------------------------------------------------- metadata


@router.get("/api/discovery/regions")
def regions():
    return [s.to_dict() for s in STATES]


@router.get("/api/discovery/industries")
def industries():
    return [{"id": i.id, "label": i.label, "description": i.description, "default": i.default} for i in INDUSTRIES]


@router.get("/api/discovery/import-profiles")
def import_profiles():
    return [{"id": k, **v} for k, v in PROFILES.items()]


@router.get("/api/discovery/overpass/usage")
def overpass_usage(settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    client = OverpassClient(settings, SessionLocal, http=None)
    usage = client.usage_today(db)
    client.http.close()
    usage.update(min_interval_s=settings.overpass_min_interval_s, endpoint=settings.overpass_url)
    return usage


# ---------------------------------------------------------------- jobs


@router.post("/api/discovery/osm", status_code=status.HTTP_202_ACCEPTED, response_model=JobOut)
def start_osm(body: OsmJobIn, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Discover companies with a website in the given regions from OpenStreetMap (Overpass API).

    State codes (``DE-SN``) expand to their Kreise; each Kreis is one Overpass request."""
    try:
        get_industries(body.industries)
    except KeyError as exc:
        raise HTTPException(422, str(exc)) from exc
    if _active_osm_jobs(db) >= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "An OpenStreetMap discovery job is already running")
    leaves = expand_leaves(body.regions)
    job = DiscoveryJob(
        kind="osm",
        params={
            "regions": body.regions,
            "region_names": [get_region(r).name for r in body.regions],
            "leaf_count": len(leaves),
            "industries": body.industries,
            "verify": body.verify,
        },
        source="osm",
        license=OSM_LICENSE,
        attribution=OSM_ATTRIBUTION,
        source_url="https://www.openstreetmap.org/",
        requested_by=user,
    )
    db.add(job)
    db.commit()
    tasks.run_osm_job.delay(job.id)
    db.refresh(job)
    return job


_SLUG = re.compile(r"[^a-z0-9]+")


@router.post("/api/companies/import", status_code=status.HTTP_202_ACCEPTED, response_model=JobOut, tags=["companies"])
async def import_companies(
    file: UploadFile = File(..., description="CSV file, UTF-8 or Windows-1252, delimiter , ; tab or |"),
    profile: str = Form("generic"),
    source_name: str | None = Form(None, max_length=80, description="Directory name, e.g. 'IHK Dresden'"),
    license: str | None = Form(None, max_length=64),
    attribution: str | None = Form(None, max_length=500),
    source_url: str | None = Form(None, max_length=500),
    verify: bool = Form(True),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
    user: str = Depends(current_user),
):
    if profile not in PROFILES:
        raise HTTPException(422, f"Unknown profile {profile!r}")
    limit = settings.import_max_mb * 1024 * 1024
    raw = await file.read(limit + 1)
    if len(raw) > limit:
        raise HTTPException(413, f"File larger than {settings.import_max_mb} MB")
    try:
        headers, mapping = inspect_header(raw)
    except CsvFormatError as exc:
        raise HTTPException(422, str(exc)) from exc
    prof = PROFILES[profile]
    if source_name and source_name.strip():
        slug = _SLUG.sub("-", source_name.strip().lower()).strip("-")[:48] or "unnamed"
        source = f"csv:{slug}"
        use_ids = True
    else:
        source = prof["source"]
        use_ids = profile != "generic"  # generic IDs from different files would collide
    job = DiscoveryJob(
        kind="csv",
        params={
            "profile": profile,
            "columns": {k: headers[v] for k, v in mapping.items()},
            "verify": verify,
            "use_source_ids": use_ids,
            "source_name": (source_name or prof["label"]).strip(),
        },
        source=source,
        license=(license or prof["license"] or None),
        attribution=(attribution or prof["attribution"] or None),
        source_url=(source_url or prof["source_url"] or None),
        file_name=(file.filename or "upload.csv")[:200],
        file_sha256=hashlib.sha256(raw).hexdigest(),
        requested_by=user,
    )
    db.add(job)
    db.flush()
    path = import_path(job.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    db.commit()
    tasks.run_csv_job.delay(job.id)
    db.refresh(job)
    return job


@router.get("/api/discovery/jobs", response_model=list[JobOut])
def list_jobs(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    return db.execute(select(DiscoveryJob).order_by(DiscoveryJob.id.desc()).limit(limit)).scalars().all()


@router.get("/api/discovery/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(DiscoveryJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job


@router.post("/api/discovery/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: int, db: Session = Depends(get_db)):
    job = db.execute(select(DiscoveryJob).where(DiscoveryJob.id == job_id).with_for_update()).scalar_one_or_none()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    if job.status in ("pending", "running"):
        job.cancel_requested = True
        if job.status == "pending":
            job.status = "cancelled"
            job.finished_at = dt.datetime.now(dt.UTC)
    db.commit()
    db.refresh(job)
    return job


@router.get("/api/discovery/jobs/{job_id}/items")
def job_items(
    job_id: int,
    outcome: str | None = Query(
        None, description="new, duplicate (both kinds), invalid (invalid_url+no_website) or an exact outcome"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    stmt = select(DiscoveryJobItem).where(DiscoveryJobItem.job_id == job_id)
    groups = {
        "duplicate": ["duplicate_existing", "duplicate_in_job"],
        "invalid": ["invalid_url", "no_website"],
    }
    if outcome:
        values = groups.get(outcome, [outcome])
        if not set(values) <= set(ITEM_OUTCOMES):
            raise HTTPException(422, f"unknown outcome {outcome!r}")
        stmt = stmt.where(DiscoveryJobItem.outcome.in_(values))
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    counts = dict(
        db.execute(
            select(DiscoveryJobItem.outcome, func.count())
            .where(DiscoveryJobItem.job_id == job_id)
            .group_by(DiscoveryJobItem.outcome)
        ).all()
    )
    rows = db.execute(stmt.order_by(DiscoveryJobItem.id).offset((page - 1) * page_size).limit(page_size)).scalars()
    return {
        "items": [ItemOut.model_validate(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "counts": counts,
    }
