"""Vacancy exports: CSV (the table) and YAML (every stored field, full description).

Both are streamed in batches with their own read-only session, so a large export never
holds all rows in memory and never depends on the request's database session staying open.
The rows are exactly those of the Vacancies table for the same filters.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections.abc import Iterator

import yaml
from sqlalchemy import select

from gcc_core.db import session_scope
from gcc_core.models import Company

from .models import Job, JobSkill

BATCH = 200
MAX_ROWS = 10_000  # hard cap per download; the header says when it was reached

CSV_COLUMNS = [
    "id",
    "company",
    "company_domain",
    "title",
    "city",
    "postcode",
    "country",
    "location_raw",
    "remote_type",
    "employment_types",
    "date_posted",
    "discovered_at",
    "last_seen_at",
    "status",
    "source_url",
    "application_url",
    "extraction_method",
    "confidence",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "skills",
]
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value) -> str | int | float:
    """Text from third-party websites must not turn into spreadsheet formulas when opened."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        value = "; ".join(str(v) for v in value)
    s = str(value).replace("\r\n", "\n")
    return "'" + s if s.startswith(_FORMULA_START) else s


def _skills_for(db, ids: list[int]) -> dict[int, list[tuple[str, str]]]:
    out: dict[int, list[tuple[str, str]]] = {}
    if ids:
        q = select(JobSkill.job_id, JobSkill.skill, JobSkill.category).where(JobSkill.job_id.in_(ids))
        for jid, skill, cat in db.execute(q.order_by(JobSkill.skill)):
            out.setdefault(jid, []).append((skill, cat))
    return out


def _batches(base, order, limit: int) -> Iterator[list[tuple[Job, Company, list[tuple[str, str]]]]]:
    with session_scope() as db:
        offset = 0
        while offset < limit:
            rows = db.execute(base.order_by(*order).limit(min(BATCH, limit - offset)).offset(offset)).all()
            if not rows:
                return
            skills = _skills_for(db, [j.id for j, _ in rows])
            yield [(j, c, skills.get(j.id, [])) for j, c in rows]
            offset += len(rows)


def csv_stream(base, order, limit: int, delimiter: str = ",") -> Iterator[bytes]:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\r\n")

    def flush() -> bytes:
        data = buf.getvalue().encode("utf-8")
        buf.seek(0)
        buf.truncate()
        return data

    yield b"\xef\xbb\xbf"  # UTF-8 BOM: Excel otherwise guesses the wrong encoding and breaks umlauts
    w.writerow(CSV_COLUMNS)
    yield flush()
    for batch in _batches(base, order, limit):
        for j, c, skills in batch:
            row = {
                **{k: getattr(j, k) for k in CSV_COLUMNS if hasattr(j, k)},
                "company": c.name,
                "company_domain": c.domain,
                "skills": [s for s, _ in skills],
            }
            w.writerow([csv_safe(row[k]) for k in CSV_COLUMNS])
        yield flush()


# --- YAML -------------------------------------------------------------------------------------


class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data):  # no &id001 anchors: every record stands on its own
        return True


def _str(dumper: yaml.SafeDumper, value: str):
    # Multi-line text (descriptions) as readable literal blocks; the emitter falls back to a
    # quoted form on its own where a block would not round-trip (e.g. trailing spaces).
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str)

_SKIP = {"search"}  # generated tsvector, not data


def job_record(j: Job, c: Company, skills: list[tuple[str, str]]) -> dict:
    """Every stored column of the vacancy, the company it belongs to, and its skills."""
    rec: dict = {col.key: getattr(j, col.key) for col in Job.__table__.columns if col.key not in _SKIP}
    rec["company"] = {"id": c.id, "name": c.name, "domain": c.domain, "website_url": c.website_url}
    rec["skills"] = [{"skill": s, "category": cat} for s, cat in skills]
    return rec


def dump(obj) -> str:
    return yaml.dump(obj, Dumper=_Dumper, allow_unicode=True, sort_keys=False, default_flow_style=False, width=100)


def yaml_stream(base, order, limit: int, *, total: int, filters: dict) -> Iterator[bytes]:
    exported = min(total, limit)
    yield dump(
        {
            "exported_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
            "filters": filters,
            "total_matching": total,
            "exported": exported,
            "truncated": total > exported,
        }
    ).encode()
    if exported == 0:
        yield b"jobs: []\n"
        return
    yield b"jobs:\n"
    for batch in _batches(base, order, limit):
        yield dump([job_record(j, c, s) for j, c, s in batch]).encode()


def single_yaml(j: Job, c: Company, skills: list[tuple[str, str]]) -> bytes:
    return dump(job_record(j, c, skills)).encode()
