"""Structured vacancy search in PostgreSQL (no external search engine).

Semantics (documented in docs/DACH.md and the OpenAPI schema)
--------------------------------------------------------------
* Values inside one filter are OR-ed (``country=DE,AT``); different filters are AND-ed. Location
  filters (country/region/city) refine each other; when several are given they must hold for the *same*
  work location of the vacancy.
* ``unknown`` is explicit: a vacancy whose attribute is unknown does **not** match a filter on that
  attribute, unless the caller opts in with ``include_unknown=<filter names>``. "Missing" never means
  "does not fit".
* Language fit compares a candidate's languages with the vacancy's *required* languages:
  ``compatible`` (every required language covered at a known sufficient level), ``unknown``
  (something required cannot be checked: no level stated, the vacancy states no requirement, or the
  candidate gave no level) and ``exceeds`` (a required language is missing or its stated CEFR level is
  higher than the candidate's). ``lang_mode=inclusive`` returns compatible + unknown (default),
  ``strict`` only compatible. ``exceeds`` is never returned.
* Salary is compared only within one currency and one period; nothing is converted.
* Ranking is deterministic and integer valued: ``round(10 * Σ weight_i * component_i)`` with the
  components in [0, 1] and the weights configurable (``BOT_RANK_WEIGHTS``). Ties break on
  first-seen time and id. Recency is evaluated at the ``as_of`` instant frozen in the cursor, so paging
  through results is stable.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field

from sqlalchemy import (
    Boolean,
    Float,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    not_,
    or_,
    select,
    tuple_,
)
from sqlalchemy.orm import Session

from .extract import taxonomy
from .extract.skills import CEFR_ORDER
from .models import Job, JobLanguage, JobLocation, JobSkill, JobSpecialization

DEFAULT_WEIGHTS = {
    "specialization": 25.0,
    "text": 20.0,
    "skills": 20.0,
    "location": 15.0,
    "work_model": 8.0,
    "language": 7.0,
    "recency": 5.0,
}
SORTS = ("newest", "relevance", "salary_desc")
UNKNOWN_FILTERS = (
    "location",
    "work_model",
    "employment",
    "seniority",
    "specialization",
    "salary",
    "remote",
)
STATUSES = ("open", "closed", "expired", "all")
PERIODS = ("year", "month", "week", "day", "hour")


class SearchError(ValueError):
    """Invalid combination of parameters (reported as HTTP 422)."""


@dataclass
class SearchParams:
    q: str | None = None
    exclude: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    city_ids: list[int] = field(default_factory=list)
    specializations: list[int] = field(default_factory=list)
    work_models: list[str] = field(default_factory=list)
    employment: list[str] = field(default_factory=list)
    seniority: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    skills_mode: str = "any"
    languages: dict[str, str | None] = field(default_factory=dict)
    lang_mode: str = "inclusive"
    salary_min: float | None = None
    currency: str | None = None
    salary_period: str = "year"
    new_since: dt.datetime | None = None
    employers: list[int] = field(default_factory=list)
    status: str = "open"
    remote_in: list[str] = field(default_factory=list)
    include_unknown: set[str] = field(default_factory=set)
    sort: str = "newest"

    def validate(self) -> None:
        if self.sort not in SORTS:
            raise SearchError(f"sort must be one of {', '.join(SORTS)}")
        if self.status not in STATUSES:
            raise SearchError(f"status must be one of {', '.join(STATUSES)}")
        if self.skills_mode not in ("any", "all"):
            raise SearchError("skills_mode must be 'any' or 'all'")
        if self.lang_mode not in ("inclusive", "strict"):
            raise SearchError("lang_mode must be 'inclusive' or 'strict'")
        bad = self.include_unknown - set(UNKNOWN_FILTERS)
        if bad:
            raise SearchError(f"include_unknown accepts: {', '.join(UNKNOWN_FILTERS)} (got {', '.join(sorted(bad))})")
        if self.salary_min is not None and not self.currency:
            raise SearchError("salary_min needs currency (salaries are never converted between currencies)")
        if self.salary_period not in PERIODS:
            raise SearchError(f"salary_period must be one of {', '.join(PERIODS)}")
        if self.sort == "salary_desc" and not self.currency:
            raise SearchError("sort=salary_desc needs currency (and optionally salary_period)")
        for lang, level in self.languages.items():
            if level is not None and level not in CEFR_ORDER:
                raise SearchError(f"unknown CEFR level '{level}' for language '{lang}'")

    def fingerprint(self) -> str:
        """Stable hash of everything that defines the result set (not paging)."""
        d = {k: (sorted(v) if isinstance(v, (set, list)) else v) for k, v in self.__dict__.items()}
        d["new_since"] = self.new_since.isoformat() if self.new_since else None
        d["languages"] = sorted(self.languages.items())
        return hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:12]


def load_weights(raw: str = "") -> dict[str, float]:
    weights = dict(DEFAULT_WEIGHTS)
    if raw.strip():
        try:
            over = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("BOT_RANK_WEIGHTS must be a JSON object") from exc
        for k, v in over.items():
            if k not in weights or not isinstance(v, (int, float)) or v < 0:
                raise ValueError(f"BOT_RANK_WEIGHTS: unknown or negative weight '{k}'")
            weights[k] = float(v)
    return weights


# --- cursor ---------------------------------------------------------------------------------------


def encode_cursor(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(token: str) -> dict:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("v") != 1:
            raise ValueError
        return data
    except Exception as exc:
        raise SearchError("invalid cursor") from exc


# --- filter conditions ------------------------------------------------------------------------------


def _unknown(p: SearchParams, name: str, known_cond, unknown_cond):
    return or_(known_cond, unknown_cond) if name in p.include_unknown else known_cond


def _location_condition(p: SearchParams):
    from gcc_core.models import GeoLocation

    levels = [bool(p.countries), bool(p.regions), bool(p.city_ids)]
    if sum(levels) == 0:
        return None
    if sum(levels) == 1:  # a single level: the denormalised GIN arrays are enough and fastest
        if p.countries:
            cond = Job.country_codes.overlap(p.countries)
        elif p.regions:
            cond = Job.region_codes.overlap(p.regions)
        else:
            cond = Job.location_ids.overlap(p.city_ids)
    else:  # several levels must hold for the same work location
        sub = (
            select(1)
            .select_from(JobLocation)
            .join(GeoLocation, GeoLocation.id == JobLocation.location_id)
            .where(JobLocation.job_id == Job.id)
        )
        if p.countries:
            sub = sub.where(GeoLocation.country_code.in_(p.countries))
        if p.regions:
            sub = sub.where(GeoLocation.region_code.in_(p.regions))
        if p.city_ids:
            sub = sub.where(GeoLocation.id.in_(p.city_ids))
        cond = exists(sub)
    return _unknown(p, "location", cond, func.cardinality(Job.location_ids) == 0)


def language_verdict(p: SearchParams):
    """SQL expression: 0 compatible, 1 unknown, 2 exceeds (for the required languages of a vacancy)."""
    if not p.languages:
        return None
    per_row = []
    for lang, level in p.languages.items():
        rank = CEFR_ORDER.get(level) if level else None
        if rank is None:
            row_v = literal(1)  # the candidate names the language without a level: cannot be checked
        else:
            row_v = case((JobLanguage.level_rank.is_(None), 1), (JobLanguage.level_rank > rank, 2), else_=0)
        per_row.append((JobLanguage.language == lang, row_v))
    verdict = case(*per_row, else_=2)  # a language the candidate does not have: exceeds
    # a requirement of unclear strength can at most be "unknown"
    verdict = case((JobLanguage.requirement == "unknown", func.least(verdict, 1)), else_=verdict)
    agg = (
        select(func.max(verdict))
        .where(JobLanguage.job_id == Job.id, JobLanguage.requirement.in_(("required", "unknown")))
        .correlate(Job)
        .scalar_subquery()
    )
    # no stated requirement at all -> nothing to compare -> unknown
    return func.coalesce(agg, 1)


def conditions(p: SearchParams) -> list:
    conds = []
    if p.status != "all":
        conds.append(Job.status == p.status)
    tsq = None
    if p.q:
        tsq = func.websearch_to_tsquery("simple", p.q)
        conds.append(Job.search.op("@@", return_type=Boolean)(tsq))
    if p.exclude:
        q = None
        for term in p.exclude:
            part = func.plainto_tsquery("simple", term)
            q = part if q is None else q.op("||")(part)
        conds.append(not_(Job.search.op("@@", return_type=Boolean)(q)))
    loc = _location_condition(p)
    if loc is not None:
        conds.append(loc)
    if p.work_models:
        conds.append(_unknown(p, "work_model", Job.work_model.in_(p.work_models), Job.work_model == "UNKNOWN"))
    if p.employment:
        conds.append(_unknown(p, "employment", Job.employment_kinds.overlap(p.employment), Job.employment_kinds.overlap(["UNKNOWN"])))
    if p.seniority:
        conds.append(_unknown(p, "seniority", Job.seniority.in_(p.seniority), Job.seniority == "UNKNOWN"))
    if p.specializations:
        has = exists(
            select(1).where(
                JobSpecialization.job_id == Job.id, JobSpecialization.specialization_id.in_(p.specializations)
            )
        )
        conds.append(_unknown(p, "specialization", has, Job.specialization_id.is_(None)))
    if p.skills:
        if p.skills_mode == "any":
            conds.append(exists(select(1).where(JobSkill.job_id == Job.id, JobSkill.skill.in_(p.skills))))
        else:
            matched = (
                select(func.count())
                .where(JobSkill.job_id == Job.id, JobSkill.skill.in_(p.skills))
                .correlate(Job)
                .scalar_subquery()
            )
            conds.append(matched == len(set(p.skills)))
    verdict = language_verdict(p)
    if verdict is not None:
        conds.append(verdict == 0 if p.lang_mode == "strict" else verdict <= 1)
    if p.currency:
        value = func.coalesce(Job.salary_max, Job.salary_min)
        known = and_(Job.salary_currency == p.currency, Job.salary_period == p.salary_period, value.is_not(None))
        if p.salary_min is not None:
            known = and_(known, value >= p.salary_min)
        if p.sort == "salary_desc":
            conds.append(known)  # sorting by salary only makes sense among comparable salaries
        else:
            conds.append(_unknown(p, "salary", known, and_(Job.salary_min.is_(None), Job.salary_max.is_(None))))
    if p.new_since:
        conds.append(Job.discovered_at > p.new_since)
    if p.employers:
        conds.append(Job.company_id.in_(p.employers))
    if p.remote_in:
        remote_job = Job.work_model.in_(("REMOTE", "HYBRID"))
        eligible = and_(remote_job, or_(Job.remote_scope == "worldwide", Job.remote_countries.overlap(p.remote_in)))
        conds.append(_unknown(p, "remote", eligible, and_(remote_job, Job.remote_scope == "unknown")))
    return conds


# --- ranking ------------------------------------------------------------------------------------------


def score_expression(p: SearchParams, weights: dict[str, float], as_of: dt.datetime):
    """Integer relevance score 0..1000 (for default weights summing to 100)."""
    parts = []
    if p.specializations:
        secondary = exists(
            select(1).where(
                JobSpecialization.job_id == Job.id, JobSpecialization.specialization_id.in_(p.specializations)
            )
        )
        comp = case((Job.specialization_id.in_(p.specializations), 1.0), (secondary, 0.6), else_=0.0)
        parts.append(weights["specialization"] * comp)
    if p.q:
        rank = func.ts_rank_cd(Job.search, func.websearch_to_tsquery("simple", p.q), 32)
        parts.append(weights["text"] * func.least(cast(1.0, Float), cast(rank, Float)))
    if p.skills:
        n = len(set(p.skills))
        matched = (
            select(func.count()).where(JobSkill.job_id == Job.id, JobSkill.skill.in_(p.skills)).correlate(Job).scalar_subquery()
        )
        parts.append(weights["skills"] * (cast(matched, Float) / n))
    if p.city_ids or p.regions or p.countries:
        comp = case(
            (Job.location_ids.overlap(p.city_ids) if p.city_ids else literal(False), 1.0),
            (Job.region_codes.overlap(p.regions) if p.regions else literal(False), 0.6),
            (Job.country_codes.overlap(p.countries) if p.countries else literal(False), 0.3),
            else_=0.0,
        )
        parts.append(weights["location"] * comp)
    if p.work_models:
        parts.append(weights["work_model"] * case((Job.work_model.in_(p.work_models), 1.0), else_=0.0))
    verdict = language_verdict(p)
    if verdict is not None:
        parts.append(weights["language"] * case((verdict == 0, 1.0), (verdict == 1, 0.4), else_=0.0))
    age_days = func.extract("epoch", literal(as_of) - Job.discovered_at) / 86400.0
    parts.append(weights["recency"] * (1.0 - func.least(cast(func.greatest(age_days, 0.0), Float), 30.0) / 30.0))
    total = parts[0]
    for part in parts[1:]:
        total = total + part
    return cast(func.round(cast(total * 10.0, Float)), Float)


# --- execution ----------------------------------------------------------------------------------------


@dataclass
class Page:
    ids: list[int]
    scores: dict[int, int]
    next_cursor: str | None
    has_more: bool
    as_of: dt.datetime


def _salary_key():
    return func.coalesce(Job.salary_max, Job.salary_min)


def run_search(
    s: Session,
    p: SearchParams,
    *,
    limit: int,
    cursor: str | None,
    weights: dict[str, float],
    capture: list | None = None,
) -> Page:
    p.validate()
    fp = p.fingerprint()
    as_of = dt.datetime.now(dt.UTC)
    after = None
    if cursor:
        c = decode_cursor(cursor)
        if c.get("h") != fp or c.get("s") != p.sort:
            raise SearchError("cursor does not belong to these search parameters")
        as_of = dt.datetime.fromisoformat(c["asof"])
        after = c["k"]
    conds = conditions(p)
    score = score_expression(p, weights, as_of) if p.sort == "relevance" else None
    if p.sort == "newest":
        keys = [Job.discovered_at, Job.id]
    elif p.sort == "relevance":
        keys = [score, Job.discovered_at, Job.id]
    else:
        keys = [_salary_key(), Job.discovered_at, Job.id]
    if after is not None:
        vals = []
        for expr, v in zip(keys, after, strict=True):
            if v is not None and expr is Job.discovered_at:
                v = dt.datetime.fromisoformat(v)
            vals.append(v)
        conds.append(tuple_(*keys) < tuple_(*[literal(v) for v in vals]))
    stmt = select(Job.id, *( [score.label("score")] if score is not None else [] ), Job.discovered_at, *( [_salary_key().label("sal")] if p.sort == "salary_desc" else [])).where(*conds)
    stmt = stmt.order_by(*[k.desc() for k in keys]).limit(limit + 1)
    if capture is not None:  # tooling (EXPLAIN scripts) gets the exact statement that will run
        capture.append(stmt)
    rows = s.execute(stmt).all()
    more = len(rows) > limit
    rows = rows[:limit]
    ids = [r.id for r in rows]
    scores = {r.id: int(r.score) for r in rows} if score is not None else {}
    next_cursor = None
    if rows and more:
        last = rows[-1]
        if p.sort == "newest":
            k = [last.discovered_at.isoformat(), last.id]
        elif p.sort == "relevance":
            k = [int(last.score), last.discovered_at.isoformat(), last.id]
        else:
            k = [last.sal, last.discovered_at.isoformat(), last.id]
        next_cursor = encode_cursor({"v": 1, "s": p.sort, "h": fp, "asof": as_of.isoformat(), "k": k})
    return Page(ids, scores, next_cursor, more, as_of)


def language_fit_for(s: Session, p: SearchParams, ids: list[int]) -> dict[int, str]:
    """compatible | unknown | exceeds for the jobs of a result page (for display)."""
    if not p.languages or not ids:
        return {}
    verdict = language_verdict(p)
    rows = s.execute(select(Job.id, verdict).where(Job.id.in_(ids))).all()
    names = {0: "compatible", 1: "unknown", 2: "exceeds"}
    return {jid: names[int(v)] for jid, v in rows}


def expand_specializations(values: list[str]) -> list[int]:
    try:
        return taxonomy.expand(values)
    except KeyError as exc:
        raise SearchError(str(exc).strip("'\"")) from exc
