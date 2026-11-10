"""Persistence of scan results: sources, jobs (conservative dedup + lifecycle), pages,
errors, site fingerprints and run bookkeeping. All functions take a sync Session; the
caller commits.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

from sqlalchemy import and_, any_, case, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from gcc_core.geo import resolve_location
from gcc_core.geo_db import set_job_locations
from gcc_core.models import Company

from . import events
from .config import CrawlerSettings
from .engine import CompanyInfo, FingerprintView, FoundJob, KnownPage, ScanResult
from .extract.normalize import NORMALIZATION_VERSION
from .extract.normalize import Job as NJob
from .extract.skills import CEFR_ORDER
from .models import (
    CareerSource,
    CrawlPage,
    CrawlRun,
    ExtractionError,
    Job,
    JobLanguage,
    JobSkill,
    JobSpecialization,
    SiteFingerprint,
)
from .schedule import next_interval_h
from .urls import canonicalize, url_hash

METHOD_RANK = {"ats": 6, "jsonld": 5, "browser": 4, "html": 3, "content": 3, "pdf": 2, "llm": 1}
FULL_DISCOVERY_DAYS = 7
MAX_ERRORS_PER_RUN = 50


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# --- context ---------------------------------------------------------------------------------


def load_context(
    s: Session, company_id: int, settings: CrawlerSettings
) -> tuple[CompanyInfo, FingerprintView, dict[str, KnownPage], dict]:
    c = s.get(Company, company_id)
    if c is None or not c.domain or not c.website_url:
        raise LookupError(f"company {company_id} has no verified website")
    info = CompanyInfo(c.id, c.name, c.domain, c.website_url)
    fp = s.get(SiteFingerprint, company_id)
    view = FingerprintView()
    if fp is not None:
        view = FingerprintView(
            best_strategy=fp.best_strategy,
            ats_type=fp.ats_type,
            ats_account=fp.ats_account,
            career_urls=list(fp.career_urls or []),
            needs_browser=fp.needs_browser,
            full_discovery_due=fp.full_discovery_at is None
            or now() - fp.full_discovery_at > dt.timedelta(days=FULL_DISCOVERY_DAYS),
        )
    known = {
        p.url: KnownPage(p.page_type, p.content_hash, p.etag, p.last_modified)
        for p in s.execute(select(CrawlPage).where(CrawlPage.company_id == company_id).limit(2000)).scalars()
    }
    resume = {}
    last = s.execute(
        select(CrawlRun)
        .where(CrawlRun.company_id == company_id, CrawlRun.status == "abandoned")
        .where(CrawlRun.heartbeat_at > now() - dt.timedelta(hours=settings.resume_max_age_h))
        .order_by(CrawlRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last is not None and last.checkpoint:
        resume = dict(last.checkpoint)
    return info, view, known, resume


# --- runs (one running per company) -------------------------------------------------------------


def start_run(s: Session, company_id: int, *, request_id: int | None, worker: str) -> int | None:
    stmt = (
        insert(CrawlRun)
        .values(company_id=company_id, scan_request_id=request_id, worker=worker, status="running")
        .on_conflict_do_nothing(index_elements=["company_id"], index_where=text("status = 'running'"))
        .returning(CrawlRun.id)
    )
    return s.execute(stmt).scalar_one_or_none()


def heartbeat(s: Session, run_id: int, state: dict | None = None, pages: int | None = None) -> None:
    values = {"heartbeat_at": now()}
    if state is not None:
        values["checkpoint"] = state
    if pages is not None:
        values["pages_fetched"] = pages
    s.execute(update(CrawlRun).where(CrawlRun.id == run_id, CrawlRun.status == "running").values(**values))


def abandon_stale_runs(s: Session, *, minutes: int) -> list[int]:
    rows = (
        s.execute(
            update(CrawlRun)
            .where(CrawlRun.status == "running", CrawlRun.heartbeat_at < now() - dt.timedelta(minutes=minutes))
            .values(status="abandoned", finished_at=now(), outcome="worker_lost")
            .returning(CrawlRun.id)
        )
        .scalars()
        .all()
    )
    return list(rows)


# --- persist ----------------------------------------------------------------------------------


def _source_ids(s: Session, company_id: int, result: ScanResult) -> dict[str, int]:
    ids = {}
    t = now()
    for src in result.sources.values():
        is_feed = src.kind == "ats_feed"
        values = dict(
            company_id=company_id,
            url=src.url,
            kind=src.kind,
            ats_type=src.ats_type,
            ats_account=src.ats_account,
            evidence=src.evidence,
            last_seen_at=t,
            last_success_at=t if src.ok else None,
            last_error=src.error,
            consecutive_failures=0 if src.ok else 1,
            last_job_count=src.job_count if src.ok else None,
        )
        if is_feed:
            values.update(
                scan_strategy=src.strategy,
                scan_complete=bool(src.ok and src.complete),
                reported_total=src.reported_total,
                scan_detail=src.detail or None,
                last_scan_at=t,
                last_complete_scan_at=t if (src.ok and src.complete) else None,
            )
        stmt = insert(CareerSource).values(**values)
        update = {
            "kind": stmt.excluded.kind,
            "active": True,
            "last_seen_at": t,
            "last_success_at": func.coalesce(stmt.excluded.last_success_at, CareerSource.last_success_at),
            "last_error": stmt.excluded.last_error,
            "consecutive_failures": (CareerSource.consecutive_failures + 1) if not src.ok else 0,
            "last_job_count": func.coalesce(stmt.excluded.last_job_count, CareerSource.last_job_count),
            "evidence": stmt.excluded.evidence,
        }
        if is_feed:
            update.update(
                scan_strategy=stmt.excluded.scan_strategy,
                scan_complete=stmt.excluded.scan_complete,
                reported_total=stmt.excluded.reported_total,
                scan_detail=stmt.excluded.scan_detail,
                last_scan_at=t,
                last_complete_scan_at=func.coalesce(
                    stmt.excluded.last_complete_scan_at, CareerSource.last_complete_scan_at
                ),
            )
        stmt = stmt.on_conflict_do_update(constraint="uq_career_sources_company_url", set_=update).returning(
            CareerSource.id
        )
        ids[src.url] = s.execute(stmt).scalar_one()
    return ids


def dedup_key(company_id: int, j: NJob, shared_url: bool) -> str:
    if j.ats_type and j.ats_job_id:
        return f"{company_id}:ats:{j.ats_type}:{(j.ats_account or '').lower()}:{j.ats_job_id}"
    posting_url = j.source_values.get("url") if isinstance(j.source_values.get("url"), str) else None
    if j.method == "jsonld" and j.ats_job_id:
        return f"{company_id}:id:{j.ats_job_id}"
    url = canonicalize(posting_url or j.source_url) or j.source_url
    if not shared_url:
        return f"{company_id}:url:{url}"
    h = hashlib.sha1(f"{url}|{j.title_norm}|{(j.city or '').lower()}".encode()).hexdigest()
    return f"{company_id}:content:{h}"


def _find_existing(s: Session, company_id: int, key: str, j: NJob, canon: str | None) -> Job | None:
    row = s.execute(select(Job).where(Job.dedup_key == key)).scalar_one_or_none()
    if row is not None:
        return row
    if canon:
        row = s.execute(
            select(Job)
            .where(Job.company_id == company_id)
            .where(or_(Job.canonical_url == canon, canon == any_(Job.alt_urls)))
            .limit(1)
        ).scalar_one_or_none()
        if row is not None and row.title_norm == j.title_norm:
            return row
    # content match: same normalized title and city, and identical description hash or one side
    # lacking a strong id (e.g. list entry first, ATS feed later)
    cands = (
        s.execute(
            select(Job)
            .where(Job.company_id == company_id, Job.title_norm == j.title_norm)
            .where(func.coalesce(Job.city, "") == (j.city or ""))
            .where(Job.last_seen_at > now() - dt.timedelta(days=60))
            .limit(5)
        )
        .scalars()
        .all()
    )
    for c in cands:
        if c.content_hash == j.content_hash:
            return c
        if (c.ats_job_id is None) != (j.ats_job_id is None) and (c.ats_type in (None, j.ats_type)):
            return c
    return None


def _apply(row: Job, j: NJob, *, overwrite: bool) -> None:
    def setv(attr, value):
        if value in (None, "", [], {}):
            return
        if overwrite or getattr(row, attr) in (None, "", [], {}):
            setattr(row, attr, value)

    for attr in (
        "title",
        "title_norm",
        "application_url",
        "career_page_url",
        "location_raw",
        "city",
        "postcode",
        "region",
        "country",
        "description",
        "requirements",
        "responsibilities",
        "languages",
        "salary_min",
        "salary_max",
        "salary_currency",
        "salary_period",
        "salary_raw",
        "valid_through",
    ):
        setv(attr, getattr(j, attr))
    if j.remote_type != "unknown" and (overwrite or row.remote_type == "unknown"):
        row.remote_type = j.remote_type
    if j.employment_types and (overwrite or not row.employment_types):
        row.employment_types = j.employment_types
    if j.date_posted and (row.date_posted is None or overwrite):
        row.date_posted = j.date_posted  # source value only
    if overwrite:
        row.extraction_method = j.method
        row.confidence = j.confidence
        row.content_hash = j.content_hash
        row.source_url = j.source_url
        row.source_values = {**(row.source_values or {}), **j.source_values}
    if j.ats_type and j.ats_job_id and not row.ats_job_id:
        row.ats_type, row.ats_job_id = j.ats_type, j.ats_job_id


def _sync_languages(s: Session, job_id: int, languages: list[dict]) -> None:
    s.execute(JobLanguage.__table__.delete().where(JobLanguage.job_id == job_id))
    rows = [
        {
            "job_id": job_id,
            "language": x["language"][:3],
            "level": x.get("level"),
            "level_rank": CEFR_ORDER.get(x.get("level") or ""),
            "requirement": x.get("requirement") or ("required" if x.get("required") else "preferred"),
            "descriptor": x.get("descriptor"),
            "evidence": (x.get("evidence") or x.get("raw") or "")[:300] or None,
            "confidence": x.get("confidence") or 0.5,
        }
        for x in languages
        if x.get("language")
    ]
    if rows:
        s.execute(insert(JobLanguage).values(rows).on_conflict_do_nothing())


def _sync_specializations(s: Session, job_id: int, primary: int | None, conf: float, secondary: list) -> None:
    s.execute(JobSpecialization.__table__.delete().where(JobSpecialization.job_id == job_id))
    if primary:
        rows = [{"job_id": job_id, "specialization_id": primary, "is_primary": True, "confidence": conf}]
        rows += [
            {"job_id": job_id, "specialization_id": i, "is_primary": False, "confidence": c}
            for i, c in secondary
            if i != primary
        ]
        s.execute(insert(JobSpecialization).values(rows).on_conflict_do_nothing())


def _apply_derived(s: Session, row: Job, j: NJob, *, overwrite: bool, company_country: str | None, cache: dict) -> None:
    """Normalized DACH fields. Same merge rule as the legacy columns: better data replaces, nothing
    known is erased by a weaker read."""
    unknown_before = not row.location_ids
    geo = j.geo
    if geo is not None and geo.status in ("ambiguous", "partial") and company_country and j.location_raw:
        geo = resolve_location(j.location_raw, j.locations_struct, context_country=company_country)
    if geo is not None and (overwrite or unknown_before):
        if geo.places:
            ids, countries, regions = set_job_locations(s, row.id, geo.places, cache)
            row.location_ids, row.country_codes, row.region_codes = ids, countries, regions
            row.geo_status, row.geo_detail = geo.status, geo.detail()
        elif unknown_before:
            row.geo_status, row.geo_detail = geo.status, geo.detail()
    if j.work_model != "UNKNOWN" and (overwrite or row.work_model == "UNKNOWN"):
        row.work_model = j.work_model
        row.remote_scope, row.remote_countries = j.remote_scope, list(j.remote_countries)
    if j.employment_kinds != ["UNKNOWN"] and (overwrite or row.employment_kinds in (None, [], ["UNKNOWN"])):
        row.employment_kinds = list(j.employment_kinds)
    if j.languages and (overwrite or not row.languages):
        row.languages = j.languages
        _sync_languages(s, row.id, j.languages)
    if j.specialization_id and (overwrite or row.specialization_id is None):
        row.specialization_id = j.specialization_id
        row.specialization_confidence = j.specialization_confidence
        _sync_specializations(s, row.id, j.specialization_id, j.specialization_confidence, j.secondary_specializations)
    if j.seniority != "UNKNOWN" and (overwrite or row.seniority == "UNKNOWN"):
        row.seniority = j.seniority
    if (j.salary_min is not None or j.salary_max is not None) and (
        overwrite or (row.salary_min is None and row.salary_max is None)
    ):
        row.salary_basis, row.salary_confidence = j.salary_basis, j.salary_confidence
    if overwrite:
        row.description_hash = j.description_hash
        row.quality_score, row.quality = j.quality_score, j.quality
        row.norm_version = NORMALIZATION_VERSION


def _set_skills(s: Session, job_id: int, skills: list[tuple[str, str]]) -> None:
    s.execute(JobSkill.__table__.delete().where(JobSkill.job_id == job_id))
    if skills:
        s.execute(
            insert(JobSkill)
            .values([{"job_id": job_id, "skill": k, "category": c} for k, c in skills])
            .on_conflict_do_nothing()
        )


def upsert_jobs(
    s: Session, company_id: int, found: list[FoundJob], source_ids: dict[str, int]
) -> tuple[set[int], int, int]:
    """Returns (seen job ids, new, updated)."""
    t = now()
    url_counts: dict[str, int] = {}
    for f in found:
        url_counts[f.job.source_url] = url_counts.get(f.job.source_url, 0) + 1
    seen: set[int] = set()
    new = updated = 0
    company_country = s.execute(select(Company.country_code).where(Company.id == company_id)).scalar()
    geo_cache: dict = {}
    for f in found:
        j = f.job
        key = dedup_key(company_id, j, url_counts[j.source_url] > 1)
        posting_url = j.source_values.get("url") if isinstance(j.source_values.get("url"), str) else None
        canon = canonicalize(posting_url or j.source_url) if url_counts[j.source_url] == 1 or posting_url else None
        row = _find_existing(s, company_id, key, j, canon)
        if row is None:
            row = Job(
                company_id=company_id,
                dedup_key=key,
                title=j.title,
                title_norm=j.title_norm,
                source_url=j.source_url,
                canonical_url=canon,
                extraction_method=j.method,
                career_source_id=source_ids.get(f.source_url),
                ats_type=j.ats_type,
                ats_job_id=j.ats_job_id,
                confidence=j.confidence,
                content_hash=j.content_hash,
                source_values=j.source_values,
                discovered_at=t,
                last_seen_at=t,
                last_checked_at=t,
                status="open",
                remote_type="unknown",
                work_model="UNKNOWN",
                employment_kinds=["UNKNOWN"],
                seniority="UNKNOWN",
                geo_status="unknown",
                remote_scope="unknown",
                country_codes=[],
                region_codes=[],
                location_ids=[],
                remote_countries=[],
                languages=[],
                norm_version=NORMALIZATION_VERSION,
            )
            _apply(row, j, overwrite=True)
            s.add(row)
            s.flush()
            _apply_derived(s, row, j, overwrite=True, company_country=company_country, cache=geo_cache)
            events.emit(s, events.CREATED, row.id)
            new += 1
        else:
            if row.id in seen:
                continue
            better = (
                METHOD_RANK.get(j.method, 0) >= METHOD_RANK.get(row.extraction_method, 0)
                and j.confidence >= (row.confidence or 0) - 0.1
            )
            before = events.snapshot(row)
            _apply(row, j, overwrite=better)
            _apply_derived(s, row, j, overwrite=better, company_country=company_country, cache=geo_cache)
            if canon and canon != row.canonical_url and canon not in (row.alt_urls or []):
                row.alt_urls = [*(row.alt_urls or []), canon][-10:]
            if j.ats_type and j.ats_job_id and ":ats:" not in row.dedup_key:
                stronger = dedup_key(company_id, j, False)
                if s.execute(select(Job.id).where(Job.dedup_key == stronger)).first() is None:
                    row.dedup_key = stronger
            row.career_source_id = source_ids.get(f.source_url, row.career_source_id)
            row.last_seen_at = row.last_checked_at = t
            row.miss_count = 0
            reopened = False
            if row.status != "open":
                # an expired posting whose own end date still lies in the past is not "active again"
                if not (row.status == "expired" and row.valid_through is not None and row.valid_through < t):
                    row.status, row.closed_at = "open", None
                    reopened = True
            if reopened:
                events.emit(s, events.REOPENED, row.id)
            if row.status == "open":
                fields = events.changed_fields(before, events.snapshot(row))
                if fields:
                    events.emit(s, events.UPDATED, row.id, {"fields": fields})
            updated += 1
        seen.add(row.id)
        _set_skills(s, row.id, j.skills)
    return seen, new, updated


def touch_unchanged(s: Session, company_id: int, urls: list[str]) -> set[int]:
    if not urls:
        return set()
    ids = (
        s.execute(
            update(Job)
            .where(Job.company_id == company_id, Job.status == "open")
            .where(or_(Job.canonical_url.in_(urls), Job.source_url.in_(urls)))
            .values(last_seen_at=now(), last_checked_at=now(), miss_count=0)
            .returning(Job.id)
        )
        .scalars()
        .all()
    )
    return set(ids)


def apply_lifecycle(
    s: Session,
    company_id: int,
    result: ScanResult,
    seen: set[int],
    source_ids: dict[str, int],
    settings: CrawlerSettings,
    methods: tuple[str, ...] | None = None,
) -> int:
    """Misses only after *complete* reads; closing needs several misses and a minimum age."""
    t = now()
    expired = (
        s.execute(
            update(Job)
            .where(
                Job.company_id == company_id,
                Job.status == "open",
                Job.valid_through.is_not(None),
                Job.valid_through < t,
            )
            .values(status="expired", closed_at=t)
            .returning(Job.id)
        )
        .scalars()
        .all()
    )
    for jid in sorted(expired):
        events.emit(s, events.CLOSED, jid, {"reason": "expired"})
    if result.stop is not None:
        return 0
    scope = None
    if result.complete:
        scope = Job.company_id == company_id
        if methods:  # e.g. the browser pass judges only rendered postings
            scope = and_(scope, Job.extraction_method.in_(methods))
        elif (
            result.browser_urls
            or s.execute(select(SiteFingerprint.needs_browser).where(SiteFingerprint.company_id == company_id)).scalar()
        ):
            scope = and_(scope, Job.extraction_method != "browser")  # rendered postings: browser pass decides
    else:
        complete_sources = [
            source_ids[u]
            for u, src in result.sources.items()
            if src.kind == "ats_feed" and src.ok and src.complete and u in source_ids
        ]
        if complete_sources:
            scope = and_(Job.company_id == company_id, Job.career_source_id.in_(complete_sources))
    if scope is None:
        return 0
    cond = and_(scope, Job.status == "open")
    if seen:
        cond = and_(cond, Job.id.not_in(seen))
    s.execute(update(Job).where(cond).values(miss_count=Job.miss_count + 1, last_checked_at=t))
    closed = (
        s.execute(
            update(Job)
            .where(
                cond,
                Job.miss_count >= settings.close_after_misses,
                Job.last_seen_at < t - dt.timedelta(days=settings.close_min_days),
            )
            .values(status="closed", closed_at=t)
            .returning(Job.id)
        )
        .scalars()
        .all()
    )
    for jid in sorted(closed):
        events.emit(s, events.CLOSED, jid, {"reason": "missed"})
    return len(closed)


def save_pages(s: Session, company_id: int, result: ScanResult, settings: CrawlerSettings) -> bool:
    """Upsert page metadata. Returns True if any content changed."""
    t = now()
    changed_any = False
    for p in result.pages:
        raw_path = expires = None
        if p.raw is not None and settings.keep_raw_html:
            raw_path = _write_raw(company_id, p.url, p.raw)
            expires = t + dt.timedelta(days=settings.raw_html_retention_days)
        stmt = insert(CrawlPage).values(
            company_id=company_id,
            url=p.url,
            url_hash=url_hash(p.url),
            page_type=p.page_type,
            http_status=p.status,
            content_type=p.content_type,
            content_hash=p.content_hash,
            etag=p.etag,
            last_modified=p.last_modified,
            strategy=p.strategy,
            jobs_found=p.jobs_found,
            fetched_at=t,
            changed_at=t if p.changed else None,
            fetch_ms=p.fetch_ms,
            bytes=p.bytes,
            raw_path=raw_path,
            raw_expires_at=expires,
        )
        changed_expr = and_(
            stmt.excluded.content_hash.is_not(None),
            func.coalesce(CrawlPage.content_hash, "") != stmt.excluded.content_hash,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_crawl_pages_company_url",
            set_={
                "page_type": func.coalesce(stmt.excluded.page_type, CrawlPage.page_type),
                "http_status": stmt.excluded.http_status,
                "content_type": func.coalesce(stmt.excluded.content_type, CrawlPage.content_type),
                "content_hash": func.coalesce(stmt.excluded.content_hash, CrawlPage.content_hash),
                "etag": func.coalesce(stmt.excluded.etag, CrawlPage.etag),
                "last_modified": func.coalesce(stmt.excluded.last_modified, CrawlPage.last_modified),
                "strategy": func.coalesce(stmt.excluded.strategy, CrawlPage.strategy),
                "jobs_found": stmt.excluded.jobs_found,
                "fetched_at": t,
                "fetch_ms": stmt.excluded.fetch_ms,
                "bytes": stmt.excluded.bytes,
                "changed_at": case((changed_expr, t), else_=CrawlPage.changed_at),
                "change_count": CrawlPage.change_count + case((changed_expr, 1), else_=0),
                "raw_path": func.coalesce(stmt.excluded.raw_path, CrawlPage.raw_path),
                "raw_expires_at": func.coalesce(stmt.excluded.raw_expires_at, CrawlPage.raw_expires_at),
            },
        )
        s.execute(stmt)
        if p.changed and p.page_type in ("job_list", "job_detail", "career_landing", "no_vacancies", "pdf_job"):
            changed_any = True
    return changed_any


def _write_raw(company_id: int, url: str, body: bytes) -> str | None:
    from gcc_core.config import get_settings

    base = Path(get_settings().data_dir) / "crawl-raw" / str(company_id)
    try:
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{url_hash(url)}.html"
        path.write_bytes(body[: 2 * 1024 * 1024])
        return str(path)
    except OSError:
        return None


def save_errors(s: Session, company_id: int, run_id: int | None, result: ScanResult) -> None:
    rows = [
        {
            "company_id": company_id,
            "run_id": run_id,
            "url": (e.url or "")[:2000] or None,
            "stage": e.stage[:16],
            "kind": e.kind[:40],
            "message": (e.message or "")[:1000],
            "http_status": e.http_status,
            "strategy": e.strategy,
        }
        for e in result.errors[:MAX_ERRORS_PER_RUN]
    ]
    if rows:
        s.execute(insert(ExtractionError).values(rows))


def update_fingerprint(
    s: Session,
    company: CompanyInfo,
    result: ScanResult,
    *,
    new_jobs: int,
    changed: bool,
    settings: CrawlerSettings,
    full_discovery: bool,
) -> SiteFingerprint:
    t = now()
    fp = s.get(SiteFingerprint, company.id)
    if fp is None:
        fp = SiteFingerprint(
            company_id=company.id,
            domain=company.domain,
            strategy_stats={},
            career_urls=[],
            technologies=[],
            error_streak=0,
            scans=0,
            open_jobs=0,
        )
        s.add(fp)
    stats = dict(fp.strategy_stats or {})
    for name, st in result.strategy_stats.items():
        cur = dict(stats.get(name) or {"attempts": 0, "ok": 0, "jobs": 0})
        cur["attempts"] += st["attempts"]
        cur["ok"] += st["ok"]
        cur["jobs"] += st["jobs"]
        if st["ok"]:
            cur["last_ok"] = t.isoformat()
        cur["rate"] = round(cur["ok"] / max(1, cur["attempts"]), 3)
        stats[name] = cur
    fp.strategy_stats = stats
    best = result.best_strategy
    if best:
        fp.best_strategy = best
    feeds = [src for src in result.sources.values() if src.kind == "ats_feed" and src.ok]
    if feeds:
        top = max(feeds, key=lambda x: x.job_count)
        fp.ats_type, fp.ats_account = top.ats_type, top.ats_account
    elif (
        full_discovery
        and result.stop is None
        and any(src.kind == "ats_feed" and not src.ok for src in result.sources.values())
    ):
        fp.ats_type = fp.ats_account = None
    pages = [src.url for src in result.sources.values() if src.kind in ("job_list", "career_page")]
    boards = [src.evidence.get("board") for src in feeds if src.evidence.get("board")]
    if result.career_found or pages or boards or result.browser_urls:
        urls = list(dict.fromkeys([*boards, *pages, *result.browser_urls]))
        fp.career_urls = urls[:10]
    elif full_discovery and result.stop is None and result.outcome == "no_career_page":
        fp.career_urls = []
    if result.browser_urls:
        fp.needs_browser = True
    elif best in ("ats", "jsonld", "html", "content"):
        fp.needs_browser = False
    fp.technologies = sorted(set(fp.technologies or []) | set(result.technologies))[:20]
    lat = [p.fetch_ms for p in result.pages if p.fetch_ms]
    if lat:
        avg = sum(lat) / len(lat)
        fp.avg_latency_ms = round(avg if fp.avg_latency_ms is None else 0.7 * fp.avg_latency_ms + 0.3 * avg, 1)
    retry = None
    if result.stop is not None:
        fp.error_streak = (fp.error_streak or 0) + 1
        retry = result.stop.retry_after_s
        if result.stop.kind == "rate_limited":
            fp.last_429_at = t
            fp.rate_limited_until = t + dt.timedelta(seconds=retry or 600)
    else:
        fp.error_streak = 0
        fp.last_success_at = t
    if full_discovery and result.stop is None:
        fp.full_discovery_at = t
    if changed:
        fp.last_change_at = t
    if new_jobs:
        fp.last_new_job_at = t
    fp.scans = (fp.scans or 0) + 1
    fp.last_scan_at = t
    fp.open_jobs = s.execute(
        select(func.count()).select_from(Job).where(Job.company_id == company.id, Job.status == "open")
    ).scalar_one()
    outcome = result.outcome
    if result.stop is not None and result.stop.kind not in ("rate_limited", "access_restricted", "robots_unavailable"):
        outcome = "unreachable"
    fp.interval_h = next_interval_h(
        settings,
        outcome=outcome,
        new_jobs=new_jobs,
        open_jobs=fp.open_jobs,
        changed=changed,
        last_new_job_at=fp.last_new_job_at,
        last_change_at=fp.last_change_at,
        error_streak=fp.error_streak,
        retry_after_s=retry,
        now=t,
    )
    fp.next_scan_at = t + dt.timedelta(hours=fp.interval_h)
    return fp


def persist(
    s: Session,
    company: CompanyInfo,
    run_id: int | None,
    result: ScanResult,
    settings: CrawlerSettings,
    *,
    full_discovery: bool,
    lifecycle_methods: tuple[str, ...] | None = None,
) -> dict:
    source_ids = _source_ids(s, company.id, result)
    seen, new, updated = upsert_jobs(s, company.id, result.jobs, source_ids)
    seen |= touch_unchanged(s, company.id, result.unchanged_job_urls)
    closed = apply_lifecycle(s, company.id, result, seen, source_ids, settings, lifecycle_methods)
    changed = save_pages(s, company.id, result, settings) or new > 0
    save_errors(s, company.id, run_id, result)
    fp = update_fingerprint(
        s, company, result, new_jobs=new, changed=changed, settings=settings, full_discovery=full_discovery
    )
    stats = {
        "jobs_found": len(result.jobs),
        "jobs_new": new,
        "jobs_updated": updated,
        "jobs_closed": closed,
        "jobs_seen": len(seen),
        "next_scan_at": fp.next_scan_at.isoformat() if fp.next_scan_at else None,
    }
    if run_id is not None:
        status = (
            "failed"
            if result.stop is not None and not result.jobs
            else ("partial" if (not result.complete or result.browser_urls) else "done")
        )
        s.execute(
            update(CrawlRun)
            .where(CrawlRun.id == run_id)
            .values(
                status=status,
                finished_at=now(),
                heartbeat_at=now(),
                pages_fetched=result.pages_fetched,
                pages_unchanged=result.pages_unchanged,
                jobs_found=len(result.jobs),
                jobs_new=new,
                jobs_closed=closed,
                strategy=result.best_strategy,
                complete=result.complete,
                outcome=result.outcome,
                browser_pending=bool(result.browser_urls) and settings.browser_enabled,
                browser_urls=result.browser_urls,
                checkpoint={},
                stats={
                    **stats,
                    "strategies": result.strategy_stats,
                    "elapsed_s": result.elapsed_s,
                    "errors": len(result.errors),
                },
            )
        )
    return stats
