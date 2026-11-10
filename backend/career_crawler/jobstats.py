"""Vacancy statistics: what the crawler *discovered*, counted once per canonical vacancy.

Definitions (also in ``docs/STATS.md``):

* **Newly discovered** = a row of ``jobs`` whose ``discovered_at`` lies in ``[start, end)``. One row is one
  canonical vacancy (``dedup_key`` is unique: the same posting reached through two URLs or two ATS feeds is
  one row), and ``discovered_at`` is set when the row is inserted and never changed. Rescans, updates and
  reopened vacancies therefore never count as new. This is "found by our crawler", not "published on that
  day": the source's own date (``date_posted``) is reported separately and never used for the headline.
* **Active** = ``status = 'open'`` now (as of the snapshot).
* **Updated / closed / reopened** = distinct vacancies with a ``job.updated`` / ``job.closed`` /
  ``job.reopened`` event in the period (the event log exists since the DACH deployment, see ``events_since``).
* All counting goes through the same filter functions, so the summary, charts, tables and the Telegram post
  agree. Everything is aggregated in PostgreSQL; only aggregate rows reach Python.

Geography uses one rule everywhere: a vacancy with exactly one distinct place is counted there; a vacancy
with several places is "Multi-location"; without places it is "Remote" (work model REMOTE) or "Unknown".
That makes the *unique* city breakdown add up to the total. The "coverage" mode counts a vacancy in every
place it lists and is therefore not additive.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, case, distinct, exists, func, literal, literal_column, null, or_, select
from sqlalchemy.orm import Session, aliased

from gcc_core.models import GeoCountry, GeoLocation, GeoRegion

from .extract import taxonomy
from .models import Job, JobEvent, JobLocation, JobSpecialization, Specialization

DACH = ("DE", "AT", "CH")
UNCLASSIFIED = "unclassified"
PERIOD_KEYS = ("today", "yesterday", "last_24h", "7d", "30d", "custom")
KPI_KEYS = ("today", "last_24h", "7d", "30d")
MAX_CUSTOM_DAYS = 366
MIN_DATE = dt.date(2000, 1, 1)
UTC = dt.UTC

KINDS = ("city", "no_city", "multi", "remote", "unknown")
KIND_LABEL = {
    "no_city": "City not specified",
    "multi": "Multi-location",
    "remote": "Remote (no location)",
    "unknown": "Unknown location",
}


class StatsError(ValueError):
    """Invalid input (period, filter, timezone). Mapped to HTTP 422 by the API."""


# --- time ---------------------------------------------------------------------------------------------


def get_tz(name: str | None, default: str = "Europe/Berlin") -> ZoneInfo:
    n = (name or default).strip()
    try:
        return ZoneInfo(n)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise StatsError(f"unknown timezone '{n[:64]}'") from exc


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


def parse_as_of(value: str | dt.datetime | None, now: dt.datetime | None = None) -> dt.datetime:
    """The snapshot time shared by all requests of one dashboard view (default: now)."""
    now = now or utcnow()
    if value is None or value == "":
        return now
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StatsError("as_of must be an ISO-8601 timestamp") from exc
    if value.tzinfo is None:
        raise StatsError("as_of must include a UTC offset")
    value = value.astimezone(UTC)
    if value > now + dt.timedelta(minutes=2):
        raise StatsError("as_of must not be in the future")
    return value


def _midnight(d: dt.date, tz: ZoneInfo) -> dt.datetime:
    """Start of the local calendar day, in UTC. Built from the local date, so DST days (23/25 h) are right."""
    return dt.datetime.combine(d, dt.time.min, tzinfo=tz).astimezone(UTC)


def parse_date(value: str | dt.date | None, name: str) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise StatsError(f"{name} must be a date like 2026-11-10") from exc


@dataclass(frozen=True)
class Period:
    key: str
    tz: str
    start: dt.datetime  # UTC, inclusive
    end: dt.datetime  # UTC, exclusive; may lie in the future for "today" / "7d" / "30d"
    upto: dt.datetime  # min(end, snapshot): what is actually counted
    prev_start: dt.datetime
    prev_end: dt.datetime  # same elapsed length as [start, upto)
    calendar: bool  # whole local calendar days (False for the rolling 24 h)

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def partial(self) -> bool:
        return self.upto < self.end

    def local(self, t: dt.datetime) -> dt.datetime:
        return t.astimezone(self.zone)

    def display(self) -> tuple[str, str]:
        """(first, last) as shown to people: local dates for calendar periods, local times for rolling ones."""
        if self.calendar:
            last = (self.local(self.end) - dt.timedelta(seconds=1)).date()
            return self.local(self.start).date().isoformat(), last.isoformat()
        fmt = "%Y-%m-%d %H:%M"
        return self.local(self.start).strftime(fmt), self.local(self.upto).strftime(fmt)

    def to_dict(self) -> dict[str, Any]:
        a, b = self.display()
        return {
            "key": self.key,
            "timezone": self.tz,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "counted_until": self.upto.isoformat(),
            "local_start": self.local(self.start).isoformat(),
            "local_end": self.local(self.end).isoformat(),
            "first": a,
            "last": b,
            "label": f"{self.local(self.start):%Y-%m-%d %H:%M} → {self.local(self.end):%Y-%m-%d %H:%M} ({self.tz}, end exclusive)",
            "calendar_days": self.calendar,
            "in_progress": self.partial,
            "previous": {"start": self.prev_start.isoformat(), "end": self.prev_end.isoformat()},
        }


def resolve_period(
    key: str,
    tz_name: str | None,
    snapshot: dt.datetime | None = None,
    date_from: str | dt.date | None = None,
    date_to: str | dt.date | None = None,
    default_tz: str = "Europe/Berlin",
) -> Period:
    """Resolve a named period in a time zone.

    today / yesterday: one local calendar day. 7d / 30d: the last 7 / 30 local calendar days *including today*.
    last_24h: the rolling 24 hours up to the snapshot. custom: ``date_from``..``date_to`` inclusive, local dates.
    The previous comparable period has the same elapsed length (a half-finished day is compared with the
    same part of the previous day, not with a whole one).
    """
    if key not in PERIOD_KEYS:
        raise StatsError(f"period must be one of {', '.join(PERIOD_KEYS)}")
    tz = get_tz(tz_name, default_tz)
    snap = (snapshot or utcnow()).astimezone(UTC)
    today = snap.astimezone(tz).date()

    if key == "last_24h":
        start, end = snap - dt.timedelta(hours=24), snap
        return Period(key, tz.key, start, end, end, start - dt.timedelta(hours=24), start, calendar=False)

    if key == "custom":
        d_from, d_to = parse_date(date_from, "from"), parse_date(date_to, "to")
        if d_from is None or d_to is None:
            raise StatsError("a custom period needs both 'from' and 'to' dates")
        if d_from > d_to:
            raise StatsError("'from' must not be after 'to'")
        if d_from < MIN_DATE:
            raise StatsError(f"'from' must not be before {MIN_DATE}")
        if (d_to - d_from).days + 1 > MAX_CUSTOM_DAYS:
            raise StatsError(f"a custom period may span at most {MAX_CUSTOM_DAYS} days")
        first, last = d_from, d_to
    elif key == "today":
        first = last = today
    elif key == "yesterday":
        first = last = today - dt.timedelta(days=1)
    else:
        n = 7 if key == "7d" else 30
        first, last = today - dt.timedelta(days=n - 1), today

    n_days = (last - first).days + 1
    start, end = _midnight(first, tz), _midnight(last + dt.timedelta(days=1), tz)
    upto = min(end, snap)
    if upto <= start:  # custom period entirely in the future: nothing to count, but stay well-formed
        upto = start
    prev_start = _midnight(first - dt.timedelta(days=n_days), tz)
    prev_end = prev_start + (upto - start)
    return Period(key, tz.key, start, end, upto, prev_start, prev_end, calendar=True)


# --- filters --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StatFilters:
    """Geography / specialization filters shared by every statistic. The time period is separate."""

    countries: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    cities: tuple[str, ...] = ()
    specializations: tuple[str, ...] = ()
    spec_ids: tuple[int, ...] = ()
    unclassified: bool = False
    exclude_initial_load: bool = False

    def as_dict(self) -> dict[str, Any]:
        d = {
            "countries": list(self.countries),
            "regions": list(self.regions),
            "cities": list(self.cities),
            "specializations": list(self.specializations),
            "exclude_initial_load": self.exclude_initial_load,
        }
        return {k: v for k, v in d.items() if v not in ([], False)}


def _codes(values: list[str] | None, pattern: str, what: str, upper: bool = True) -> tuple[str, ...]:
    import re

    out: list[str] = []
    for raw in values or []:
        for part in str(raw).split(","):
            v = part.strip()
            if not v:
                continue
            v = v.upper() if upper else v
            if not re.fullmatch(pattern, v):
                raise StatsError(f"invalid {what} '{part.strip()[:40]}'")
            if v not in out:
                out.append(v)
    return tuple(out)


def parse_filters(
    countries: list[str] | None = None,
    regions: list[str] | None = None,
    cities: list[str] | None = None,
    specializations: list[str] | None = None,
    exclude_initial_load: bool = False,
) -> StatFilters:
    cs = _codes(countries, r"[A-Z]{2}", "country code")
    rs = _codes(regions, r"[A-Z]{2}-[A-Z0-9]{1,3}", "region code")
    city_list: list[str] = []
    for c in cities or []:
        c = " ".join(str(c).split())
        if not c:
            continue
        if len(c) > 80:
            raise StatsError("city name is too long")
        if c.lower() not in [x.lower() for x in city_list]:
            city_list.append(c)
    specs: list[str] = []
    unclassified = False
    for s in specializations or []:
        for part in str(s).split(","):
            v = part.strip()
            if not v:
                continue
            if v.lower() == UNCLASSIFIED:
                unclassified = True
            elif v not in specs:
                specs.append(v)
    try:
        ids = tuple(taxonomy.expand(specs)) if specs else ()
    except KeyError as exc:
        raise StatsError(str(exc).strip("'\"")) from exc
    tokens = tuple(specs) + ((UNCLASSIFIED,) if unclassified else ())
    return StatFilters(cs, rs, tuple(city_list), tokens, ids, unclassified, bool(exclude_initial_load))


def dach_scope_condition():
    """"All DACH": vacancies in D/A/CH, plus those without any country yet. Vacancies located only outside
    DACH are left out. Unknown-country vacancies stay visible (and are labelled as such in the breakdowns)."""
    return or_(func.cardinality(Job.country_codes) == 0, Job.country_codes.overlap(list(DACH)))


def specialization_condition(spec_ids: tuple[int, ...] | list[int], unclassified: bool):
    parts = []
    if spec_ids:
        parts.append(Job.specialization_id.in_(list(spec_ids)))
    if unclassified:
        parts.append(Job.specialization_id.is_(None))
    if not parts:
        return None
    return or_(*parts)


def place_condition(city: str, countries=(), regions=()):
    """The vacancy lists a place with this city name (case-insensitive, exact). Country / region narrow the *place*,
    so "Neustadt" in Austria and in Germany stay distinct when a country is given."""
    q = (
        select(literal(1))
        .select_from(JobLocation.__table__.join(GeoLocation.__table__, GeoLocation.id == JobLocation.location_id))
        .where(JobLocation.job_id == Job.id, func.lower(GeoLocation.city) == city.lower())
        .correlate(Job)
    )
    if countries:
        q = q.where(GeoLocation.country_code.in_(list(countries)))
    if regions:
        q = q.where(GeoLocation.region_code.in_(list(regions)))
    return exists(q)


def filter_conditions(f: StatFilters) -> list:
    """Everything except the time window. Shared by every statistic and by the vacancy table's click-through."""
    conds: list = []
    if f.countries:
        conds.append(Job.country_codes.overlap(list(f.countries)))
    else:
        conds.append(dach_scope_condition())
    if f.regions:
        conds.append(Job.region_codes.overlap(list(f.regions)))
    if f.cities:
        conds.append(or_(*[place_condition(c, f.countries, f.regions) for c in f.cities]))
    spec = specialization_condition(f.spec_ids, f.unclassified)
    if spec is not None:
        conds.append(spec)
    return conds


