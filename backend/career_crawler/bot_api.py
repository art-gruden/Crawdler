"""Versioned, read-only API for bots and other machine clients: ``/api/bot/v1``.

Authentication is a Bearer token (see ``gcc_core.botauth``); the administrator session is not accepted.
Responses use a normalized public schema and expose no crawler internals (dedup keys, extraction
method, miss counters, content hashes, raw source values).
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from gcc_core.botauth import bot_auth
from gcc_core.config import Settings, get_settings
from gcc_core.db import get_db
from gcc_core.geo_data import COUNTRIES, OTHER_COUNTRIES, REGIONS
from gcc_core.models import Company, GeoLocation

from . import search as srch
from .extract import taxonomy
from .extract.normalize import EMPLOYMENT_KINDS
from .extract.skills import CEFR_ORDER, canonical_skill, skill_category
from .models import Job, JobSkill

router = APIRouter(
    prefix="/api/bot/v1",
    tags=["bot-api"],
    dependencies=[Depends(bot_auth)],
    responses={401: {"description": "Missing or invalid API token"}, 429: {"description": "Rate limit exceeded"}},
)

# --- schema ---------------------------------------------------------------------------------------------


class CompanyRef(BaseModel):
    id: int
    name: str
    domain: str | None = None
    country: str | None = Field(None, description="ISO 3166-1 alpha-2 of the headquarters, null when unknown")


class LocationOut(BaseModel):
    id: int
    country: str
    country_name: str | None
    region: str | None = Field(None, description="ISO 3166-2 code, e.g. DE-SN, AT-9, CH-ZH")
    region_name: str | None
    city: str | None
    postcode: str | None
    is_primary: bool
    confidence: float
    source: str = Field(description="structured | gazetteer | text | context | legacy")


class RemoteEligibility(BaseModel):
    scope: Literal["unknown", "worldwide", "europe", "eu", "dach", "countries"]
    countries: list[str] = Field(description="DACH countries a remote employee may live in; [] with scope worldwide/unknown")


class SpecializationOut(BaseModel):
    id: int
    slug: str
    name: str
    name_de: str
    group: str | None
    is_it: bool
    confidence: float | None = None


class SkillOut(BaseModel):
    name: str
    category: str | None


class LanguageOut(BaseModel):
    language: str = Field(description="ISO 639-1")
    level: Literal["A1", "A2", "B1", "B2", "C1", "C2"] | None = Field(
        None, description="CEFR level, present only when the posting wrote one"
    )
    requirement: Literal["required", "preferred", "unknown"]
    descriptor: Literal["native", "fluent", "advanced", "good", "basic"] | None = None
    evidence: str | None = None
    confidence: float


class SalaryOut(BaseModel):
    min: float | None
    max: float | None
    currency: str | None = Field(None, description="ISO 4217; null when the posting named none (never assumed)")
    period: str | None = Field(None, description="year | month | hour … exactly as stated, never converted")
    basis: Literal["gross", "net", "unknown"]
    raw: str | None
    confidence: float | None


class QualityOut(BaseModel):
    score: float | None
    completeness: float | None
    missing: list[str] = Field(default_factory=list, description="unknown fields; missing is not negative")
    field_confidence: dict[str, float] = Field(default_factory=dict)


class JobOut(BaseModel):
    id: int
    title: str
    company: CompanyRef
    url: str
    application_url: str | None
    status: Literal["open", "closed", "expired"]
    published_at: dt.date | None = Field(None, description="from the source only, never the discovery date")
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime
    last_checked_at: dt.datetime | None
    updated_at: dt.datetime
    closed_at: dt.datetime | None
    geo_status: Literal["resolved", "partial", "ambiguous", "unknown"]
    locations: list[LocationOut]
    work_model: Literal["REMOTE", "HYBRID", "ONSITE", "UNKNOWN"]
    remote_eligibility: RemoteEligibility
    employment_types: list[str]
    specialization: SpecializationOut | None
    secondary_specializations: list[SpecializationOut] = Field(default_factory=list)
    seniority: Literal["INTERN", "JUNIOR", "MID", "SENIOR", "LEAD", "EXECUTIVE", "UNKNOWN"]
    skills: list[SkillOut]
    languages: list[LanguageOut]
    salary: SalaryOut | None
    quality: QualityOut
    language_fit: Literal["compatible", "unknown", "exceeds"] | None = Field(
        None, description="only when languages were given in the request"
    )
    relevance: int | None = Field(None, description="only with sort=relevance")


class JobDetail(JobOut):
    description: str | None
    requirements: list[str]
    responsibilities: list[str]


class JobPage(BaseModel):
    items: list[JobOut]
    next_cursor: str | None = Field(description="pass as cursor to get the next page; null on the last page")
    has_more: bool


class ChangeItem(BaseModel):
    event_id: int
    event_type: Literal["job.created", "job.updated", "job.closed", "job.reopened"]
    job_id: int
    occurred_at: dt.datetime
    fields: list[str] | None = Field(None, description="job.updated only: which fields changed meaningfully")
    reason: str | None = Field(None, description="job.closed only: missed | expired")


class ChangesPage(BaseModel):
    items: list[ChangeItem]
    next_cursor: str = Field(description="persist this and send it on the next poll")
    has_more: bool


class CountryFacet(BaseModel):
    code: str
    name: str
    name_local: str
    in_dach: bool
    open_jobs: int
    companies: int


class RegionFacet(BaseModel):
    code: str
    country: str
    name: str
    name_local: str
    kind: str
    open_jobs: int


class CityFacet(BaseModel):
    id: int
    city: str
    country: str
    region: str | None
    open_jobs: int


class SpecializationFacet(BaseModel):
    id: int
    slug: str
    name: str
    name_de: str
    is_it: bool
    open_jobs: int
    children: list[SpecializationFacet] = Field(default_factory=list)


class SkillFacet(BaseModel):
    name: str
    category: str | None
    open_jobs: int


# --- parameters ---------------------------------------------------------------------------------------------


def _csv(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        out += [x.strip() for x in v.split(",") if x.strip()]
    return out


_COUNTRY_CODES = set(COUNTRIES) | set(OTHER_COUNTRIES)


def _enum(values: list[str], allowed, name: str) -> list[str]:
    vals = [v.upper() for v in values]
    bad = [v for v in vals if v not in allowed]
    if bad:
        raise HTTPException(422, f"{name}: unknown value(s) {', '.join(bad)}; allowed: {', '.join(sorted(allowed))}")
    return vals


def search_params(
    q: str | None = Query(None, max_length=200, description="Full text; supports \"phrases\" and -excluded words"),
    exclude: list[str] | None = Query(None, description="Words that must not occur (comma separated or repeated)"),
    country: list[str] | None = Query(None, description="ISO 3166-1 codes: DE, AT, CH"),
    region: list[str] | None = Query(None, description="ISO 3166-2 codes: DE-SN, AT-9, CH-ZH"),
    city: list[int] | None = Query(None, description="Location ids from /facets/cities"),
    specialization: list[str] | None = Query(None, description="Slugs or ids from /facets/specializations"),
    work_model: list[str] | None = Query(None, description="REMOTE, HYBRID, ONSITE"),
    employment: list[str] | None = Query(None, description="FULL_TIME, PART_TIME, CONTRACT, FREELANCE, TEMPORARY, INTERNSHIP, APPRENTICESHIP, WORKING_STUDENT, OTHER"),
    seniority: list[str] | None = Query(None, description="INTERN, JUNIOR, MID, SENIOR, LEAD, EXECUTIVE"),
    skills: list[str] | None = Query(None, description="Skill names from /facets/skills"),
    skills_mode: Literal["any", "all"] = "any",
    languages: str | None = Query(None, description="Candidate languages, e.g. de:B1,en:C1 (level optional)"),
    lang_mode: Literal["inclusive", "strict"] = "inclusive",
    salary_min: float | None = Query(None, ge=0),
    currency: str | None = Query(None, min_length=3, max_length=3, description="ISO 4217; required with salary_min"),
    salary_period: str = Query("year", description="year | month | hour …; salaries are never converted"),
    new_since: dt.datetime | None = Query(None, description="Only vacancies first seen after this instant (ISO 8601)"),
    employer: list[int] | None = Query(None, description="Company ids"),
    status: Literal["open", "closed", "expired", "all"] = "open",
    remote_in: list[str] | None = Query(None, description="Country codes a remote employee would live in"),
    include_unknown: list[str] | None = Query(
        None, description="Filters for which vacancies with an unknown value still match: " + ", ".join(srch.UNKNOWN_FILTERS)
    ),
    sort: Literal["newest", "relevance", "salary_desc"] = "newest",
) -> srch.SearchParams:
    try:
        reg = _enum(_csv(region), set(REGIONS), "region")
        spec = srch.expand_specializations(_csv(specialization)) if specialization else []
        skill_names = []
        unknown_skills = []
        for name in _csv(skills):
            c = canonical_skill(name)
            (skill_names if c else unknown_skills).append(c or name)
        if unknown_skills:
            raise HTTPException(422, f"skills: unknown {', '.join(unknown_skills)}; see /facets/skills")
        langs: dict[str, str | None] = {}
        for item in _csv([languages] if languages else None):
            code, _, lvl = item.partition(":")
            code = code.strip().lower()
            if not (2 <= len(code) <= 3 and code.isalpha()):
                raise HTTPException(422, f"languages: bad language code '{code}'")
            langs[code] = lvl.strip().upper() or None
        p = srch.SearchParams(
            q=q.strip() if q and q.strip() else None,
            exclude=_csv(exclude),
            countries=_enum(_csv(country), _COUNTRY_CODES, "country"),
            regions=reg,
            city_ids=list(city or []),
            specializations=spec,
            work_models=_enum(_csv(work_model), {"REMOTE", "HYBRID", "ONSITE"}, "work_model"),
            employment=_enum(_csv(employment), set(EMPLOYMENT_KINDS), "employment"),
            seniority=_enum(_csv(seniority), set(taxonomy.SENIORITIES), "seniority"),
            skills=skill_names,
            skills_mode=skills_mode,
            languages=langs,
            lang_mode=lang_mode,
            salary_min=salary_min,
            currency=currency.upper() if currency else None,
            salary_period=salary_period.lower(),
            new_since=new_since if new_since is None or new_since.tzinfo else new_since.replace(tzinfo=dt.UTC),
            employers=list(employer or []),
            status=status,
            remote_in=_enum(_csv(remote_in), _COUNTRY_CODES, "remote_in"),
            include_unknown={x.lower() for x in _csv(include_unknown)},
            sort=sort,
        )
        p.validate()
        return p
    except srch.SearchError as exc:
        raise HTTPException(422, str(exc)) from exc


# --- serialisation ---------------------------------------------------------------------------------------------


def _country_name(code: str | None) -> str | None:
    if code in COUNTRIES:
        return COUNTRIES[code]["name"]
    return OTHER_COUNTRIES[code][0] if code in OTHER_COUNTRIES else None


def _spec(spec_id: int | None, conf: float | None = None) -> SpecializationOut | None:
    d = taxonomy.public_spec(spec_id)
    return SpecializationOut(**d, confidence=conf) if d else None


def build_jobs(
    s: Session, ids: list[int], *, detail: bool = False, fit: dict | None = None, scores: dict | None = None
) -> list[JobOut]:
    if not ids:
        return []
    rows = s.execute(
        select(Job, Company.name, Company.domain, Company.country_code)
        .join(Company, Company.id == Job.company_id)
        .where(Job.id.in_(ids))
    ).all()
    by_id = {j.id: (j, n, d, c) for j, n, d, c in rows}
    locs: dict[int, list[LocationOut]] = {i: [] for i in ids}
    for r in s.execute(
        text(
            "SELECT jl.job_id, g.id, g.country_code, g.region_code, g.city, jl.postcode, jl.is_primary, jl.confidence, jl.source "
            "FROM job_locations jl JOIN geo_locations g ON g.id = jl.location_id "
            "WHERE jl.job_id = ANY(:ids) ORDER BY jl.job_id, jl.is_primary DESC, g.id"
        ),
        {"ids": ids},
    ):
        locs[r[0]].append(
            LocationOut(
                id=r[1],
                country=r[2],
                country_name=_country_name(r[2]),
                region=r[3],
                region_name=REGIONS[r[3]][1] if r[3] in REGIONS else None,
                city=r[4],
                postcode=r[5],
                is_primary=r[6],
                confidence=round(r[7], 2),
                source=r[8],
            )
        )
    skills: dict[int, list[SkillOut]] = {i: [] for i in ids}
    for jid, skill, cat in s.execute(
        select(JobSkill.job_id, JobSkill.skill, JobSkill.category).where(JobSkill.job_id.in_(ids)).order_by(JobSkill.skill)
    ):
        skills[jid].append(SkillOut(name=skill, category=cat))
    secondary: dict[int, list[SpecializationOut]] = {i: [] for i in ids}
    for jid, sid, conf in s.execute(
        text(
            "SELECT job_id, specialization_id, confidence FROM job_specializations "
            "WHERE job_id = ANY(:ids) AND NOT is_primary ORDER BY job_id, confidence DESC, specialization_id"
        ),
        {"ids": ids},
    ):
        sp = _spec(sid, conf)
        if sp:
            secondary[jid].append(sp)
    out: list[JobOut] = []
    for jid in ids:
        if jid not in by_id:
            continue
        j, cname, cdomain, ccountry = by_id[jid]
        qd = dict(j.quality or {})
        fc = {k: v for k, v in (qd.get("field_confidence") or {}).items() if k != "extraction"}
        has_salary = j.salary_min is not None or j.salary_max is not None
        payload = dict(
            id=j.id,
            title=j.title,
            company=CompanyRef(id=j.company_id, name=cname, domain=cdomain, country=ccountry),
            url=j.canonical_url or j.source_url,
            application_url=j.application_url,
            status=j.status,
            published_at=j.date_posted,
            first_seen_at=j.discovered_at,
            last_seen_at=j.last_seen_at,
            last_checked_at=j.last_checked_at,
            updated_at=j.updated_at,
            closed_at=j.closed_at,
            geo_status=j.geo_status,
            locations=locs[jid],
            work_model=j.work_model,
            remote_eligibility=RemoteEligibility(scope=j.remote_scope, countries=list(j.remote_countries or [])),
            employment_types=list(j.employment_kinds or ["UNKNOWN"]),
            specialization=_spec(j.specialization_id, j.specialization_confidence),
            secondary_specializations=secondary[jid],
            seniority=j.seniority,
            skills=skills[jid],
            languages=[
                LanguageOut(
                    language=x["language"],
                    level=x.get("level"),
                    requirement=x.get("requirement") or ("required" if x.get("required") else "preferred"),
                    descriptor=x.get("descriptor"),
                    evidence=x.get("evidence") or x.get("raw"),
                    confidence=x.get("confidence", 0.5),
                )
                for x in (j.languages or [])
            ],
            salary=SalaryOut(
                min=j.salary_min,
                max=j.salary_max,
                currency=j.salary_currency,
                period=j.salary_period,
                basis=j.salary_basis,
                raw=j.salary_raw,
                confidence=j.salary_confidence,
            )
            if has_salary
            else None,
            quality=QualityOut(
                score=j.quality_score, completeness=qd.get("completeness"), missing=qd.get("missing", []), field_confidence=fc
            ),
            language_fit=(fit or {}).get(jid),
            relevance=(scores or {}).get(jid),
        )
        if detail:
            out.append(
                JobDetail(
                    **payload,
                    description=j.description,
                    requirements=list(j.requirements or []),
                    responsibilities=list(j.responsibilities or []),
                )
            )
        else:
            out.append(JobOut(**payload))
    return out


# --- endpoints ---------------------------------------------------------------------------------------------------


@router.get("/jobs", response_model=JobPage, summary="Search vacancies")
def search_jobs(
    response: Response,
    p: srch.SearchParams = Depends(search_params),
    limit: int = Query(25, ge=1, le=100),
    cursor: str | None = Query(None, description="Opaque cursor from a previous page"),
    s: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Filters combine with AND; several values of one filter combine with OR. Unknown values never match a
    filter unless named in ``include_unknown``. Pagination is keyset based and stable while data changes."""
    limit = min(limit, settings.bot_max_page_size)
    try:
        weights = srch.load_weights(settings.bot_rank_weights)
    except ValueError as exc:
        raise HTTPException(503, "Ranking is misconfigured") from exc
    try:
        page = srch.run_search(s, p, limit=limit, cursor=cursor, weights=weights)
    except srch.SearchError as exc:
        raise HTTPException(422, str(exc)) from exc
    fit = srch.language_fit_for(s, p, page.ids)
    items = build_jobs(s, page.ids, fit=fit, scores=page.scores)
    response.headers["Cache-Control"] = "no-store"
    return JobPage(items=items, next_cursor=page.next_cursor, has_more=page.has_more)


