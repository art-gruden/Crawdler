"""Job runners. Kept free of Celery so they can be called directly in tests and scripts."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from gcc_core.config import get_settings
from gcc_core.db import SessionLocal, session_scope
from gcc_core.log import get_logger
from gcc_core.models import Company
from gcc_core.netsafe import NetPolicy, Resolver, system_resolver

from .csv_import import CsvFormatError, RowError, parse_rows
from .industries import get_industries
from .models import DiscoveryJob
from .overpass import (
    OSM_ATTRIBUTION,
    OSM_LICENSE,
    OverpassBudgetExceeded,
    OverpassClient,
    OverpassError,
    build_query,
    parse_response,
)
from .regions import expand_leaves, parent_state
from .service import Candidate, CompanyUpserter
from .verify import apply_result, verify_many

log = get_logger(__name__)
MAX_STORED_ERRORS = 100
BATCH = 500

Dispatch = Callable[[list[int]], None]


def import_path(job_id: int) -> Path:
    return Path(get_settings().data_dir) / "imports" / f"{job_id}.csv"


def _default_dispatch(ids: list[int]) -> None:
    from .tasks import verify_companies

    verify_companies.delay(ids)


def _cancel_requested(job_id: int) -> bool:
    with SessionLocal() as s:
        return bool(s.execute(select(DiscoveryJob.cancel_requested).where(DiscoveryJob.id == job_id)).scalar())


def _start(job_id: int) -> DiscoveryJob | None:
    with session_scope() as s:
        job = s.execute(select(DiscoveryJob).where(DiscoveryJob.id == job_id).with_for_update()).scalar_one_or_none()
        if job is None or job.status != "pending":
            return None
        if job.cancel_requested:
            job.status = "cancelled"
            job.finished_at = dt.datetime.now(dt.UTC)
            return None
        job.status = "running"
        job.started_at = dt.datetime.now(dt.UTC)
        s.flush()
        s.expunge(job)
        return job


def _save(job_id: int, *, stats: Counter, errors: list, **fields) -> None:
    with session_scope() as s:
        values = {"stats": dict(stats), "errors": errors[:MAX_STORED_ERRORS], **fields}
        s.execute(update(DiscoveryJob).where(DiscoveryJob.id == job_id).values(**values))


def _finish(job_id: int, status: str, *, stats: Counter, errors: list, error: str | None = None) -> None:
    _save(job_id, stats=stats, errors=errors, status=status, error=error, finished_at=dt.datetime.now(dt.UTC))
    log.info("discovery.job_finished", job_id=job_id, status=status, **{k: v for k, v in stats.items()})


def _queue_verification(company_ids: list[int], dispatch: Dispatch, stats: Counter) -> None:
    """Lease the unverified companies touched by a job and dispatch verification batches."""
    if not company_ids:
        return
    settings = get_settings()
    with session_scope() as s:
        leased = lease_for_verification(s, ids=company_ids, limit=len(company_ids), only_unverified=True)
    for i in range(0, len(leased), settings.verify_batch_size):
        dispatch(leased[i : i + settings.verify_batch_size])
    if leased:
        stats["verification_queued"] += len(leased)


def lease_for_verification(
    session: Session, *, ids: list[int] | None = None, limit: int = 200, only_unverified: bool = False
) -> list[int]:
    """Atomically pick companies due for verification and push next_verify_at 2h ahead (a lease)."""
    where = ["domain IS NOT NULL", "website_url IS NOT NULL", "verification_status <> 'no_website'"]
    params: dict = {"limit": limit}
    if ids is not None:
        where.append("id = ANY(:ids)")
        params["ids"] = ids
    if only_unverified:
        where.append("verification_status = 'unverified' AND (next_verify_at IS NULL OR next_verify_at <= now())")
    else:
        where.append("next_verify_at IS NOT NULL AND next_verify_at <= now()")
    sql = f"""
        UPDATE companies SET next_verify_at = now() + interval '2 hours'
        WHERE id IN (
            SELECT id FROM companies WHERE {" AND ".join(where)}
            ORDER BY next_verify_at NULLS FIRST, id
            LIMIT :limit FOR UPDATE SKIP LOCKED
        )
        RETURNING id
    """
    return [r[0] for r in session.execute(text(sql), params)]


# --------------------------------------------------------------------------- OSM


def run_osm_job(
    job_id: int,
    *,
    client: OverpassClient | None = None,
    dispatch: Dispatch = _default_dispatch,
) -> None:
    job = _start(job_id)
    if job is None:
        return
    settings = get_settings()
    stats: Counter = Counter()
    errors: list = []
    try:
        regions = expand_leaves(job.params.get("regions") or [])
        industries = get_industries(job.params.get("industries"))
    except KeyError as exc:
        _finish(job_id, "failed", stats=stats, errors=errors, error=str(exc))
        return
    if client is None:
        client = OverpassClient(settings, SessionLocal, cancelled=lambda: _cancel_requested(job_id))
    stats["steps_total"] = len(regions)
    touched: list[int] = []
    upserter: CompanyUpserter | None = None
    newest_base: dt.datetime | None = None
    failed_steps = 0

    for region in regions:
        if _cancel_requested(job_id):
            _finish(job_id, "cancelled", stats=stats, errors=errors)
            return
        stats_region = {"region": region.code, "name": region.name}
        _save(job_id, stats=stats, errors=errors, params={**job.params, "current_region": region.name})
        query = build_query(
            region, industries, timeout_s=settings.overpass_query_timeout_s, maxsize_mb=settings.overpass_maxsize_mb
        )
        try:
            payload, meta = client.run(query)
            parsed = parse_response(payload, industries)
        except OverpassBudgetExceeded as exc:
            errors.append({**stats_region, "error": str(exc)})
            _finish(job_id, "failed", stats=stats, errors=errors, error=str(exc))
            _queue_verification(touched, dispatch, stats)
            return
        except OverpassError as exc:
            if str(exc) == "cancelled":
                _finish(job_id, "cancelled", stats=stats, errors=errors)
                return
            failed_steps += 1
            errors.append({**stats_region, "error": str(exc)[:500]})
            stats["steps_done"] += 1
            stats["steps_failed"] += 1
            continue
        if meta.from_cache:
            stats["cache_hits"] += 1
        else:
            stats["overpass_requests"] += meta.attempts
        stats["overpass_bytes"] += meta.bytes
        if not parsed.area_ids:
            failed_steps += 1
            errors.append({**stats_region, "error": "region boundary not found in OSM (check the Kreisschlüssel)"})
            stats["steps_done"] += 1
            stats["steps_failed"] += 1
            continue
        if parsed.osm_base and (newest_base is None or parsed.osm_base > newest_base):
            newest_base = parsed.osm_base
        stats["elements"] += parsed.raw_elements - len(parsed.area_ids)
        state = parent_state(region)
        for i in range(0, len(parsed.companies), BATCH):
            with session_scope() as s:
                if upserter is None:
                    upserter = CompanyUpserter(s, job_id=job_id, allowed_ports=settings.allowed_ports)
                upserter.session = s
                for oc in parsed.companies[i : i + BATCH]:
                    t = oc.tags
                    street = " ".join(x for x in (t.get("addr:street"), t.get("addr:housenumber")) if x) or None
                    city = t.get("addr:city") or (region.name if region.kind == "kreisfreie_stadt" else None)
                    cand = Candidate(
                        source="osm",
                        source_record_id=oc.record_id,
                        source_url=oc.url,
                        name=oc.name,
                        websites=oc.websites,
                        street=street,
                        postcode=t.get("addr:postcode"),
                        city=city,
                        district_key=region.code,
                        district_name=region.name,
                        state_code=state.code,
                        lat=oc.lat,
                        lon=oc.lon,
                        industries=oc.industries,
                        data=t,
                        license=OSM_LICENSE,
                        attribution=OSM_ATTRIBUTION,
                        data_timestamp=parsed.osm_base,
                    )
                    try:
                        with s.begin_nested():
                            res = upserter.upsert(cand, region_code=region.code)
                    except Exception as exc:  # one bad record must not abort the region
                        log.warning("discovery.record_failed", record=oc.record_id, error=repr(exc))
                        stats["error"] += 1
                        errors.append({**stats_region, "record": oc.record_id, "error": repr(exc)[:300]})
                        continue
                    stats[res.outcome] += 1
                    stats["records"] += 1
                    if res.company_id:
                        touched.append(res.company_id)
        stats["steps_done"] += 1
        _save(job_id, stats=stats, errors=errors, data_timestamp=newest_base)

    params = {k: v for k, v in job.params.items() if k != "current_region"}
    if job.params.get("verify", True):
        _queue_verification(list(dict.fromkeys(touched)), dispatch, stats)
    status = "failed" if regions and failed_steps == len(regions) else "completed"
    _save(job_id, stats=stats, errors=errors, params=params, data_timestamp=newest_base)
    _finish(job_id, status, stats=stats, errors=errors, error="all regions failed" if status == "failed" else None)


# --------------------------------------------------------------------------- CSV


def run_csv_job(job_id: int, *, dispatch: Dispatch = _default_dispatch) -> None:
    job = _start(job_id)
    if job is None:
        return
    settings = get_settings()
    stats: Counter = Counter()
    errors: list = []
    path = import_path(job_id)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        _finish(job_id, "failed", stats=stats, errors=errors, error=f"upload not found: {exc}")
        return
    touched: list[int] = []
    upserter: CompanyUpserter | None = None
    try:
        rows = parse_rows(
            raw,
            profile=job.params.get("profile", "generic"),
            max_rows=settings.import_max_rows,
            source=job.source,
            license=job.license,
            attribution=job.attribution,
            source_url=job.source_url,
            use_source_ids=bool(job.params.get("use_source_ids")),
        )
        batch: list[Candidate] = []

        def flush(items: list[Candidate]) -> None:
            nonlocal upserter
            with session_scope() as s:
                if upserter is None:
                    upserter = CompanyUpserter(s, job_id=job_id, allowed_ports=settings.allowed_ports)
                upserter.session = s
                for cand in items:
                    try:
                        with s.begin_nested():
                            res = upserter.upsert(cand, region_code=cand.district_key)
                    except Exception as exc:
                        stats["error"] += 1
                        errors.append({"name": cand.name[:100], "error": repr(exc)[:300]})
                        continue
                    stats[res.outcome] += 1
                    stats["records"] += 1
                    if res.company_id:
                        touched.append(res.company_id)

        for item in rows:
            if isinstance(item, RowError):
                stats["row_errors"] += 1
                errors.append({"line": item.line, "error": item.message})
                continue
            batch.append(item)
            if len(batch) >= BATCH:
                if _cancel_requested(job_id):
                    _finish(job_id, "cancelled", stats=stats, errors=errors)
                    return
                flush(batch)
                batch = []
                _save(job_id, stats=stats, errors=errors)
        if batch:
            flush(batch)
    except CsvFormatError as exc:
        _finish(job_id, "failed", stats=stats, errors=errors, error=str(exc))
        return
    if job.params.get("verify", True):
        _queue_verification(list(dict.fromkeys(touched)), dispatch, stats)
    _finish(job_id, "completed", stats=stats, errors=errors)
    try:
        path.unlink()  # the parsed content now lives in the database; keep only the checksum
    except OSError:
        pass


# --------------------------------------------------------------------------- verification


def verify_company_ids(ids: list[int], *, resolver: Resolver = system_resolver) -> Counter:
    settings = get_settings()
    policy = NetPolicy.from_settings(settings)
    with SessionLocal() as s:
        rows = s.execute(
            select(Company.id, Company.website_url, Company.name, Company.domain).where(
                Company.id.in_(ids), Company.website_url.is_not(None)
            )
        ).all()
    results = asyncio.run(verify_many([tuple(r) for r in rows], policy=policy, settings=settings, resolver=resolver))
    counts: Counter = Counter()
    with session_scope() as s:
        for cid, result in results.items():
            company = s.get(Company, cid)
            if company is None:
                continue
            queued = apply_result(s, company, result, settings=settings)
            counts[company.verification_status] += 1
            if queued == "queued":
                counts["queued_for_scan"] += 1
    log.info("verify.batch_done", **dict(counts))
    return counts