def has_earlier_jobs(start: dt.datetime, company_col=None):
    """The company already had a vacancy discovered before ``start``. A company without one is in its initial
    catalogue load: those vacancies are "new to us", not necessarily new on the market."""
    o = aliased(Job)
    col = company_col if company_col is not None else Job.company_id
    q = select(literal(1)).where(o.company_id == col, o.discovered_at < start).correlate_except(o)
    return exists(q)


def window_cond(start, upto, f: StatFilters, cols=None):
    c = cols or Job
    conds = [c.discovered_at >= start, c.discovered_at < upto]
    if f.exclude_initial_load:
        conds.append(has_earlier_jobs(start, c.company_id))
    return and_(*conds)


# --- helpers ---------------------------------------------------------------------------------------------


def _pct(cur: int, prev: int) -> float | None:
    if prev <= 0:
        return None
    return round((cur - prev) / prev * 100, 1)


def _kpi_periods(tz: str, snapshot: dt.datetime) -> dict[str, Period]:
    return {k: resolve_period(k, tz, snapshot) for k in KPI_KEYS}


def count_new(db: Session, f: StatFilters, start: dt.datetime, upto: dt.datetime) -> int:
    stmt = select(func.count(Job.id)).where(*filter_conditions(f), window_cond(start, upto, f))
    return int(db.execute(stmt).scalar_one())


