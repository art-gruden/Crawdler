"""Insert-or-update of discovered companies with domain deduplication and provenance.

Matching order for each candidate record:
1. the same source record seen before (e.g. OSM node/123)  → that company ("updated")
2. the same registrable domain                            → existing company ("duplicate_existing",
                                                              or "duplicate_in_job" if this job created it)
3. no usable website: same normalized name + postcode/city → existing company (conservative fallback)
4. otherwise                                               → new company ("new")

Existing values are never overwritten by a later source; empty fields are filled in.
Manually edited companies (is_manual) keep all their values.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from gcc_core.models import Company, CompanySource
from gcc_core.urls import NormalizedUrl, first_valid_website

from .models import DiscoveryJobItem
from .regions import fold

LEGAL_FORMS = re.compile(
    r"\b(gmbh\s*&\s*co\.?\s*kg(aa)?|gmbh|mbh|ag|kg|ohg|gbr|ug|haftungsbeschrankt|e\.?\s?k\.?|e\.?\s?v\.?|"
    r"se|kgaa|co\.?|inh\.?|ltd\.?|inc\.?|llc|partg(mbb)?|stiftung)\b",
    re.I,
)


def name_key(name: str) -> str:
    folded = fold(name.replace("&", " und "))
    folded = LEGAL_FORMS.sub(" ", folded)
    folded = re.sub(r"\b(und|the|der|die|das)\b", " ", folded)
    return re.sub(r"\s+", " ", folded).strip() or fold(name)


@dataclass
class Candidate:
    source: str
    source_record_id: str | None
    name: str
    websites: list[str | None]
    source_url: str | None = None
    street: str | None = None
    postcode: str | None = None
    city: str | None = None
    district_key: str | None = None
    district_name: str | None = None
    state_code: str | None = None
    lat: float | None = None
    lon: float | None = None
    industries: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    license: str | None = None
    attribution: str | None = None
    data_timestamp: dt.datetime | None = None

    def record_id(self) -> str:
        if self.source_record_id:
            return self.source_record_id
        basis = "|".join([name_key(self.name), *(w or "" for w in self.websites), self.postcode or "", self.city or ""])
        return "sha1:" + hashlib.sha1(basis.encode()).hexdigest()


@dataclass
class UpsertResult:
    outcome: str
    company_id: int | None
    domain: str | None
    is_new_company: bool


FILLABLE = ("street", "postcode", "city", "district_key", "district_name", "state_code", "lat", "lon")


class CompanyUpserter:
    """Stateful per job, so duplicates within one job are recognised."""

    def __init__(self, session: Session, *, job_id: int | None, allowed_ports: set[int] | frozenset[int]):
        self.session = session
        self.job_id = job_id
        self.allowed_ports = frozenset(allowed_ports)
        self.created_in_job: set[int] = set()
        self.seen_in_job: set[int] = set()
        self.new_company_ids: list[int] = []

    def _source_company(self, source: str, record_id: str) -> int | None:
        return self.session.execute(
            select(CompanySource.company_id).where(
                CompanySource.source == source, CompanySource.source_record_id == record_id
            )
        ).scalar_one_or_none()

    def _insert_company(self, cand: Candidate, url: NormalizedUrl | None, reject: str | None) -> int | None:
        now = dt.datetime.now(dt.UTC)
        values = {
            "name": cand.name.strip(),
            "name_key": name_key(cand.name),
            "website_url": url.url if url else None,
            "domain": url.domain if url else None,
            "website_raw": next((w for w in cand.websites if w), None),
            "website_rejected_reason": None if url else reject,
            "verification_status": "unverified" if url else "no_website",
            "next_verify_at": now if url else None,
            "industries": cand.industries,
            "first_seen_at": now,
            "last_seen_at": now,
            **{k: getattr(cand, k) for k in FILLABLE},
        }
        stmt = insert(Company).values(**values)
        if url:
            stmt = stmt.on_conflict_do_nothing(index_elements=["domain"])
        return self.session.execute(stmt.returning(Company.id)).scalar_one_or_none()

    def _merge(self, company_id: int, cand: Candidate, url: NormalizedUrl | None) -> None:
        company = self.session.get(Company, company_id)
        if company is None:
            return
        company.last_seen_at = dt.datetime.now(dt.UTC)
        if company.is_manual:
            return
        for attr in FILLABLE:
            if getattr(company, attr) in (None, "") and getattr(cand, attr) not in (None, ""):
                setattr(company, attr, getattr(cand, attr))
        if cand.industries:
            merged = list(dict.fromkeys([*(company.industries or []), *cand.industries]))
            if merged != company.industries:
                company.industries = merged
        if url and not company.domain:
            taken = self.session.execute(select(Company.id).where(Company.domain == url.domain)).scalar_one_or_none()
            if taken is None:
                company.website_url = url.url
                company.domain = url.domain
                company.website_rejected_reason = None
                company.verification_status = "unverified"
                company.next_verify_at = dt.datetime.now(dt.UTC)
        if cand.name and name_key(cand.name) != company.name_key and cand.name not in (company.alt_names or []):
            company.alt_names = [*(company.alt_names or []), cand.name][:20]

    def _fallback_match(self, cand: Candidate) -> int | None:
        key = name_key(cand.name)
        q = select(Company.id).where(Company.name_key == key)
        if cand.postcode:
            q = q.where(Company.postcode == cand.postcode)
        elif cand.city:
            q = q.where(func.lower(Company.city) == cand.city.lower())
        else:
            return None
        return self.session.execute(q.limit(1)).scalar_one_or_none()

    def _record_source(self, company_id: int, cand: Candidate, record_id: str) -> None:
        now = dt.datetime.now(dt.UTC)
        stmt = insert(CompanySource).values(
            company_id=company_id,
            source=cand.source,
            source_record_id=record_id,
            source_url=cand.source_url,
            name=cand.name,
            license=cand.license,
            attribution=cand.attribution,
            data=cand.data,
            data_timestamp=cand.data_timestamp,
            discovery_job_id=self.job_id,
            first_seen_at=now,
            last_seen_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_company_sources_record",
            set_={
                "company_id": company_id,
                "name": stmt.excluded.name,
                "data": stmt.excluded.data,
                "data_timestamp": stmt.excluded.data_timestamp,
                "discovery_job_id": stmt.excluded.discovery_job_id,
                "license": stmt.excluded.license,
                "attribution": stmt.excluded.attribution,
                "source_url": stmt.excluded.source_url,
                "last_seen_at": now,
            },
        )
        self.session.execute(stmt)

    def upsert(self, cand: Candidate, *, region_code: str | None = None) -> UpsertResult:
        url, reject = first_valid_website(cand.websites, allowed_ports=self.allowed_ports)
        record_id = cand.record_id()
        domain = url.domain if url else None

        company_id = self._source_company(cand.source, record_id)

        if url is None:
            # No usable website: keep the company (it may get one later), report the URL problem.
            if company_id is None:
                company_id = self._fallback_match(cand)
            is_new = company_id is None
            if is_new:
                company_id = self._insert_company(cand, None, reject)
                self.created_in_job.add(company_id)
                self.new_company_ids.append(company_id)
            else:
                self._merge(company_id, cand, None)
            outcome = "no_website" if reject == "empty" else "invalid_url"
            self.seen_in_job.add(company_id)
            self._record_source(company_id, cand, record_id)
            self._item(company_id, cand, outcome, None, record_id, region_code, reject)
            return UpsertResult(outcome, company_id, None, is_new)

        outcome: str
        if company_id is not None:
            outcome = "updated"
        else:
            company_id = self.session.execute(
                select(Company.id).where(Company.domain == url.domain)
            ).scalar_one_or_none()
            if company_id is not None:
                outcome = "duplicate_in_job" if company_id in self.seen_in_job else "duplicate_existing"
            else:
                company_id = self._insert_company(cand, url, None)
                if company_id is None:  # inserted concurrently by another job
                    company_id = self.session.execute(
                        select(Company.id).where(Company.domain == url.domain)
                    ).scalar_one()
                    outcome = "duplicate_existing"
                else:
                    outcome = "new"

        if outcome == "new":
            self.created_in_job.add(company_id)
            self.new_company_ids.append(company_id)
        else:
            self._merge(company_id, cand, url)
        self.seen_in_job.add(company_id)
        self._record_source(company_id, cand, record_id)
        self._item(company_id, cand, outcome, domain, record_id, region_code, None)
        return UpsertResult(outcome, company_id, domain, outcome == "new")

    def _item(self, company_id, cand: Candidate, outcome, domain, record_id, region_code, detail) -> None:
        if self.job_id is None:
            return
        self.session.add(
            DiscoveryJobItem(
                job_id=self.job_id,
                company_id=company_id,
                outcome=outcome,
                name=cand.name[:500],
                domain=domain,
                website_raw=(next((w for w in cand.websites if w), None) or "")[:500] or None,
                source_record_id=record_id,
                region_code=region_code,
                detail=detail,
            )
        )
