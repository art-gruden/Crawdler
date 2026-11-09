"""Career-scan queue (outbox pattern).

Producers (discovery, dashboard) call :func:`request_scan`. The career crawler,
which lives in its own module, calls :func:`claim` / :func:`finish`. Neither side
imports the other; ``scan_requests`` is the contract.

Only companies whose website is verified may be queued: crawling is restricted to
verified domains.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .models import Company, ScanRequest

PRIORITY_MANUAL = 10
PRIORITY_DISCOVERY = 0


class NotScannable(Exception):
    pass


def request_scan(session: Session, company: Company, *, source: str, priority: int = PRIORITY_DISCOVERY) -> str:
    """Queue a company. Returns 'queued' or 'already_queued'. Caller commits."""
    if company.verification_status != "verified" or not company.domain:
        raise NotScannable(f"company {company.id} website is not verified ({company.verification_status})")
    stmt = (
        insert(ScanRequest)
        .values(company_id=company.id, source=source, priority=priority, status="pending")
        .on_conflict_do_nothing(index_elements=["company_id"], index_where=text("status IN ('pending', 'claimed')"))
        .returning(ScanRequest.id)
    )
    inserted = session.execute(stmt).scalar_one_or_none()
    if inserted is None:
        if priority > PRIORITY_DISCOVERY:  # a manual click bumps an existing pending request
            session.execute(
                update(ScanRequest)
                .where(ScanRequest.company_id == company.id, ScanRequest.status == "pending")
                .where(ScanRequest.priority < priority)
                .values(priority=priority)
            )
        return "already_queued"
    company.scan_status = "queued"
    return "queued"


def claim(session: Session, *, worker: str, limit: int = 10) -> list[ScanRequest]:
    """For the crawler: atomically claim the next pending requests (highest priority first)."""
    rows = (
        session.execute(
            select(ScanRequest)
            .where(ScanRequest.status == "pending")
            .order_by(ScanRequest.priority.desc(), ScanRequest.requested_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    now = dt.datetime.now(dt.UTC)
    for row in rows:
        row.status = "claimed"
        row.claimed_at = now
        row.claimed_by = worker
        row.attempts += 1
    if rows:
        session.execute(
            update(Company).where(Company.id.in_([r.company_id for r in rows])).values(scan_status="scanning")
        )
    return rows


def finish(session: Session, request_id: int, *, ok: bool, error: str | None = None) -> None:
    req = session.get(ScanRequest, request_id)
    if req is None:
        return
    now = dt.datetime.now(dt.UTC)
    req.status = "done" if ok else "failed"
    req.finished_at = now
    req.error = error
    session.execute(
        update(Company)
        .where(Company.id == req.company_id)
        .values(scan_status="done" if ok else "failed", last_scan_at=now)
    )