def count_active(db: Session, f: StatFilters, snapshot: dt.datetime) -> int:
    stmt = select(func.count(Job.id)).where(*filter_conditions(f), Job.status == "open", Job.discovered_at < snapshot)
    return int(db.execute(stmt).scalar_one())


def _spec_index(db: Session) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for s in db.execute(select(Specialization)).scalars():
        out[s.id] = {
            "id": s.id,
            "slug": s.slug,
            "name_en": s.name_en,
            "name_de": s.name_de,
            "parent_id": s.parent_id,
            "is_it": s.is_it,
            "sort_order": s.sort_order,
        }
    return out


def _geo_names(db: Session) -> tuple[dict[str, str], dict[str, str]]:
    countries = {c.code: c.name for c in db.execute(select(GeoCountry)).scalars()}
    regions = {r.code: r.name for r in db.execute(select(GeoRegion)).scalars()}
    return countries, regions


# --- unique-geography base ---------------------------------------------------------------------------------


def _job_base(conds: list, *, geo: bool):
    """One row per vacancy that matches ``conds`` with its (unique) place classification.

    kind: city | no_city | multi | remote | unknown (see the module docstring)."""
    cols = [
        Job.id.label("id"),
        Job.company_id.label("company_id"),
        Job.career_source_id.label("source_id"),
        Job.discovered_at.label("discovered_at"),
        Job.status.label("status"),
        Job.specialization_id.label("spec"),
    ]
    stmt = select(*cols).select_from(Job)
    if geo:
        # Uses the denormalised jobs.location_ids / country_codes (kept in step with job_locations by
        # geo_db.set_job_locations), so no per-vacancy lookup is needed: a plain join for single-place vacancies.
        gl = aliased(GeoLocation)
        n = func.cardinality(Job.location_ids)
        single = n == 1
        one_country = func.cardinality(Job.country_codes) == 1
        kind = case(
            (and_(single, gl.city.is_not(None)), "city"),
            (single, "no_city"),
            (n > 1, "multi"),
            (Job.work_model == "REMOTE", "remote"),
            else_="unknown",
        )
        country = case(
            (single, gl.country_code),
            (one_country, Job.country_codes[1]),
            else_=null(),
        )
        region = case((single, gl.region_code), else_=null())
        city = case((and_(single, gl.city.is_not(None)), gl.city), else_=null())
        stmt = stmt.add_columns(kind.label("kind"), country.label("country"), region.label("region"), city.label("city"))
        stmt = stmt.outerjoin(gl, and_(single, gl.id == Job.location_ids[1]))
    return stmt.where(*conds)


def _grouped(
    db: Session,
    f: StatFilters,
    windows: dict[str, tuple[dt.datetime, dt.datetime]],
    group: list[str],
    *,
    geo: bool,
    active_at: dt.datetime | None = None,
) -> dict[tuple, dict[str, int]]:
    """Counts per group for several time windows in one pass (FILTER aggregates) – or, with ``active_at``,
    the open vacancies per group. Returns ``{group_key: {window: n, 'companies': n, 'sources': n}}``."""
    conds = list(filter_conditions(f))
    aggs: list = []
    if active_at is not None:
        conds += [Job.status == "open", Job.discovered_at < active_at]
    else:
        conds += [
            Job.discovered_at >= min(s for s, _ in windows.values()),
            Job.discovered_at < max(u for _, u in windows.values()),
        ]
    base = _job_base(conds, geo=geo).subquery("b")
    if active_at is not None:
        aggs.append(func.count().label("active"))
    else:
        for name, (s, u) in windows.items():
            aggs.append(func.count().filter(window_cond(s, u, f, base.c)).label(name))
        first = next(iter(windows))
        s0, u0 = windows[first]
        aggs.append(func.count(distinct(base.c.company_id)).filter(window_cond(s0, u0, f, base.c)).label("companies"))
        aggs.append(func.count(distinct(base.c.source_id)).filter(window_cond(s0, u0, f, base.c)).label("sources"))
    gcols = [base.c[g] for g in group]
    stmt = select(*gcols, *aggs)
    if gcols:
        stmt = stmt.group_by(*gcols)
    out: dict[tuple, dict[str, int]] = {}
    for row in db.execute(stmt):
        m = row._mapping
        out[tuple(m[g] for g in group)] = {k: int(m[k] or 0) for k in m.keys() if k not in group}
    return out


