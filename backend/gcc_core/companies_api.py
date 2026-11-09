"""Company registry API (shared by all modules): list, detail, export, queue for scanning."""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload

from . import scan_queue
from .auth import current_user
from .db import get_db
from .models import RETRYABLE_VERIFICATION, SCAN_STATUSES, VERIFICATION_STATUSES, Company, CompanySource
from .urls import display_domain

router = APIRouter(prefix="/api/companies", tags=["companies"], dependencies=[Depends(current_user)])

SORTS = {
    "name": func.lower(Company.name),
    "domain": Company.domain,
    "first_seen": Company.first_seen_at,
    "last_seen": Company.last_seen_at,
    "city": func.lower(Company.city),
    "verification": Company.verification_status,
}

OSM_NOTICE = (
    "Contains information from OpenStreetMap (c) OpenStreetMap contributors, ODbL 1.0; see licenses column per row"
)


class SourceOut(BaseModel):
    source: str
    source_record_id: str
    source_url: str | None
    name: str | None
    license: str | None
    attribution: str | None
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime
    data_timestamp: dt.datetime | None
    discovery_job_id: int | None
    data: dict | None = None

    model_config = {"from_attributes": True}


class CompanyOut(BaseModel):
    id: int
    name: str
    website_url: str | None
    domain: str | None
    domain_display: str | None = None
    website_raw: str | None
    website_rejected_reason: str | None
    verification_status: str
    verification_confidence: float | None
    verified_at: dt.datetime | None
    street: str | None
    postcode: str | None
    city: str | None
    district_key: str | None
    district_name: str | None
    state_code: str | None
    lat: float | None
    lon: float | None
    industries: list[str]
    scan_status: str
    last_scan_at: dt.datetime | None
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime
    sources: list[str] = []


class CompanyDetail(CompanyOut):
    alt_names: list[str]
    verification_detail: dict
    source_records: list[SourceOut]


class Page(BaseModel):
    items: list[CompanyOut]
    total: int
    page: int
    page_size: int


class ScanBulkIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=1000)


def _filters(
    q: str | None = Query(None, max_length=200, description="Search in name, domain, city"),
    region: str | None = Query(None, description="Kreisschlüssel (e.g. 14612) or ISO 3166-2 state code"),
    industry: str | None = Query(None),
    verification: str | None = Query(None, description="Comma-separated verification statuses"),
    scan_status: str | None = Query(None),
    source: str | None = Query(None, description="osm, csv:standort_sachsen, …; prefix match"),
    job_id: int | None = Query(None, description="Only companies touched by this discovery job"),
    new_in_job: bool = Query(False, description="With job_id: only companies created by that job"),
) -> dict:
    return locals()


def build_query(f: dict) -> Select:
    stmt = select(Company)
    if f["q"]:
        like = f"%{f['q'].strip().lower()}%"
        stmt = stmt.where(
            or_(func.lower(Company.name).like(like), Company.domain.like(like), func.lower(Company.city).like(like))
        )
    if f["region"]:
        if f["region"].startswith("DE-"):
            stmt = stmt.where(Company.state_code == f["region"])
        else:
            stmt = stmt.where(Company.district_key == f["region"])
    if f["industry"]:
        stmt = stmt.where(Company.industries.contains([f["industry"]]))
    if f["verification"]:
        values = [v for v in f["verification"].split(",") if v in VERIFICATION_STATUSES]
        stmt = stmt.where(Company.verification_status.in_(values))
    if f["scan_status"]:
        values = [v for v in f["scan_status"].split(",") if v in SCAN_STATUSES]
        stmt = stmt.where(Company.scan_status.in_(values))
    if f["source"]:
        sub = select(CompanySource.company_id).where(CompanySource.source.like(f["source"].replace("%", "") + "%"))
        stmt = stmt.where(Company.id.in_(sub))
    if f["job_id"] is not None:
        from sqlalchemy import text

        outcome = "AND outcome = 'new'" if f["new_in_job"] else ""
        stmt = stmt.where(
            Company.id.in_(
                select(text("company_id"))
                .select_from(text("discovery_job_items"))
                .where(text(f"job_id = :job_id {outcome}"))
                .params(job_id=f["job_id"])
            )
        )
    return stmt


def to_out(c: Company, sources: list[str], model=None, **extra):
    model = model or CompanyOut
    data = {name: getattr(c, name) for name in CompanyOut.model_fields if name not in ("sources", "domain_display")}
    return model.model_validate({**data, "sources": sources, "domain_display": display_domain(c.domain), **extra})


