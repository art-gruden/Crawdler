"""Fixtures for the DACH platform tests: postings are built as raw extractions, never fetched."""

from __future__ import annotations

from career_crawler import store
from career_crawler.engine import FoundJob
from career_crawler.extract.normalize import RawJob, normalize
from career_crawler.models import Job
from gcc_core.models import Company

from .crawler_helpers import make_company

BOT_TOKEN = "t" * 40 + "-bot-test-token"


def raw(title: str, host: str, n: int = 1, **kw) -> RawJob:
    kw.setdefault("source_url", f"https://{host}/jobs/{n}")
    kw.setdefault("method", "html")
    return RawJob(title=title, **kw)


def found(r: RawJob, *, country: str | None = None) -> FoundJob:
    job = normalize(r, context_country=country)
    assert job is not None
    return FoundJob(job=job, source_url=f"https://{r.source_url.split('/')[2]}/jobs", source_kind="html")


def ingest(db, company: Company, raws: list[RawJob]) -> list[int]:
    seen, _new, _upd = store.upsert_jobs(db, company.id, [found(r, country=company.country_code) for r in raws], {})
    db.commit()
    return sorted(seen)


def company(db, host: str, name: str | None = None, country: str | None = None) -> Company:
    c = make_company(db, host, name=name)
    c.country_code = country
    db.commit()
    return c


def job(db, jid: int) -> Job:
    db.expire_all()
    return db.get(Job, jid)