def _windows(period: Period, snapshot: dt.datetime) -> dict[str, tuple[dt.datetime, dt.datetime]]:
    """The windows every table needs: the selected period first (it drives companies/sources), its previous
    period, and the fixed today / 7 d / 30 d columns."""
    w = {"sel": (period.start, period.upto), "prev": (period.prev_start, period.prev_end)}
    for k, p in _kpi_periods(period.tz, snapshot).items():
        if k in ("today", "7d", "30d"):
            w[k] = (p.start, p.upto)
    return w


# --- summary ---------------------------------------------------------------------------------------------


def summary(db: Session, period: Period, f: StatFilters, snapshot: dt.datetime) -> dict[str, Any]:
    kpis_p = _kpi_periods(period.tz, snapshot)
    wins: dict[str, tuple[dt.datetime, dt.datetime]] = {"selected": (period.start, period.upto), "selected_prev": (period.prev_start, period.prev_end)}
    for k, p in kpis_p.items():
        wins[k] = (p.start, p.upto)
        wins[f"{k}_prev"] = (p.prev_start, p.prev_end)
    cols = [func.count(Job.id).filter(window_cond(s, u, f)).label(n) for n, (s, u) in wins.items()]
    for k in ("selected", *KPI_KEYS):
        s, u = wins[k]
        cols.append(func.count(distinct(Job.company_id)).filter(window_cond(s, u, f)).label(f"companies_{k}"))
    earliest = min(s for s, _ in wins.values())
    counts = db.execute(
        select(*cols).where(*filter_conditions(f), Job.discovered_at >= earliest, Job.discovered_at < snapshot)
    ).one()._mapping

    active = count_active(db, f, snapshot)

    ev_rows = db.execute(
        select(JobEvent.event_type, func.count(distinct(JobEvent.job_id)))
        .join(Job, Job.id == JobEvent.job_id)
        .where(
            JobEvent.occurred_at >= period.start,
            JobEvent.occurred_at < period.upto,
            JobEvent.event_type.in_(("job.updated", "job.closed", "job.reopened")),
            *filter_conditions(f),
        )
        .group_by(JobEvent.event_type)
    ).all()
    ev = {r[0]: int(r[1]) for r in ev_rows}

    created_ev = exists(
        select(literal(1)).where(JobEvent.job_id == Job.id, JobEvent.event_type == "job.created").correlate(Job)
    )
    sel_cond = window_cond(period.start, period.upto, f)
    dq = db.execute(
        select(
            func.count(Job.id),
            func.count(Job.id).filter(created_ev),
            func.count(Job.id).filter(~has_earlier_jobs(period.start)),
            func.count(Job.id).filter(Job.date_posted.is_not(None)),
            func.count(Job.id).filter(Job.specialization_id.is_not(None)),
            func.count(Job.id).filter(func.cardinality(Job.location_ids) > 0),
        ).where(*filter_conditions(f), sel_cond)
    ).one()
    total, with_event, initial, with_date, classified, located = (int(x) for x in dq)
    events_since = db.execute(
        select(func.min(JobEvent.occurred_at)).where(JobEvent.event_type == "job.created")
    ).scalar_one()

    def pc(n: int) -> float | None:
        return round(n / total * 100, 1) if total else None

    warnings: list[dict[str, str]] = []
    if total and initial / total >= 0.5:
        warnings.append(
            {
                "code": "initial_load",
                "message": f"{initial} of {total} newly discovered vacancies belong to companies whose first vacancy was "
                "found in this period (initial catalogue load). Treat the number as growth of the database, not of the market.",
            }
        )
    if total and with_event < total:
        warnings.append(
            {
                "code": "events_incomplete",
                "message": f"{total - with_event} of {total} newly discovered vacancies have no job.created event "
                "(imported or created before the event log). Updated/closed/reopened counts can be incomplete.",
            }
        )
    if total and classified / total < 0.6:
        warnings.append({"code": "low_classification", "message": f"Only {pc(classified)} % have a specialization."})

    kpis = []
    for k in KPI_KEYS:
        p = kpis_p[k]
        cur, prev = int(counts[k]), int(counts[f"{k}_prev"])
        kpis.append(
            {
                "key": k,
                "new": cur,
                "previous": prev,
                "change_pct": _pct(cur, prev),
                "companies": int(counts[f"companies_{k}"]),
                "period": p.to_dict(),
            }
        )
    cur, prev = int(counts["selected"]), int(counts["selected_prev"])
    return {
        "snapshot_at": snapshot.isoformat(),
        "period": period.to_dict(),
        "filters": f.as_dict(),
        "new": {
            "total": cur,
            "previous": prev,
            "change_pct": _pct(cur, prev),
            "companies": int(counts["companies_selected"]),
        },
        "active": active,
        "updated": ev.get("job.updated", 0),
        "closed": ev.get("job.closed", 0),
        "reopened": ev.get("job.reopened", 0),
        "kpis": kpis,
        "data_quality": {
            "new_total": total,
            "with_created_event": with_event,
            "created_event_pct": pc(with_event),
            "initial_load": initial,
            "initial_load_pct": pc(initial),
            "with_source_date": with_date,
            "with_source_date_pct": pc(with_date),
            "classified": classified,
            "classified_pct": pc(classified),
            "with_location": located,
            "with_location_pct": pc(located),
            "events_since": events_since.isoformat() if events_since else None,
            "warnings": warnings,
        },
    }