@router.get("/jobs/{job_id}", response_model=JobDetail, summary="One vacancy with the full description")
def get_job(job_id: int, response: Response, s: Session = Depends(get_db)):
    items = build_jobs(s, [job_id], detail=True)
    if not items:
        raise HTTPException(404, "Vacancy not found")
    response.headers["Cache-Control"] = "no-store"
    return items[0]


def _facet_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "private, max-age=60"


@router.get("/facets/countries", response_model=list[CountryFacet], summary="Countries with open vacancy counts")
def facet_countries(response: Response, s: Session = Depends(get_db)):
    _facet_headers(response)
    jobs = dict(
        s.execute(
            text("SELECT cc, count(*) FROM jobs, unnest(country_codes) AS cc WHERE status = 'open' GROUP BY cc")
        ).all()
    )
    companies = dict(
        s.execute(
            text(
                "SELECT country_code, count(*) FROM companies WHERE country_code IS NOT NULL "
                "AND verification_status = 'verified' GROUP BY country_code"
            )
        ).all()
    )
    rows = s.execute(text("SELECT code, name, name_local, in_dach FROM geo_countries ORDER BY in_dach DESC, code")).all()
    return [
        CountryFacet(code=c, name=n, name_local=nl, in_dach=d, open_jobs=jobs.get(c, 0), companies=companies.get(c, 0))
        for c, n, nl, d in rows
        if d or jobs.get(c)
    ]