def _source_names(db: Session, ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    if not ids:
        return out
    rows = db.execute(
        select(CompanySource.company_id, CompanySource.source)
        .where(CompanySource.company_id.in_(ids))
        .distinct()
        .order_by(CompanySource.company_id, CompanySource.source)
    )
    for cid, src in rows:
        out.setdefault(cid, []).append(src)
    return out


@router.get("", response_model=Page)
def list_companies(
    f: dict = Depends(_filters),
    sort: str = Query("first_seen", pattern="^(" + "|".join(SORTS) + ")$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    stmt = build_query(f)
    total = db.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar_one()
    col = SORTS[sort]
    stmt = stmt.order_by(col.desc().nulls_last() if order == "desc" else col.asc().nulls_last(), Company.id)
    rows = db.execute(stmt.offset((page - 1) * page_size).limit(page_size)).scalars().all()
    sources = _source_names(db, [r.id for r in rows])
    items = [to_out(r, sources.get(r.id, [])) for r in rows]
    return Page(items=items, total=total, page=page, page_size=page_size)


EXPORT_COLUMNS = [
    "id",
    "name",
    "website_url",
    "domain",
    "verification_status",
    "street",
    "postcode",
    "city",
    "district_key",
    "district_name",
    "state_code",
    "lat",
    "lon",
    "industries",
    "scan_status",
    "first_seen_at",
    "last_seen_at",
    "sources",
    "licenses",
    "attribution",
]


@router.get("/export.csv")
def export_csv(f: dict = Depends(_filters), db: Session = Depends(get_db)):
    """CSV export of the filtered list. Every row carries its sources, licences and attribution."""
    stmt = build_query(f).options(selectinload(Company.sources)).order_by(Company.id)

    def generate() -> Iterator[str]:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(EXPORT_COLUMNS)
        yield "﻿" + buf.getvalue()
        for chunk_start in range(0, 10**9, 1000):
            rows = db.execute(stmt.offset(chunk_start).limit(1000)).scalars().unique().all()
            if not rows:
                break
            buf.seek(0)
            buf.truncate()
            for c in rows:
                srcs = sorted({s.source for s in c.sources})
                licenses = sorted({s.license for s in c.sources if s.license})
                attributions = sorted({s.attribution for s in c.sources if s.attribution})
                writer.writerow(
                    [
                        c.id,
                        c.name,
                        c.website_url or "",
                        c.domain or "",
                        c.verification_status,
                        c.street or "",
                        c.postcode or "",
                        c.city or "",
                        c.district_key or "",
                        c.district_name or "",
                        c.state_code or "",
                        c.lat if c.lat is not None else "",
                        c.lon if c.lon is not None else "",
                        ";".join(c.industries or []),
                        c.scan_status,
                        c.first_seen_at.isoformat(),
                        c.last_seen_at.isoformat(),
                        ";".join(srcs),
                        ";".join(licenses),
                        " | ".join(attributions),
                    ]
                )
            yield buf.getvalue()

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M")
    headers = {
        "Content-Disposition": f'attachment; filename="companies-{stamp}.csv"',
        "X-Data-Attribution": OSM_NOTICE,
    }
    return StreamingResponse(generate(), media_type="text/csv; charset=utf-8", headers=headers)


@router.get("/{company_id}", response_model=CompanyDetail)
def get_company(company_id: int, db: Session = Depends(get_db)):
    c = db.execute(
        select(Company).options(selectinload(Company.sources)).where(Company.id == company_id)
    ).scalar_one_or_none()
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    return to_out(
        c,
        sorted({s.source for s in c.sources}),
        CompanyDetail,
        alt_names=c.alt_names or [],
        verification_detail=c.verification_detail or {},
        source_records=[SourceOut.model_validate(s) for s in c.sources],
    )


def _queue_one(db: Session, company: Company, source: str) -> str:
    if company.verification_status == "verified" and company.domain:
        return scan_queue.request_scan(db, company, source=source, priority=scan_queue.PRIORITY_MANUAL)
    if company.domain and company.verification_status in ("unverified", *RETRYABLE_VERIFICATION):
        return "needs_verification"
    return "not_scannable"


@router.post("/{company_id}/scan", status_code=status.HTTP_202_ACCEPTED)
def scan_company(company_id: int, db: Session = Depends(get_db), user: str = Depends(current_user)):
    """Queue one company for career-page scanning (verified websites only).

    Unverified websites are verified first; the company is queued automatically if
    verification succeeds."""
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    result = _queue_one(db, company, "manual")
    if result == "needs_verification":
        _dispatch_verification([company.id], queue_after=True, db=db)
        db.commit()
        return {"company_id": company_id, "status": "verifying"}
    if result == "not_scannable":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Website status is '{company.verification_status}'; only verified websites are crawled.",
        )
    db.commit()
    return {"company_id": company_id, "status": result}


@router.post("/scan", status_code=status.HTTP_202_ACCEPTED)
def scan_companies(body: ScanBulkIn, db: Session = Depends(get_db)):
    """Queue several companies (dashboard: 'Queue selected for vacancy scanning')."""
    companies = db.execute(select(Company).where(Company.id.in_(body.ids))).scalars().all()
    counts = {"queued": 0, "already_queued": 0, "verifying": 0, "not_scannable": 0}
    to_verify = []
    for c in companies:
        r = _queue_one(db, c, "manual")
        if r == "needs_verification":
            to_verify.append(c.id)
            counts["verifying"] += 1
        else:
            counts[r] += 1
    if to_verify:
        _dispatch_verification(to_verify, queue_after=True, db=db)
    db.commit()
    counts["not_found"] = len(set(body.ids)) - len(companies)
    return counts


@router.post("/{company_id}/verify", status_code=status.HTTP_202_ACCEPTED)
def verify_company(company_id: int, db: Session = Depends(get_db)):
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    if not company.domain:
        raise HTTPException(status.HTTP_409_CONFLICT, "Company has no usable website")
    _dispatch_verification([company.id], queue_after=False, db=db)
    db.commit()
    return {"company_id": company_id, "status": "verifying"}


# Verification is implemented by the discovery module; the core only dispatches by task name
# so that it does not import module code.
VERIFY_TASK = "company_discovery.tasks.verify_companies"


def _dispatch_verification(ids: list[int], *, queue_after: bool, db: Session) -> None:
    from .celery_app import celery_app

    db.query(Company).filter(Company.id.in_(ids)).update(
        {Company.next_verify_at: dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)}, synchronize_session=False
    )
    if queue_after:  # a manual scan request: queue as soon as verification succeeds
        db.query(Company).filter(Company.id.in_(ids)).update(
            {Company.queue_after_verify: True}, synchronize_session=False
        )
    db.commit()  # the worker must see the flags
    task = celery_app.tasks.get(VERIFY_TASK)
    if task is not None and celery_app.conf.task_always_eager:
        task.apply(args=[ids])
    else:
        celery_app.send_task(VERIFY_TASK, args=[ids], queue="verify")