# --- trends -----------------------------------------------------------------------------------------------


def trends(db: Session, period: Period, f: StatFilters) -> dict[str, Any]:
    """New vacancies per local day (hourly when the period is at most 48 h long)."""
    hourly = (period.upto - period.start) <= dt.timedelta(hours=48)
    unit = "hour" if hourly else "day"
    tz_sql = period.tz.replace("'", "''")  # a validated IANA name; escaped anyway
    bucket = func.date_trunc(literal_column(f"'{unit}'"), Job.discovered_at, literal_column(f"'{tz_sql}'"))
    rows = db.execute(
        select(bucket.label("b"), func.count(Job.id))
        .where(*filter_conditions(f), window_cond(period.start, period.upto, f))
        .group_by(bucket)
    ).all()
    got = {r[0].astimezone(UTC): int(r[1]) for r in rows}

    tz = period.zone
    points: list[dict[str, Any]] = []
    if hourly:
        t = period.start.astimezone(tz).replace(minute=0, second=0, microsecond=0).astimezone(UTC)
        while t < period.upto:
            points.append({"t": t.isoformat(), "label": t.astimezone(tz).strftime("%H:%M"), "count": got.get(t, 0)})
            t += dt.timedelta(hours=1)
    else:
        d = period.start.astimezone(tz).date()
        while True:
            t = _midnight(d, tz)
            if t >= period.upto:
                break
            points.append({"t": t.isoformat(), "label": d.isoformat(), "count": got.get(t, 0)})
            d += dt.timedelta(days=1)
    return {
        "period": period.to_dict(),
        "bucket": unit,
        "points": points,
        "total": sum(p["count"] for p in points),
        "empty": not any(p["count"] for p in points),
    }


# --- specializations ----------------------------------------------------------------------------------------

SPEC_SORTS = ("sel", "prev", "change", "today", "7d", "30d", "active", "name")


def _sort_rows(rows: list[dict], sort: str, order: str, name_key: str = "name") -> list[dict]:
    keymap = {"change": "change_pct", "name": name_key}
    key = keymap.get(sort, sort)
    rev = order != "asc"
    if sort == "name":
        return sorted(rows, key=lambda r: (str(r[key]).lower(), r.get("key", "")), reverse=rev)
    present = [r for r in rows if r.get(key) is not None]
    missing = [r for r in rows if r.get(key) is None]
    present.sort(key=lambda r: (r[key], r["sel"], str(r.get("key", ""))), reverse=rev)
    return present + missing


def specializations(
    db: Session,
    period: Period,
    f: StatFilters,
    snapshot: dt.datetime,
    *,
    level: str = "specialization",
    sort: str = "sel",
    order: str = "desc",
    limit: int | None = 10,
) -> dict[str, Any]:
    """Primary specialization per vacancy (additive; sums to the total). ``overlap`` counts vacancies that list
    the specialization as primary *or secondary* and is not additive."""
    if level not in ("specialization", "group"):
        raise StatsError("level must be 'specialization' or 'group'")
    if sort not in SPEC_SORTS:
        raise StatsError(f"sort must be one of {', '.join(SPEC_SORTS)}")
    idx = _spec_index(db)

    def gid(sid: int | None) -> int | None:
        if sid is None or sid not in idx:
            return None
        if level == "specialization":
            return sid
        return idx[sid]["parent_id"] or sid

    wins = _windows(period, snapshot)
    cur = _grouped(db, f, wins, ["spec"], geo=False)
    act = _grouped(db, f, {}, ["spec"], geo=False, active_at=snapshot)

    merged: dict[int | None, dict[str, int]] = {}
    for src in (cur, act):
        for (sid,), vals in src.items():
            tgt = merged.setdefault(gid(sid), {})
            for k, v in vals.items():
                if k in ("companies", "sources"):  # distinct counts cannot be summed across merged rows: recomputed below
                    tgt[k] = max(tgt.get(k, 0), v)
                else:
                    tgt[k] = tgt.get(k, 0) + v

    if level == "group":  # distinct companies / sources of a group need a real distinct count
        cmp = _distinct_by_group(db, period, f, idx)
        for g, vals in cmp.items():
            merged.setdefault(g, {}).update(vals)

    # secondary (overlapping) counts
    S = aliased(Specialization)
    gexpr = S.id if level == "specialization" else func.coalesce(S.parent_id, S.id)
    ov_rows = db.execute(
        select(gexpr, func.count(distinct(Job.id)))
        .select_from(Job)
        .join(JobSpecialization, JobSpecialization.job_id == Job.id)
        .join(S, S.id == JobSpecialization.specialization_id)
        .where(*filter_conditions(f), window_cond(period.start, period.upto, f))
        .group_by(gexpr)
    ).all()
    overlap = {int(r[0]): int(r[1]) for r in ov_rows}

    rows: list[dict[str, Any]] = []
    for sid, v in merged.items():
        meta = idx.get(sid) if sid is not None else None
        sel, prev = v.get("sel", 0), v.get("prev", 0)
        if not any(v.get(k, 0) for k in ("sel", "prev", "today", "7d", "30d", "active")):
            continue
        rows.append(
            {
                "key": meta["slug"] if meta else UNCLASSIFIED,
                "id": sid,
                "slug": meta["slug"] if meta else UNCLASSIFIED,
                "name": meta["name_en"] if meta else "Unclassified",
                "name_de": meta["name_de"] if meta else "Nicht klassifiziert",
                "group": (idx[meta["parent_id"]]["name_en"] if meta and meta["parent_id"] else (meta["name_en"] if meta else None)),
                "is_it": bool(meta and meta["is_it"]),
                "is_group": bool(meta and meta["parent_id"] is None),
                "sel": sel,
                "prev": prev,
                "change_pct": _pct(sel, prev),
                "today": v.get("today", 0),
                "7d": v.get("7d", 0),
                "30d": v.get("30d", 0),
                "active": v.get("active", 0),
                "companies": v.get("companies", 0),
                "sources": v.get("sources", 0),
                "overlap": overlap.get(sid, 0) if sid is not None else None,
            }
        )
    rows = _sort_rows(rows, sort, order)
    totals = {k: sum(r[k] for r in rows) for k in ("sel", "prev", "today", "7d", "30d", "active")}
    totals["change_pct"] = _pct(totals["sel"], totals["prev"])
    shown = rows if not limit else rows[:limit]
    rest = None
    if limit and len(rows) > limit:
        tail = rows[limit:]
        rest = {k: sum(r[k] for r in tail) for k in ("sel", "prev", "today", "7d", "30d", "active")}
        rest["rows"] = len(tail)
    classified = sum(r["sel"] for r in rows if r["id"] is not None)
    return {
        "period": period.to_dict(),
        "level": level,
        "rows": shown,
        "rest": rest,
        "totals": totals,
        "total_rows": len(rows),
        "completeness": {
            "new_total": totals["sel"],
            "classified": classified,
            "classified_pct": round(classified / totals["sel"] * 100, 1) if totals["sel"] else None,
        },
        "notes": [
            "Counts use each vacancy's primary specialization, so the rows add up to the total.",
            "'Overlap' counts vacancies that also list the specialization as secondary; it is not additive.",
        ],
    }


