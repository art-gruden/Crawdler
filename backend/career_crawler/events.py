"""Durable vacancy events (transactional outbox).

Guarantees
----------
* **Atomic**: events are inserted in the same database transaction as the state change. A rollback
  discards them; a commit makes both visible together.
* **Ordered by commit**: ``job_events.id`` is a sequence, and a sequence value is allocated at insert
  time, not at commit time, so two concurrent writers could commit "out of order" and a reader holding
  a cursor could skip an event for good. To prevent that the events are inserted in a ``before_commit``
  hook, right after taking one transaction-level advisory lock that is released only when the
  transaction ends. Writers therefore serialise for the few milliseconds between the hook and the
  COMMIT, and ``id`` order equals commit order. Readers need nothing special: a row they can see is
  never preceded by an invisible row with a smaller id.
* **Replayable / idempotent**: events are never updated or deleted by the application; a consumer
  resumes from any ``event_id``. Delivery is at-least-once from the consumer's point of view
  (re-reading a page returns the same events), so consumers de-duplicate on ``event_id``.
* **No Redis**: history lives in PostgreSQL only.

Semantics
---------
``job.created``   a canonical vacancy seen for the first time (rescans never emit it again)
``job.updated``   meaningful change only: title, apply URL, work model, employment type, salary,
                  location, remote eligibility, a *new* language requirement, or a significant
                  description change. Refreshing ``last_seen_at`` / ``last_checked_at`` emits nothing.
``job.closed``    the existing conservative closure (complete reads, repeated misses, minimum age) or
                  the source's own expiry date
``job.reopened``  a closed vacancy that is reliably active again
"""

from __future__ import annotations

import json
import re

from sqlalchemy import event, text
from sqlalchemy.orm import Session

ADVISORY_LOCK_KEY = 0x6A6F6265  # "jobe": namespace of the outbox lock
INFO_KEY = "job_events"

CREATED, UPDATED, CLOSED, REOPENED = "job.created", "job.updated", "job.closed", "job.reopened"


def emit(session: Session, event_type: str, job_id: int, payload: dict | None = None) -> None:
    """Queue an event on the session; it is written when (and only if) the transaction commits."""
    session.info.setdefault(INFO_KEY, []).append((event_type, job_id, payload or {}))


def pending(session: Session) -> list[tuple[str, int, dict]]:
    return list(session.info.get(INFO_KEY, []))


def _flush_events(session: Session) -> None:
    queued = session.info.pop(INFO_KEY, None)
    if not queued:
        return
    session.flush()  # state changes first, so the lock below is held for as short a time as possible
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
    session.execute(
        text("INSERT INTO job_events (event_type, job_id, payload) VALUES (:t, :j, CAST(:p AS jsonb))"),
        [{"t": t, "j": j, "p": json.dumps(p, ensure_ascii=False)} for t, j, p in queued],
    )


def _discard(session: Session) -> None:
    session.info.pop(INFO_KEY, None)


_installed = False


def install() -> None:
    """Register the session hooks once per process (idempotent)."""
    global _installed
    if _installed:
        return
    event.listen(Session, "before_commit", _flush_events)
    event.listen(Session, "after_rollback", _discard)
    _installed = True


install()


# --- what counts as a meaningful change --------------------------------------------------------

_WORD = re.compile(r"\w+", re.U)


def description_changed(old: str | None, new: str | None) -> bool:
    """True for a substantial rewrite; boilerplate noise (dates, counters, whitespace) is ignored."""
    if not old and not new:
        return False
    if not old or not new:
        return len(old or new or "") >= 200  # a real description appeared / vanished
    a = set(_WORD.findall(old.lower()))
    b = set(_WORD.findall(new.lower()))
    if not a or not b:
        return False
    jaccard = len(a & b) / len(a | b)
    size = abs(len(new) - len(old)) / max(len(old), 1)
    return jaccard < 0.90 or size > 0.15


def _lang_sig(languages: list[dict] | None) -> set[tuple]:
    return {(x.get("language"), x.get("level"), x.get("requirement") or ("required" if x.get("required") else "preferred"))
            for x in (languages or [])}


def snapshot(row) -> dict:
    """The tracked, comparable state of a Job (ORM row)."""
    return {
        "title": row.title,
        "application_url": row.application_url,
        "work_model": row.work_model,
        "employment": sorted(row.employment_kinds or []),
        "salary": (row.salary_min, row.salary_max, row.salary_currency, row.salary_period),
        "locations": sorted(row.location_ids or []),
        "remote_eligibility": (row.remote_scope, sorted(row.remote_countries or [])),
        "languages": _lang_sig(row.languages),
        "description": row.description,
    }


def changed_fields(before: dict, after: dict) -> list[str]:
    """Names of meaningfully changed fields between two snapshots (empty = nothing to announce)."""
    out = []
    for key in ("title", "application_url", "work_model", "employment", "salary", "locations", "remote_eligibility"):
        if before[key] != after[key]:
            # an unknown value being replaced by knowledge, or knowledge changing, both count;
            # losing knowledge (e.g. a re-read without a salary) never reaches here: merging keeps old values
            out.append(key)
    if after["languages"] - before["languages"]:
        out.append("languages")
    if description_changed(before["description"], after["description"]):
        out.append("description")
    return out