@router.get("/facets/regions", response_model=list[RegionFacet], summary="Regions (Länder / cantons) of a country")
def facet_regions(response: Response, country: str = Query(..., min_length=2, max_length=2), s: Session = Depends(get_db)):
    _facet_headers(response)
    country = country.upper()
    if country not in _COUNTRY_CODES:
        raise HTTPException(422, f"unknown country '{country}'")
    counts = dict(
        s.execute(
            text("SELECT rc, count(*) FROM jobs, unnest(region_codes) AS rc WHERE status = 'open' GROUP BY rc")
        ).all()
    )
    rows = s.execute(
        text("SELECT code, country_code, name, name_local, kind FROM geo_regions WHERE country_code = :c ORDER BY name_local"),
        {"c": country},
    ).all()
    return [RegionFacet(code=c, country=cc, name=n, name_local=nl, kind=k, open_jobs=counts.get(c, 0)) for c, cc, n, nl, k in rows]


@router.get("/facets/cities", response_model=list[CityFacet], summary="Cities with open vacancies")
def facet_cities(
    response: Response,
    country: str = Query(..., min_length=2, max_length=2),
    region: str | None = Query(None, description="ISO 3166-2 code"),
    q: str | None = Query(None, max_length=60, description="Name prefix (umlauts and ae/oe/ue are interchangeable)"),
    limit: int = Query(100, ge=1, le=500),
    s: Session = Depends(get_db),
):
    from gcc_core.geo import fold

    _facet_headers(response)
    country = country.upper()
    if country not in _COUNTRY_CODES:
        raise HTTPException(422, f"unknown country '{country}'")
    if region and region.upper() not in REGIONS:
        raise HTTPException(422, f"unknown region '{region}'")
    sql = (
        "SELECT g.id, g.city, g.country_code, g.region_code, count(DISTINCT jl.job_id) AS n "
        "FROM geo_locations g JOIN job_locations jl ON jl.location_id = g.id "
        "JOIN jobs j ON j.id = jl.job_id AND j.status = 'open' "
        "WHERE g.city IS NOT NULL AND g.country_code = :c "
    )
    args: dict = {"c": country, "n": limit}
    if region:
        sql += "AND g.region_code = :r "
        args["r"] = region.upper()
    if q and q.strip():
        sql += "AND g.search_name LIKE :p "
        args["p"] = fold(q) + "%"
    sql += "GROUP BY g.id ORDER BY n DESC, g.city LIMIT :n"
    return [CityFacet(id=i, city=c, country=cc, region=r, open_jobs=n) for i, c, cc, r, n in s.execute(text(sql), args)]