def _distinct_by_group(db: Session, period: Period, f: StatFilters, idx: dict) -> dict[int | None, dict[str, int]]:
    S = aliased(Specialization)
    gexpr = func.coalesce(S.parent_id, S.id)
    rows = db.execute(
        select(gexpr, func.count(distinct(Job.company_id)), func.count(distinct(Job.career_source_id)))
        .select_from(Job)
        .join(S, S.id == Job.specialization_id, isouter=True)
        .where(*filter_conditions(f), window_cond(period.start, period.upto, f))
        .group_by(gexpr)
    ).all()
    return {(int(r[0]) if r[0] is not None else None): {"companies": int(r[1]), "sources": int(r[2])} for r in rows}


# --- cities -------------------------------------------------------------------------------------------------

CITY_LEVELS = ("city", "region", "country")


def _place_label(kind: str, country: str | None, region: str | None, city: str | None, cn: dict, rn: dict) -> dict[str, Any]:
    if kind == "city":
        label = city or ""
    elif kind == "no_city":
        label = f"{KIND_LABEL[kind]} ({rn.get(region or '', region) or cn.get(country or '', country) or '?'})"
    else:
        label = KIND_LABEL[kind] + (f" ({cn.get(country, country)})" if country and kind in ("multi", "unknown") else "")
    return {
        "key": f"{kind}|{country or ''}|{region or ''}|{(city or '').lower()}",
        "kind": kind,
        "country_code": country,
        "country": cn.get(country or "", None),
        "region_code": region,
        "region": rn.get(region or "", None),
        "city": city,
        "label": label,
        "clickable": kind in ("city", "no_city"),
    }


def cities(
    db: Session,
    period: Period,
    f: StatFilters,
    snapshot: dt.datetime,
    *,
    mode: str = "unique",
    level: str = "city",
    sort: str = "sel",
    order: str = "desc",
    limit: int | None = 20,
) -> dict[str, Any]:
    if mode not in ("unique", "coverage"):
        raise StatsError("mode must be 'unique' or 'coverage'")
    if level not in CITY_LEVELS:
        raise StatsError(f"level must be one of {', '.join(CITY_LEVELS)}")
    if sort not in SPEC_SORTS:
        raise StatsError(f"sort must be one of {', '.join(SPEC_SORTS)}")
    cn, rn = _geo_names(db)
    wins = _windows(period, snapshot)

    group = ["kind", "country", "region", "city"]
    cur = _grouped(db, f, wins, group, geo=True)
    act = _grouped(db, f, {}, group, geo=True, active_at=snapshot)

    def collapse(key: tuple) -> tuple:
        kind, country, region, city = key
        if level == "city" or kind in ("multi", "remote", "unknown"):
            return key
        if level == "region":
            return ("region" if region else kind, country, region, None)
        return ("country" if country else kind, country, None, None)

    merged: dict[tuple, dict[str, int]] = {}
    for src in (cur, act):
        for key, vals in src.items():
            tgt = merged.setdefault(collapse(key), {})
            for k, v in vals.items():
                tgt[k] = tgt.get(k, 0) + v if k not in ("companies", "sources") else max(tgt.get(k, 0), v)

    if mode == "coverage":
        merged = _coverage(db, f, wins, snapshot, level, merged)

    rows: list[dict[str, Any]] = []
    for (kind, country, region, city), v in merged.items():
        if not any(v.get(k, 0) for k in ("sel", "prev", "today", "7d", "30d", "active")):
            continue
        if kind in ("region", "country"):
            ident = {
                "key": f"{kind}|{country or ''}|{region or ''}|",
                "kind": kind,
                "country_code": country,
                "country": cn.get(country or ""),
                "region_code": region,
                "region": rn.get(region or ""),
                "city": None,
                "label": (rn.get(region or "", region) if kind == "region" else cn.get(country or "", country)) or "?",
                "clickable": True,
            }
        else:
            ident = _place_label(kind, country, region, city, cn, rn)
        sel, prev = v.get("sel", 0), v.get("prev", 0)
        rows.append(
            {
                **ident,
                "name": ident["label"],
                "sel": sel,
                "prev": prev,
                "change_pct": _pct(sel, prev),
                "today": v.get("today", 0),
                "7d": v.get("7d", 0),
                "30d": v.get("30d", 0),
                "active": v.get("active", 0),
                "companies": v.get("companies", 0),
            }
        )
    rows = _sort_rows(rows, sort, order)
    sum_keys = ("sel", "prev", "today", "7d", "30d", "active")
    totals = {k: sum(r[k] for r in rows) for k in sum_keys}
    additive = mode == "unique"
    shown = rows if not limit else rows[:limit]
    rest = None
    if limit and len(rows) > limit:
        rest = {k: sum(r[k] for r in rows[limit:]) for k in sum_keys}
        rest["rows"] = len(rows) - limit
    return {
        "period": period.to_dict(),
        "mode": mode,
        "level": level,
        "additive": additive,
        "rows": shown,
        "rest": rest if additive else None,
        "totals": totals if additive else None,
        "total_rows": len(rows),
        "notes": [
            "Unique job breakdown: every vacancy is counted in exactly one row; rows add up to the total."
            if additive
            else "City coverage: a vacancy is counted in every place it lists, so rows overlap and must not be summed.",
            "Same-named cities in different countries/regions are separate rows.",
        ],
    }


