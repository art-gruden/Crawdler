"""Career crawler API: vacancy search, crawler dashboard data, per-company crawl diagnostics."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import Float, and_, any_, cast, desc, func, or_, select
from sqlalchemy.orm import Session

from gcc_core import scan_queue
from gcc_core.auth import current_user
from gcc_core.db import get_db
from gcc_core.models import Company, ScanRequest

from . import export
from .models import CareerSource, CrawlMetric, CrawlPage, CrawlRun, ExtractionError, Job, JobSkill, SiteFingerprint

jobs_router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(current_user)])
crawler_router = APIRouter(prefix="/api/crawler", tags=["crawler"], dependencies=[Depends(current_user)])


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# --- jobs ----------------------------------------------------------------------------------------


class JobOut(BaseModel):
    id: int
    company_id: int
    company_name: str
    company_domain: str | None
    title: str
    city: str | None
    postcode: str | None
    country: str | None
    location_raw: str | None
    remote_type: str
    employment_types: list[str]
    date_posted: dt.date | None
    discovered_at: dt.datetime
    last_seen_at: dt.datetime
    status: str
    source_url: str
    application_url: str | None
    extraction_method: str
    confidence: float
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    salary_period: str | None
    skills: list[str] = []
    # DACH platform fields (additive; older clients ignore them)
    country_codes: list[str] = []
    region_codes: list[str] = []
    geo_status: str = "unknown"
    work_model: str = "UNKNOWN"
    seniority: str = "UNKNOWN"
    employment_kinds: list[str] = []


class JobDetail(JobOut):
    career_page_url: str | None
    canonical_url: str | None
    description: str | None  # plain text (never HTML): safe to render as text
    requirements: list[str]
    responsibilities: list[str]
    languages: list[dict]
    salary_raw: str | None
    valid_through: dt.datetime | None
    closed_at: dt.datetime | None
    miss_count: int
    ats_type: str | None
    ats_job_id: str | None
    source_values: dict


class JobPage(BaseModel):
    total: int
    items: list[JobOut]


def _job_out(j: Job, c: Company, skills: list[str], model=JobOut, **extra):
    data = {k: getattr(j, k) for k in model.model_fields if hasattr(j, k) and k not in ("skills",)}
    return model(**data, company_name=c.name, company_domain=c.domain, skills=skills, **extra)


class JobFilters:
    """The filters of the Vacancies table. One definition, shared by the list and the exports,
    so a download always contains exactly the rows the table is showing."""

    def __init__(
        self,
        q: str | None = Query(None, max_length=200, description="full-text search in title, location and description"),
        status_: str | None = Query("open", alias="status", pattern="^(open|closed|expired|all)$"),
        company_id: int | None = None,
        city: str | None = Query(None, max_length=80),
        remote_type: str | None = Query(None, pattern="^(onsite|hybrid|remote|unknown)$"),
        employment_type: str | None = Query(None, max_length=32),
        skill: list[str] | None = Query(None),
        method: str | None = Query(None, pattern="^(ats|jsonld|html|content|pdf|browser|llm)$"),
        new_days: int | None = Query(None, ge=1, le=365, description="discovered in the last N days"),
        posted_days: int | None = Query(None, ge=1, le=365, description="published in the last N days (source date)"),
        min_confidence: float | None = Query(None, ge=0, le=1),
        sort: str = Query("discovered", pattern="^(discovered|posted|title|company|relevance)$"),
        country_code: list[str] | None = Query(None, description="ISO 3166-1 (DE, AT, CH); several = OR"),
        region: list[str] | None = Query(None, description="ISO 3166-2 (DE-SN, AT-9, CH-ZH); several = OR"),
        specialization: list[str] | None = Query(None, description="taxonomy slug or id; a group includes its members"),
        work_model: list[str] | None = Query(None, description="REMOTE | HYBRID | ONSITE | UNKNOWN"),
        seniority: list[str] | None = Query(
            None, description="INTERN | JUNIOR | MID | SENIOR | LEAD | EXECUTIVE | UNKNOWN"
        ),
    ):
        self.country_code = [x.upper() for x in country_code or []]
        self.region = [x.upper() for x in region or []]
        self.specialization = list(specialization or [])
        self.work_model = [x.upper() for x in work_model or []]
        self.seniority = [x.upper() for x in seniority or []]
        self.q, self.status, self.company_id, self.city = q, status_, company_id, city
        self.remote_type, self.employment_type, self.skill, self.method = remote_type, employment_type, skill, method
        self.new_days, self.posted_days, self.min_confidence, self.sort = new_days, posted_days, min_confidence, sort

    def as_dict(self) -> dict:
        """Active filters only (written into the export header)."""
        d = {
            k: getattr(self, k)
            for k in (
                "q status company_id city remote_type employment_type skill method new_days posted_days "
                "min_confidence sort country_code region specialization work_model seniority"
            ).split()
        }
        return {k: v for k, v in d.items() if v not in (None, "", [])}

    def query(self):
        conds = []
        if self.status and self.status != "all":
            conds.append(Job.status == self.status)
        if self.company_id:
            conds.append(Job.company_id == self.company_id)
        if self.city:
            conds.append(or_(Job.city.ilike(f"%{self.city}%"), Job.location_raw.ilike(f"%{self.city}%")))
        if self.remote_type:
            conds.append(Job.remote_type == self.remote_type)
        if self.employment_type:
            conds.append(self.employment_type == any_(Job.employment_types))
        if self.method:
            conds.append(Job.extraction_method == self.method)
        if self.new_days:
            conds.append(Job.discovered_at >= _now() - dt.timedelta(days=self.new_days))
        if self.posted_days:
            conds.append(Job.date_posted >= (_now() - dt.timedelta(days=self.posted_days)).date())
        if self.min_confidence is not None:
            conds.append(Job.confidence >= self.min_confidence)
        if self.country_code:
            conds.append(Job.country_codes.overlap(self.country_code))
        if self.region:
            conds.append(Job.region_codes.overlap(self.region))
        if self.specialization:
            from .search import SearchError, expand_specializations

            try:
                conds.append(Job.specialization_id.in_(expand_specializations(self.specialization)))
            except SearchError as exc:
                raise HTTPException(422, str(exc)) from exc
        if self.work_model:
            conds.append(Job.work_model.in_(self.work_model))
        if self.seniority:
            conds.append(Job.seniority.in_(self.seniority))
        for sk in self.skill or []:
            conds.append(Job.id.in_(select(JobSkill.job_id).where(func.lower(JobSkill.skill) == sk.lower())))
        tsq = None
        if self.q and self.q.strip():
            tsq = func.websearch_to_tsquery("simple", self.q.strip())
            conds.append(or_(Job.search.op("@@")(tsq), Job.title.ilike(f"%{self.q.strip()}%")))
        base = select(Job, Company).join(Company, Company.id == Job.company_id).where(and_(*conds) if conds else True)
        order = {
            "discovered": [desc(Job.discovered_at)],
            "posted": [desc(Job.date_posted).nulls_last(), desc(Job.discovered_at)],
            "title": [func.lower(Job.title)],
            "company": [func.lower(Company.name), func.lower(Job.title)],
            "relevance": [desc(func.ts_rank(Job.search, tsq))] if tsq is not None else [desc(Job.discovered_at)],
        }[self.sort]
        return base, [*order, Job.id]


@jobs_router.get("", response_model=JobPage)
def list_jobs(
    f: JobFilters = Depends(),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    base, order = f.query()
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    rows = db.execute(base.order_by(*order).limit(limit).offset(offset)).all()
    ids = [j.id for j, _ in rows]
    skills: dict[int, list[str]] = {}
    if ids:
        for jid, sk in db.execute(select(JobSkill.job_id, JobSkill.skill).where(JobSkill.job_id.in_(ids))):
            skills.setdefault(jid, []).append(sk)
    return JobPage(total=total, items=[_job_out(j, c, skills.get(j.id, [])) for j, c in rows])


@jobs_router.get("/skills")
def top_skills(limit: int = Query(40, ge=1, le=200), db: Session = Depends(get_db)):
    rows = db.execute(
        select(JobSkill.skill, JobSkill.category, func.count().label("n"))
        .join(Job, Job.id == JobSkill.job_id)
        .where(Job.status == "open")
        .group_by(JobSkill.skill, JobSkill.category)
        .order_by(desc("n"))
        .limit(limit)
    ).all()
    return [{"skill": s, "category": c, "jobs": n} for s, c, n in rows]


def _download(chunks, media: str, name: str) -> StreamingResponse:
    return StreamingResponse(
        chunks,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )


def _stamp() -> str:
    return _now().strftime("%Y-%m-%d")


@jobs_router.get("/export.csv", summary="Download the filtered vacancies as CSV (same filters as the table)")
def export_csv(
    f: JobFilters = Depends(),
    delimiter: str = Query(",", pattern="^[,;]$", description="';' opens directly in German-locale Excel"),
    limit: int = Query(export.MAX_ROWS, ge=1, le=export.MAX_ROWS),
    db: Session = Depends(get_db),
):
    base, order = f.query()
    return _download(
        export.csv_stream(base, order, limit, delimiter), "text/csv; charset=utf-8", f"vacancies-{_stamp()}.csv"
    )


@jobs_router.get("/export.yaml", summary="Download the filtered vacancies as YAML: every field, full description")
def export_yaml(
    f: JobFilters = Depends(),
    limit: int = Query(export.MAX_ROWS, ge=1, le=export.MAX_ROWS),
    db: Session = Depends(get_db),
):
    base, order = f.query()
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    return _download(
        export.yaml_stream(base, order, limit, total=total, filters=f.as_dict()),
        "application/yaml; charset=utf-8",
        f"vacancies-{_stamp()}.yaml",
    )


@jobs_router.get("/{job_id}/export.yaml", summary="Download one vacancy as YAML (all fields, full description)")
def export_job_yaml(job_id: int, db: Session = Depends(get_db)):
    j = db.get(Job, job_id)
    if j is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    skills = list(
        db.execute(select(JobSkill.skill, JobSkill.category).where(JobSkill.job_id == j.id).order_by(JobSkill.skill))
    )
    body = export.single_yaml(j, db.get(Company, j.company_id), [(s, c) for s, c in skills])
    return Response(
        body,
        media_type="application/yaml; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="vacancy-{j.id}.yaml"', "Cache-Control": "no-store"},
    )


@jobs_router.get("/{job_id}", response_model=JobDetail)
def get_job(job_id: int, db: Session = Depends(get_db)):
    j = db.get(Job, job_id)
    if j is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    c = db.get(Company, j.company_id)
    skills = list(db.execute(select(JobSkill.skill).where(JobSkill.job_id == j.id)).scalars())
    return _job_out(j, c, skills, model=JobDetail)


# --- crawler dashboard -------------------------------------------------------------------------------


@crawler_router.get("/stats")
def stats(hours: int = Query(24, ge=1, le=24 * 30), db: Session = Depends(get_db)):
    now = _now()
    since = now - dt.timedelta(hours=hours)
    m = (
        db.execute(
            select(
                *(
                    func.coalesce(func.sum(getattr(CrawlMetric, c)), 0).label(c)
                    for c in (
                        "pages",
                        "requests",
                        "bytes",
                        "status_429",
                        "status_503",
                        "errors",
                        "latency_ms_sum",
                        "companies_done",
                        "jobs_found",
                        "jobs_new",
                        "browser_pages",
                    )
                )
            ).where(CrawlMetric.bucket >= since)
        )
        .one()
        ._asdict()
    )
    last5 = db.execute(
        select(func.coalesce(func.sum(CrawlMetric.pages), 0)).where(CrawlMetric.bucket >= now - dt.timedelta(minutes=5))
    ).scalar_one()
    last_hour_companies = db.execute(
        select(func.coalesce(func.sum(CrawlMetric.companies_done), 0)).where(
            CrawlMetric.bucket >= now - dt.timedelta(hours=1)
        )
    ).scalar_one()
    by_method: dict[str, dict[str, int]] = {}
    for (bm,) in db.execute(select(CrawlMetric.by_method).where(CrawlMetric.bucket >= since)):
        for name, v in (bm or {}).items():
            cur = by_method.setdefault(name, {"ok": 0, "fail": 0, "jobs": 0})
            for k in cur:
                cur[k] += int(v.get(k, 0))
    jobs_by_method = dict(
        db.execute(
            select(Job.extraction_method, func.count()).where(Job.status == "open").group_by(Job.extraction_method)
        ).all()
    )
    queue = dict(
        db.execute(
            select(ScanRequest.status, func.count())
            .where(ScanRequest.status.in_(("pending", "claimed")))
            .group_by(ScanRequest.status)
        ).all()
    )
    browser_q = db.execute(select(func.count()).select_from(CrawlRun).where(CrawlRun.browser_pending)).scalar_one()
    llm_q = db.execute(
        select(func.count()).select_from(CrawlRun).where(func.cardinality(CrawlRun.llm_urls) > 0)
    ).scalar_one()
    runs = dict(
        db.execute(
            select(CrawlRun.status, func.count())
            .where(or_(CrawlRun.status == "running", CrawlRun.finished_at >= since))
            .group_by(CrawlRun.status)
        ).all()
    )
    outcomes = dict(
        db.execute(
            select(CrawlRun.outcome, func.count()).where(CrawlRun.finished_at >= since).group_by(CrawlRun.outcome)
        ).all()
    )
    # latest resource sample per worker (last 10 minutes)
    latest = (
        select(CrawlMetric.worker, func.max(CrawlMetric.bucket).label("b"))
        .where(CrawlMetric.bucket >= now - dt.timedelta(minutes=10), CrawlMetric.cpu_pct.is_not(None))
        .group_by(CrawlMetric.worker)
        .subquery()
    )
    workers = [
        {
            "worker": r.worker,
            "at": r.bucket,
            "cpu_pct": r.cpu_pct,
            "mem_available_mb": r.mem_available_mb,
            "container_mem_mb": r.container_mem_mb,
            "http_limit": r.http_limit,
            "http_active": r.http_active,
            "browser_active": r.browser_active,
            "browser_paused": r.browser_paused,
        }
        for r in db.execute(
            select(CrawlMetric).join(
                latest, and_(CrawlMetric.worker == latest.c.worker, CrawlMetric.bucket == latest.c.b)
            )
        ).scalars()
    ]
    totals = db.execute(
        select(
            func.count().filter(Job.status == "open"),
            func.count(),
            func.count().filter(Job.discovered_at >= since),
        ).select_from(Job)
    ).one()
    requests = m["requests"] or 0
    return {
        "window_hours": hours,
        "active_scans": runs.get("running", 0),
        "completed_scans": sum(v for k, v in runs.items() if k != "running"),
        "runs_by_status": runs,
        "outcomes": outcomes,
        "companies_per_hour": last_hour_companies,
        "pages_per_second": round(last5 / 300, 3),
        "pages": m["pages"],
        "requests": requests,
        "mb_downloaded": round(m["bytes"] / 1024 / 1024, 1),
        "avg_latency_ms": round(m["latency_ms_sum"] / requests, 1) if requests else None,
        "rate_429_503": round((m["status_429"] + m["status_503"]) / requests, 4) if requests else 0.0,
        "fetch_errors": m["errors"],
        "jobs_open": totals[0],
        "jobs_total": totals[1],
        "jobs_new": totals[2],
        "jobs_found_in_scans": m["jobs_found"],
        "browser_pages": m["browser_pages"],
        "extraction_by_method": by_method,
        "open_jobs_by_method": jobs_by_method,
        "queues": {
            "scan_pending": queue.get("pending", 0),
            "scan_claimed": queue.get("claimed", 0),
            "browser_pending": browser_q,
            "llm_pending": llm_q,
        },
        "workers": workers,
    }


@crawler_router.get("/timeseries")
def timeseries(
    hours: int = Query(24, ge=1, le=24 * 14),
    bucket_minutes: int = Query(15, ge=1, le=240),
    db: Session = Depends(get_db),
):
    since = _now() - dt.timedelta(hours=hours)
    b = func.to_timestamp(
        func.floor(func.extract("epoch", CrawlMetric.bucket) / (bucket_minutes * 60)) * (bucket_minutes * 60)
    )
    rows = db.execute(
        select(
            b.label("t"),
            func.sum(CrawlMetric.pages),
            func.sum(CrawlMetric.jobs_new),
            func.sum(CrawlMetric.errors),
            func.sum(CrawlMetric.status_429 + CrawlMetric.status_503),
            func.sum(CrawlMetric.companies_done),
            func.avg(CrawlMetric.cpu_pct),
            func.min(CrawlMetric.mem_available_mb),
            func.sum(CrawlMetric.browser_pages),
            cast(func.avg(CrawlMetric.http_limit), Float),
        )
        .where(CrawlMetric.bucket >= since)
        .group_by("t")
        .order_by("t")
    ).all()
    keys = (
        "t",
        "pages",
        "jobs_new",
        "errors",
        "rate_limited",
        "companies",
        "cpu_pct",
        "mem_available_mb",
        "browser_pages",
        "http_limit",
    )
    return [dict(zip(keys, r, strict=True)) for r in rows]


@crawler_router.get("/runs")
def runs(
    status_: str | None = Query(None, alias="status", pattern="^(running|done|partial|failed|abandoned)$"),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    q = select(CrawlRun, Company.name, Company.domain).join(Company, Company.id == CrawlRun.company_id)
    if status_:
        q = q.where(CrawlRun.status == status_)
    rows = db.execute(q.order_by(desc(CrawlRun.id)).limit(limit)).all()
    return [
        {
            "id": r.id,
            "company_id": r.company_id,
            "company": name,
            "domain": domain,
            "status": r.status,
            "outcome": r.outcome,
            "strategy": r.strategy,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "heartbeat_at": r.heartbeat_at,
            "pages_fetched": r.pages_fetched,
            "jobs_found": r.jobs_found,
            "jobs_new": r.jobs_new,
            "jobs_closed": r.jobs_closed,
            "complete": r.complete,
            "browser_pending": r.browser_pending,
            "worker": r.worker,
        }
        for r, name, domain in rows
    ]


@crawler_router.get("/problem-domains")
def problem_domains(limit: int = Query(30, ge=1, le=200), db: Session = Depends(get_db)):
    """Slow or failing sites: error streaks, rate limits, high latency."""
    rows = db.execute(
        select(SiteFingerprint, Company.name)
        .join(Company, Company.id == SiteFingerprint.company_id)
        .where(
            or_(
                SiteFingerprint.error_streak > 0,
                SiteFingerprint.avg_latency_ms > 3000,
                SiteFingerprint.rate_limited_until > _now(),
            )
        )
        .order_by(desc(SiteFingerprint.error_streak), desc(SiteFingerprint.avg_latency_ms).nulls_last())
        .limit(limit)
    ).all()
    out = []
    for fp, name in rows:
        last = db.execute(
            select(ExtractionError.kind, ExtractionError.message)
            .where(ExtractionError.company_id == fp.company_id)
            .order_by(desc(ExtractionError.id))
            .limit(1)
        ).first()
        out.append(
            {
                "company_id": fp.company_id,
                "company": name,
                "domain": fp.domain,
                "error_streak": fp.error_streak,
                "avg_latency_ms": fp.avg_latency_ms,
                "rate_limited_until": fp.rate_limited_until,
                "last_error": {"kind": last[0], "message": last[1]} if last else None,
                "next_scan_at": fp.next_scan_at,
            }
        )
    return out


# --- ATS providers (admin diagnostics) -------------------------------------------------------------


def _source_row(x: CareerSource, company: str | None) -> dict:
    d = x.scan_detail or {}
    return {
        "source_id": x.id,
        "company_id": x.company_id,
        "company": company,
        "provider": x.ats_type,
        "account": x.ats_account,
        "url": x.url,
        "strategy": x.scan_strategy,
        "scan_complete": x.scan_complete,
        "reported_total": x.reported_total,
        "job_count": x.last_job_count,
        "last_scan_at": x.last_scan_at,
        "last_complete_scan_at": x.last_complete_scan_at,
        "last_success_at": x.last_success_at,
        "last_error": x.last_error,
        "consecutive_failures": x.consecutive_failures,
        "search": {
            k: d.get(k)
            for k in (
                "queries_planned",
                "queries_run",
                "queries_failed",
                "queries_saturated",
                "skipped_by_budget",
                "aborted",
            )
        }
        if d.get("strategy") in ("search", "catalog+search")
        else None,
        "capped": bool(d.get("capped")) or None,
    }


def _observed_access(total: int, ok: int, errors: dict[str, int]) -> str:
    """What the crawler actually saw (not what the adapter claims)."""
    if total == 0:
        return "no_data"
    if errors.get("robots_disallowed", 0) >= max(1, total - errors.get("provider_disabled", 0)):
        return "robots_blocked"
    if errors.get("provider_disabled", 0) == total:
        return "not_fetched"
    return "ok" if ok else "failing"


@crawler_router.get("/ats/providers")
def ats_providers(db: Session = Depends(get_db)):
    """Per ATS provider: capabilities, verification status, access outcome, counts and completeness."""
    from .ats import capabilities as caps_mod
    from .config import get_crawler_settings

    cfg = get_crawler_settings()
    enabled = caps_mod.enabled_providers(cfg.ats_enabled_providers)
    open_by_provider = dict(
        db.execute(
            select(Job.ats_type, func.count())
            .where(Job.status == "open", Job.ats_type.is_not(None))
            .group_by(Job.ats_type)
        ).all()
    )
    src_rows = db.execute(
        select(
            CareerSource.ats_type,
            func.count().label("total"),
            func.count().filter(CareerSource.consecutive_failures == 0).label("ok"),
            func.count().filter(CareerSource.scan_complete.is_(True)).label("complete"),
            func.count()
            .filter(CareerSource.scan_complete.is_(False), CareerSource.consecutive_failures == 0)
            .label("partial"),
            func.max(CareerSource.last_scan_at).label("last_scan_at"),
            func.max(CareerSource.last_complete_scan_at).label("last_complete_scan_at"),
            func.coalesce(func.sum(CareerSource.last_job_count), 0).label("jobs_last_scan"),
        )
        .where(CareerSource.kind == "ats_feed", CareerSource.active.is_(True))
        .group_by(CareerSource.ats_type)
    ).all()
    stats = {r.ats_type: r for r in src_rows}
    strat = {}
    for t, st, n in db.execute(
        select(CareerSource.ats_type, CareerSource.scan_strategy, func.count())
        .where(CareerSource.kind == "ats_feed", CareerSource.active.is_(True))
        .group_by(CareerSource.ats_type, CareerSource.scan_strategy)
    ).all():
        strat.setdefault(t, {})[st or "unknown"] = n
    errs = {}
    for t, e, n, last in db.execute(
        select(CareerSource.ats_type, CareerSource.last_error, func.count(), func.max(CareerSource.last_seen_at))
        .where(CareerSource.kind == "ats_feed", CareerSource.active.is_(True), CareerSource.last_error.is_not(None))
        .group_by(CareerSource.ats_type, CareerSource.last_error)
        .order_by(desc(func.count()))
    ).all():
        errs.setdefault(t, []).append({"error": e, "sources": n, "last_seen_at": last})
    providers = []
    names = list(dict.fromkeys([*caps_mod.CAPABILITIES, *stats]))
    for name in names:
        cap = caps_mod.CAPABILITIES.get(name)
        st = stats.get(name)
        e = errs.get(name, [])
        total = st.total if st else 0
        providers.append(
            {
                "provider": name,
                "enabled": name in enabled,
                "capabilities": cap.as_dict() if cap else None,
                "observed_access": _observed_access(total, st.ok if st else 0, {x["error"]: x["sources"] for x in e}),
                "sources": {
                    "total": total,
                    "ok": st.ok if st else 0,
                    "scan_complete": st.complete if st else 0,
                    "scan_partial": st.partial if st else 0,
                },
                "strategies": strat.get(name, {}),
                "open_jobs": open_by_provider.get(name, 0),
                "jobs_in_last_scans": int(st.jobs_last_scan) if st else 0,
                "last_scan_at": st.last_scan_at if st else None,
                "last_complete_scan_at": st.last_complete_scan_at if st else None,
                "errors": e[:8],
            }
        )
    return {
        "generated_at": _now(),
        "settings": {
            "enabled_providers": sorted(enabled),
            "search_enabled": cfg.ats_search_enabled,
            "catalog_max_jobs": cfg.ats_catalog_max_jobs,
            "search_max_queries": cfg.ats_search_max_queries,
            "search_max_pages": cfg.ats_search_max_pages,
            "search_max_jobs": cfg.ats_search_max_jobs,
            "search_keywords": [k.strip() for k in cfg.ats_search_keywords.split(",") if k.strip()],
            "search_countries": [c.strip() for c in cfg.ats_search_countries.split(",") if c.strip()],
        },
        "providers": providers,
    }


@crawler_router.get("/ats/sources")
def ats_sources(
    provider: str | None = Query(None, max_length=32),
    only: str | None = Query(None, pattern="^(failing|incomplete|disabled|capped|search)$"),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """ATS sources with their strategy, completeness and last error (filter: failing/incomplete/disabled/...)."""
    q = (
        select(CareerSource, Company.name)
        .join(Company, Company.id == CareerSource.company_id)
        .where(CareerSource.kind == "ats_feed", CareerSource.active.is_(True))
    )
    if provider:
        q = q.where(CareerSource.ats_type == provider)
    if only == "failing":
        q = q.where(CareerSource.consecutive_failures > 0, CareerSource.last_error != "provider_disabled")
    elif only == "incomplete":
        q = q.where(CareerSource.scan_complete.is_(False), CareerSource.consecutive_failures == 0)
    elif only == "disabled":
        q = q.where(CareerSource.last_error.in_(["provider_disabled", "search_disabled"]))
    elif only == "capped":
        q = q.where(CareerSource.scan_detail["capped"].astext == "true")
    elif only == "search":
        q = q.where(CareerSource.scan_strategy.in_(["search", "catalog+search"]))
    rows = db.execute(q.order_by(desc(CareerSource.last_scan_at).nulls_last(), CareerSource.id).limit(limit)).all()
    return [_source_row(x, name) for x, name in rows]


@crawler_router.get("/companies/{company_id}")
def company_crawl(company_id: int, db: Session = Depends(get_db)):
    """Everything needed to understand why a company yields (no) postings."""
    c = db.get(Company, company_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    fp = db.get(SiteFingerprint, company_id)
    sources = (
        db.execute(
            select(CareerSource)
            .where(CareerSource.company_id == company_id)
            .order_by(desc(CareerSource.last_seen_at))
            .limit(50)
        )
        .scalars()
        .all()
    )
    runs_ = (
        db.execute(select(CrawlRun).where(CrawlRun.company_id == company_id).order_by(desc(CrawlRun.id)).limit(10))
        .scalars()
        .all()
    )
    errors = (
        db.execute(
            select(ExtractionError)
            .where(ExtractionError.company_id == company_id)
            .order_by(desc(ExtractionError.id))
            .limit(50)
        )
        .scalars()
        .all()
    )
    pages = db.execute(
        select(CrawlPage.page_type, func.count(), func.max(CrawlPage.fetched_at))
        .where(CrawlPage.company_id == company_id)
        .group_by(CrawlPage.page_type)
    ).all()
    career_pages = db.execute(
        select(
            CrawlPage.url,
            CrawlPage.page_type,
            CrawlPage.http_status,
            CrawlPage.jobs_found,
            CrawlPage.strategy,
            CrawlPage.fetched_at,
        )
        .where(
            CrawlPage.company_id == company_id,
            CrawlPage.page_type.in_(("job_list", "career_landing", "no_vacancies", "js_shell", "job_detail", "error")),
        )
        .order_by(desc(CrawlPage.fetched_at))
        .limit(50)
    ).all()
    jobs = dict(
        db.execute(select(Job.status, func.count()).where(Job.company_id == company_id).group_by(Job.status)).all()
    )
    last = runs_[0] if runs_ else None
    return {
        "company": {
            "id": c.id,
            "name": c.name,
            "domain": c.domain,
            "website_url": c.website_url,
            "verification_status": c.verification_status,
            "scan_status": c.scan_status,
            "last_scan_at": c.last_scan_at,
        },
        "diagnosis": _diagnose(c, fp, last, errors),
        "fingerprint": None
        if fp is None
        else {
            "best_strategy": fp.best_strategy,
            "strategy_stats": fp.strategy_stats,
            "ats_type": fp.ats_type,
            "ats_account": fp.ats_account,
            "career_urls": fp.career_urls,
            "needs_browser": fp.needs_browser,
            "technologies": fp.technologies,
            "avg_latency_ms": fp.avg_latency_ms,
            "error_streak": fp.error_streak,
            "rate_limited_until": fp.rate_limited_until,
            "interval_h": fp.interval_h,
            "next_scan_at": fp.next_scan_at,
            "last_success_at": fp.last_success_at,
            "last_new_job_at": fp.last_new_job_at,
            "open_jobs": fp.open_jobs,
        },
        "sources": [
            {
                "id": x.id,
                "url": x.url,
                "kind": x.kind,
                "ats_type": x.ats_type,
                "ats_account": x.ats_account,
                "evidence": x.evidence,
                "last_success_at": x.last_success_at,
                "last_error": x.last_error,
                "last_job_count": x.last_job_count,
                "consecutive_failures": x.consecutive_failures,
            }
            for x in sources
        ],
        "runs": [
            {
                "id": r.id,
                "status": r.status,
                "outcome": r.outcome,
                "strategy": r.strategy,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "pages_fetched": r.pages_fetched,
                "jobs_found": r.jobs_found,
                "jobs_new": r.jobs_new,
                "jobs_closed": r.jobs_closed,
                "complete": r.complete,
                "browser_pending": r.browser_pending,
                "stats": r.stats,
            }
            for r in runs_
        ],
        "errors": [
            {
                "at": e.created_at,
                "stage": e.stage,
                "kind": e.kind,
                "url": e.url,
                "message": e.message,
                "http_status": e.http_status,
                "strategy": e.strategy,
            }
            for e in errors
        ],
        "pages": [{"page_type": t, "count": n, "last_fetched": f} for t, n, f in pages],
        "career_pages": [dict(r._mapping) for r in career_pages],
        "jobs": jobs,
    }


EXPLAIN = {
    "jobs_found": "Postings were extracted.",
    "no_career_page": "No career page was found on the website (links, sitemap and common paths were checked).",
    "no_vacancies": "The career page states that there are currently no open positions.",
    "career_page_no_jobs": "A career page was found but no postings could be extracted from it.",
    "needs_browser": "The career page is rendered with JavaScript; it is queued for the browser worker.",
    "rate_limited": "The website answered 429/503; the scan stopped and honours Retry-After.",
    "access_restricted": "Login wall, HTTP 401/403 or a bot challenge. This is never bypassed.",
    "robots_unavailable": "robots.txt answered with a server error; RFC 9309 treats that as 'disallow all'.",
    "worker_lost": "The worker stopped during the scan; it is resumed from the checkpoint.",
    "internal_error": "The crawler raised an unexpected error (see errors).",
}


def _diagnose(c: Company, fp, last, errors) -> dict:
    if c.verification_status != "verified":
        return {
            "code": "not_verified",
            "text": f"Website status is '{c.verification_status}'; only verified websites are crawled.",
        }
    if last is None:
        return {"code": "never_scanned", "text": "No scan has run yet."}
    code = last.outcome or last.status
    if code and code.startswith("site unreachable"):
        code = "unreachable"
    text = EXPLAIN.get(code, f"Last scan outcome: {code}.")
    if last.browser_pending:
        text += " Browser pass pending."
    if errors:
        text += f" Last error: {errors[0].stage}/{errors[0].kind}" + (
            f" – {errors[0].message[:160]}" if errors[0].message else ""
        )
    return {"code": code, "text": text}


class RescanIn(BaseModel):
    full: bool = False  # forget cached strategy/ATS and run full discovery


@crawler_router.post("/companies/{company_id}/rescan", status_code=status.HTTP_202_ACCEPTED)
def rescan(company_id: int, body: RescanIn | None = None, db: Session = Depends(get_db)):
    c = db.get(Company, company_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    fp = db.get(SiteFingerprint, company_id)
    if fp is not None:
        fp.rate_limited_until = None if (body and body.full) else fp.rate_limited_until
        if body and body.full:
            fp.full_discovery_at = None
    try:
        result = scan_queue.request_scan(db, c, source="manual", priority=scan_queue.PRIORITY_MANUAL)
    except scan_queue.NotScannable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    return {"company_id": company_id, "status": result, "full": bool(body and body.full)}