@router.get("/facets/specializations", response_model=list[SpecializationFacet], summary="Specialization taxonomy with counts")
def facet_specializations(response: Response, s: Session = Depends(get_db)):
    _facet_headers(response)
    leaf = dict(
        s.execute(
            text(
                "SELECT js.specialization_id, count(*) FROM job_specializations js "
                "JOIN jobs j ON j.id = js.job_id AND j.status = 'open' GROUP BY js.specialization_id"
            )
        ).all()
    )
    group = dict(
        s.execute(
            text(
                "SELECT sp.parent_id, count(DISTINCT js.job_id) FROM job_specializations js "
                "JOIN specializations sp ON sp.id = js.specialization_id "
                "JOIN jobs j ON j.id = js.job_id AND j.status = 'open' WHERE sp.parent_id IS NOT NULL GROUP BY sp.parent_id"
            )
        ).all()
    )
    out = []
    for g in taxonomy.GROUPS:
        kids = [
            SpecializationFacet(id=x.id, slug=x.slug, name=x.name_en, name_de=x.name_de, is_it=x.is_it, open_jobs=leaf.get(x.id, 0))
            for x in taxonomy.LEAVES
            if x.group == g.id
        ]
        out.append(
            SpecializationFacet(
                id=g.id, slug=g.slug, name=g.name_en, name_de=g.name_de, is_it=g.is_it, open_jobs=group.get(g.id, 0), children=kids
            )
        )
    return out