def _coverage(db, f, wins, snapshot, level, unique_merged):
    """Overlapping counts per listed place. Remote / unknown vacancies (no place) are taken from the unique pass."""
    jl, gl = JobLocation.__table__, GeoLocation.__table__
    gcols = {"city": [gl.c.country_code, gl.c.region_code, gl.c.city], "region": [gl.c.country_code, gl.c.region_code], "country": [gl.c.country_code]}[level]
    aggs = []
    for name, (s, u) in wins.items():
        aggs.append(func.count(distinct(Job.id)).filter(window_cond(s, u, f)).label(name))
    aggs.append(func.count(distinct(Job.company_id)).filter(window_cond(*wins["sel"], f)).label("companies"))
    base = (
        select(*gcols, *aggs)
        .select_from(Job)
        .join(jl, jl.c.job_id == Job.id)
        .join(gl, gl.c.id == jl.c.location_id)
        .where(*filter_conditions(f), Job.discovered_at >= min(s for s, _ in wins.values()), Job.discovered_at < max(u for _, u in wins.values()))
        .group_by(*gcols)
    )
    act = (
        select(*gcols, func.count(distinct(Job.id)).label("active"))
        .select_from(Job)
        .join(jl, jl.c.job_id == Job.id)
        .join(gl, gl.c.id == jl.c.location_id)
        .where(*filter_conditions(f), Job.status == "open", Job.discovered_at < snapshot)
        .group_by(*gcols)
    )
    out: dict[tuple, dict[str, int]] = {}

    def key_of(m) -> tuple:
        if level == "city":
            return (("city" if m["city"] else "no_city"), m["country_code"], m["region_code"], m["city"])
        if level == "region":
            return ("region", m["country_code"], m["region_code"], None)
        return ("country", m["country_code"], None, None)

    for stmt in (base, act):
        for row in db.execute(stmt):
            m = row._mapping
            tgt = out.setdefault(key_of(m), {})
            for k in m.keys():
                if k in ("country_code", "region_code", "city"):
                    continue
                tgt[k] = int(m[k] or 0)
    for key, vals in unique_merged.items():
        if key[0] in ("remote", "unknown"):
            out[key] = vals
    return out


# --- countries ----------------------------------------------------------------------------------------------


def countries(db: Session, period: Period, f: StatFilters, snapshot: dt.datetime) -> dict[str, Any]:
    """Per country of the vacancy (``jobs.country_codes``): one country, several (MULTI) or none (UNKNOWN).
    Additive: rows add up to the total."""
    cn, _ = _geo_names(db)
    key = case(
        (func.cardinality(Job.country_codes) == 1, Job.country_codes[1]),
        (func.cardinality(Job.country_codes) > 1, "MULTI"),
        else_="UNKNOWN",
    )
    wins = _windows(period, snapshot)
    cols = [func.count(Job.id).filter(window_cond(s, u, f)).label(n) for n, (s, u) in wins.items()]
    stmt = (
        select(key.label("c"), *cols)
        .where(*filter_conditions(f), Job.discovered_at >= min(s for s, _ in wins.values()), Job.discovered_at < max(u for _, u in wins.values()))
        .group_by(key)
    )
    act = dict(
        db.execute(
            select(key.label("c"), func.count(Job.id))
            .where(*filter_conditions(f), Job.status == "open", Job.discovered_at < snapshot)
            .group_by(key)
        ).all()
    )
    names = {"MULTI": "Several countries", "UNKNOWN": "Unknown country"}
    rows = []
    seen = set()
    for row in db.execute(stmt):
        m = row._mapping
        code = m["c"]
        seen.add(code)
        rows.append(_country_row(code, m, act.get(code, 0), cn, names))
    for code, a in act.items():
        if code not in seen:
            rows.append(_country_row(code, {}, a, cn, names))
    for c in DACH:  # the three DACH countries are always listed, even with zero
        if c not in {r["code"] for r in rows} and (not f.countries or c in f.countries):
            rows.append(_country_row(c, {}, 0, cn, names))
    rows = [r for r in rows if r["code"] in DACH or any(r[k] for k in ("sel", "prev", "today", "7d", "30d", "active"))]
    rows.sort(key=lambda r: (r["code"] not in DACH, -r["sel"], r["code"]))
    return {
        "period": period.to_dict(),
        "rows": rows,
        "totals": {k: sum(r[k] for r in rows) for k in ("sel", "prev", "today", "7d", "30d", "active")},
        "empty": not any(r["sel"] for r in rows),
    }


def _country_row(code: str, m, active: int, cn: dict, names: dict) -> dict[str, Any]:
    sel, prev = int(m.get("sel", 0) or 0), int(m.get("prev", 0) or 0)
    return {
        "code": code,
        "name": names.get(code) or cn.get(code, code),
        "sel": sel,
        "prev": prev,
        "change_pct": _pct(sel, prev),
        "today": int(m.get("today", 0) or 0),
        "7d": int(m.get("7d", 0) or 0),
        "30d": int(m.get("30d", 0) or 0),
        "active": int(active or 0),
    }


# --- specialization x city ---------------------------------------------------------------------------------------


def specialization_city(
    db: Session,
    period: Period,
    f: StatFilters,
    snapshot: dt.datetime,
    *,
    level: str = "group",
    rows: int = 8,
    cols: int = 8,
    combos: int = 20,
    sort: str = "total",
    order: str = "desc",
) -> dict[str, Any]:
    """Matrix of the selected period: primary specialization x unique place. Every vacancy sits in exactly one cell,
    so cells add up to the total (cells outside the shown rows/columns are summed in ``other_*``)."""
    if level not in ("specialization", "group"):
        raise StatsError("level must be 'specialization' or 'group'")
    if sort not in ("total", "name"):
        raise StatsError("sort must be 'total' or 'name'")
    idx = _spec_index(db)
    cn, rn = _geo_names(db)
    raw = _grouped(db, f, {"sel": (period.start, period.upto)}, ["spec", "kind", "country", "region", "city"], geo=True)

    def gid(sid):
        if sid is None or sid not in idx:
            return None
        return sid if level == "specialization" else (idx[sid]["parent_id"] or sid)

    cell: dict[tuple, int] = {}
    for (sid, kind, country, region, city), v in raw.items():
        k = (gid(sid), _place_label(kind, country, region, city, cn, rn)["key"])
        cell[k] = cell.get(k, 0) + v.get("sel", 0)

    place_meta: dict[str, dict] = {}
    for (sid, kind, country, region, city) in raw:
        p = _place_label(kind, country, region, city, cn, rn)
        place_meta[p["key"]] = p

    def spec_meta(sid):
        m = idx.get(sid) if sid is not None else None
        return {"id": sid, "slug": m["slug"] if m else UNCLASSIFIED, "name": m["name_en"] if m else "Unclassified"}

    row_tot: dict[Any, int] = {}
    col_tot: dict[str, int] = {}
    for (sid, pk), n in cell.items():
        row_tot[sid] = row_tot.get(sid, 0) + n
        col_tot[pk] = col_tot.get(pk, 0) + n
    total = sum(row_tot.values())

    def order_keys(tot: dict, name_of) -> list:
        ks = list(tot)
        if sort == "name":
            return sorted(ks, key=lambda k: name_of(k).lower(), reverse=(order == "desc"))
        return sorted(ks, key=lambda k: (-tot[k] if order == "desc" else tot[k], str(k)))

    top_rows = order_keys(row_tot, lambda k: spec_meta(k)["name"])[:rows]
    # columns: real places first; the explicit buckets (multi / remote / unknown) stay out of the column ranking
    place_keys = {k: v for k, v in col_tot.items() if place_meta[k]["kind"] in ("city", "no_city")}
    top_cols = order_keys(place_keys, lambda k: place_meta[k]["label"])[:cols]
    matrix = [[cell.get((r, c), 0) for c in top_cols] for r in top_rows]
    shown = sum(sum(x) for x in matrix)
    pairs = sorted(cell.items(), key=lambda kv: (-kv[1], str(kv[0])))
    top = [
        {"specialization": spec_meta(s), "place": place_meta[p], "count": n}
        for (s, p), n in pairs[:combos]
        if n > 0
    ]
    return {
        "period": period.to_dict(),
        "level": level,
        "total": total,
        "rows": [{**spec_meta(r), "total": row_tot[r]} for r in top_rows],
        "columns": [{**place_meta[c], "total": col_tot[c]} for c in top_cols],
        "cells": matrix,
        "other": {
            "outside_shown": total - shown,
            "in_buckets": sum(n for k, n in col_tot.items() if place_meta[k]["kind"] not in ("city", "no_city")),
        },
        "top_combinations": top,
        "empty": total == 0,
        "notes": ["Each vacancy is in one cell (primary specialization x its single place); multi-location, remote and unknown are separate buckets."],
    }


# --- filter options for the UI ------------------------------------------------------------------------------------


def filter_options(db: Session) -> dict[str, Any]:
    idx = _spec_index(db)
    specs = sorted(idx.values(), key=lambda s: (s["parent_id"] or s["id"], s["parent_id"] is not None, s["sort_order"], s["name_en"]))
    countries_ = [
        {"code": c.code, "name": c.name, "in_dach": c.in_dach}
        for c in db.execute(select(GeoCountry).where(GeoCountry.in_dach.is_(True)).order_by(GeoCountry.code)).scalars()
    ]
    regions = [
        {"code": r.code, "country_code": r.country_code, "name": r.name}
        for r in db.execute(
            select(GeoRegion).where(GeoRegion.country_code.in_(DACH)).order_by(GeoRegion.country_code, GeoRegion.name)
        ).scalars()
    ]
    return {"specializations": specs, "countries": countries_, "regions": regions, "periods": list(PERIOD_KEYS)}