@router.get("/facets/skills", response_model=list[SkillFacet], summary="Skills with open vacancy counts")
def facet_skills(
    response: Response,
    q: str | None = Query(None, max_length=40, description="Name prefix"),
    limit: int = Query(50, ge=1, le=200),
    s: Session = Depends(get_db),
):
    _facet_headers(response)
    stmt = (
        select(JobSkill.skill, func.max(JobSkill.category), func.count())
        .join(Job, Job.id == JobSkill.job_id)
        .where(Job.status == "open")
        .group_by(JobSkill.skill)
        .order_by(func.count().desc(), JobSkill.skill)
        .limit(limit)
    )
    if q and q.strip():
        stmt = stmt.where(func.lower(JobSkill.skill).like(q.strip().lower() + "%"))
    return [SkillFacet(name=n, category=c or skill_category(n), open_jobs=k) for n, c, k in s.execute(stmt)]


# --- change feed -------------------------------------------------------------------------------------------------


def _event_cursor(event_id: int) -> str:
    return srch.encode_cursor({"v": 1, "e": event_id})


def _event_id(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        data = srch.decode_cursor(cursor)
        eid = int(data["e"])
        if eid < 0:
            raise ValueError
        return eid
    except (srch.SearchError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, "invalid cursor") from exc


@router.get("/changes", response_model=ChangesPage, summary="Ordered, replayable vacancy change feed")
def changes(
    response: Response,
    cursor: str | None = Query(None, description="Omit to read from the beginning; otherwise the next_cursor you stored"),
    limit: int = Query(100, ge=1, le=500),
    s: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """At-least-once feed in commit order. Persist ``next_cursor`` after processing a page; re-reading with the
    same cursor returns the same events, so process by ``event_id`` idempotently."""
    limit = min(limit, settings.bot_max_changes_page)
    after = _event_id(cursor)
    head = s.execute(text("SELECT coalesce(max(id), 0) FROM job_events")).scalar_one()
    if after > head:
        raise HTTPException(409, "cursor is ahead of the event log (was the database restored?)")
    rows = s.execute(
        text("SELECT id, event_type, job_id, occurred_at, payload FROM job_events WHERE id > :a ORDER BY id LIMIT :n"),
        {"a": after, "n": limit + 1},
    ).all()
    more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for eid, etype, jid, at, payload in rows:
        payload = payload or {}
        items.append(
            ChangeItem(
                event_id=eid,
                event_type=etype,
                job_id=jid,
                occurred_at=at,
                fields=payload.get("fields"),
                reason=payload.get("reason"),
            )
        )
    response.headers["Cache-Control"] = "no-store"
    return ChangesPage(items=items, next_cursor=_event_cursor(rows[-1][0] if rows else after), has_more=more)


class Head(BaseModel):
    cursor: str = Field(description="cursor that points at 'now': use it to receive only future events")


@router.get("/changes/head", response_model=Head, summary="Cursor positioned after the newest event")
def changes_head(response: Response, s: Session = Depends(get_db)):
    head = s.execute(text("SELECT coalesce(max(id), 0) FROM job_events")).scalar_one()
    response.headers["Cache-Control"] = "no-store"
    return Head(cursor=_event_cursor(head))


SpecializationFacet.model_rebuild()
_ = (CEFR_ORDER, GeoLocation)  # re-exported for tests / docs tooling
