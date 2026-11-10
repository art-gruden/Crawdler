"""Reference data and the backfill of already stored companies/vacancies (migration 0003 + maintenance).

Uses plain SQL only (no ORM models), so a migration written today keeps working after later schema
changes. Everything is idempotent: re-running it does not create duplicates (job_locations,
job_languages, job_specializations have primary keys; geo rows are keyed by ``loc_key``).

No events are emitted: a backfill is not a change of the vacancy itself.
"""

from __future__ import annotations

import re
import sys

from sqlalchemy import create_engine, text

from gcc_core.geo import Place, remote_scope, resolve_location
from gcc_core.geo_data import REGIONS
from gcc_core.geo_db import seed_geo, set_company_location, set_job_locations

from .extract import taxonomy
from .extract.normalize import (
    _CUR,
    NORMALIZATION_VERSION,
    WORK_MODEL,
    _basis,
    currency_code,
    employment_kinds,
    quality_report,
    salary_from_text,
)
from .extract.skills import CEFR_ORDER, find_languages

LEGACY_EMPLOYMENT = {
    "full_time": "FULL_TIME",
    "part_time": "PART_TIME",
    "contract": "CONTRACT",
    "working_student": "WORKING_STUDENT",
    "internship": "INTERNSHIP",
    "apprenticeship": "APPRENTICESHIP",
    "mini_job": "OTHER",
    "thesis": "OTHER",
    "volunteer": "OTHER",
}


def seed_reference(conn) -> dict:
    """Countries, regions, the city gazetteer and the specialization taxonomy."""
    stats = seed_geo(conn)
    rows = [
        {
            "id": s.id,
            "slug": s.slug,
            "en": s.name_en,
            "de": s.name_de,
            "p": s.group,
            "it": s.is_it,
            "o": s.sort,
        }
        for s in taxonomy.SPECIALIZATIONS
    ]
    # groups first (parents), then leaves, so the self-referencing FK is satisfied
    ordered = sorted(rows, key=lambda r: (r["p"] is not None, r["id"]))
    for r in ordered:
        conn.execute(
            text(
                "INSERT INTO specializations (id, slug, name_en, name_de, parent_id, is_it, sort_order) "
                "VALUES (:id, :slug, :en, :de, :p, :it, :o) "
                "ON CONFLICT (id) DO UPDATE SET slug = EXCLUDED.slug, name_en = EXCLUDED.name_en, "
                "name_de = EXCLUDED.name_de, parent_id = EXCLUDED.parent_id, is_it = EXCLUDED.is_it, "
                "sort_order = EXCLUDED.sort_order"
            ),
            r,
        )
    stats["specializations"] = len(rows)
    return stats


def backfill_companies(conn) -> int:
    """country_code from the ISO 3166-2 state code, and the headquarters location."""
    conn.execute(
        text(
            "UPDATE companies SET country_code = left(state_code, 2) "
            "WHERE country_code IS NULL AND state_code ~ '^[A-Z]{2}-'"
        )
    )
    rows = conn.execute(
        text(
            "SELECT id, city, postcode, state_code, country_code FROM companies "
            "WHERE country_code IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM company_locations cl WHERE cl.company_id = companies.id AND cl.kind = 'hq')"
        )
    ).all()
    cache: dict = {}
    n = 0
    for cid, city, postcode, state, country in rows:
        region = state if state in REGIONS else None
        place = None
        if city:
            try:
                res = resolve_location(
                    None, [{"city": city, "postcode": postcode, "region": REGIONS[region][1] if region else None, "country": country}]
                )
                found = res.places
            except Exception:  # a malformed city name falls back to the plain value
                found = []
            place = found[0] if found else Place(country, region, city, postcode, confidence=0.6, source="company")
        elif region:
            place = Place(country, region, None, postcode, confidence=0.8, source="company")
        if place:
            place.postcode = place.postcode or postcode
            set_company_location(conn, cid, place, "hq", cache)
            n += 1
    return n


def _spec_rows(job_id: int, cls: taxonomy.Classification) -> list[dict]:
    rows = []
    if cls.primary_id:
        rows.append({"j": job_id, "s": cls.primary_id, "p": True, "c": cls.confidence})
        rows += [{"j": job_id, "s": i, "p": False, "c": c} for i, c in cls.secondary if i != cls.primary_id]
    return rows


def derive_job_fields(row: dict, skills: list[str]) -> dict:
    """Derive the normalized fields of one stored vacancy from what is already in the database.

    The stored legacy ``country`` is treated as *weak* evidence (confidence 0.5, source ``legacy``)
    because older code assigned DE from a 5-digit postcode. Stronger evidence wins.
    """
    raw_loc = row["location_raw"] or row["city"]
    geo = resolve_location(raw_loc, None)
    places = list(geo.places)
    if not places and row["city"] and row["country"]:
        legacy = resolve_location(None, [{"city": row["city"], "postcode": row["postcode"], "country": row["country"]}])
        places = list(legacy.places)
        for p in places:
            if p.source != "gazetteer":
                p.confidence, p.source = 0.5, "legacy"
        if places:
            geo.status = "resolved" if not geo.unresolved else "partial"
            geo.unresolved = []
    work_model = WORK_MODEL.get(row["remote_type"], "UNKNOWN")
    text_ = row["description"] or ""
    scope = remote_scope([row["location_raw"], row["title"], text_[:6000]], work_model, places)
    sv = row["source_values"] or {}
    emp_raw = sv.get("employmentType")
    emp_raw = [emp_raw] if isinstance(emp_raw, str) else [str(x) for x in (emp_raw or [])]
    kinds = employment_kinds(emp_raw, row["title"], text_)
    if kinds == ["UNKNOWN"]:
        mapped = [LEGACY_EMPLOYMENT[k] for k in (row["employment_types"] or []) if k in LEGACY_EMPLOYMENT]
        kinds = list(dict.fromkeys(mapped)) or ["UNKNOWN"]
    langs = find_languages(text_, list(row["requirements"] or []))
    cls = taxonomy.classify(row["title"], text_, skills)
    salary_raw = row["salary_raw"] or ""
    currency = row["salary_currency"]
    has_salary = row["salary_min"] is not None or row["salary_max"] is not None
    new_salary = None
    if has_salary:
        # older code defaulted to EUR; the stored wording is the only evidence, otherwise it is unknown
        m = re.search(_CUR, salary_raw, re.I)
        found = currency_code(m.group(0)) if m else None
        if found:
            currency = found
        elif currency == "EUR" and not re.search(r"currency", salary_raw, re.I):
            currency = None
    elif text_ and (new_salary := salary_from_text(text_)):  # e.g. CHF amounts the old parser could not read
        has_salary, currency, salary_raw = True, new_salary["currency"], new_salary["raw"]
    sal_conf = None
    if has_salary:
        sal_conf = new_salary["confidence"] if new_salary else (0.9 if currency and salary_raw.lstrip().startswith("{") else 0.5)
    score, quality = quality_report(
        description=text_,
        geo=geo if places else None,
        employment=kinds,
        work_model=work_model,
        has_salary=has_salary,
        languages=langs,
        spec_conf=cls.confidence if cls.primary_id else 0.0,
        date_posted=row["date_posted"],
        application_url=row["application_url"],
        sections=bool(row["requirements"] or row["responsibilities"]),
        extraction_confidence=row["confidence"] or 0.0,
        salary_confidence=sal_conf,
    )
    return {
        "places": places,
        "geo_status": geo.status,
        "geo_detail": geo.detail(),
        "work_model": work_model,
        "remote_scope": scope.scope,
        "remote_countries": scope.countries,
        "kinds": kinds,
        "languages": langs,
        "cls": cls,
        "currency": currency,
        "new_salary": new_salary,
        "salary_basis": (new_salary["basis"] if new_salary else _basis(salary_raw, 0, len(salary_raw)))
        if has_salary
        else "unknown",
        "salary_confidence": sal_conf,
        "quality_score": score,
        "quality": quality,
    }


def backfill_jobs(conn, *, batch: int = 500, below_version: int = NORMALIZATION_VERSION, limit: int | None = None) -> int:
    """Derive normalized fields for vacancies stored by older code. Returns the number processed."""
    import json

    done = 0
    last_id = 0
    cache: dict = {}
    while True:
        rows = (
            conn.execute(
                text(
                    "SELECT id, title, location_raw, city, postcode, country, remote_type, employment_types, description, "
                    "requirements, responsibilities, source_values, salary_raw, salary_currency, salary_min, salary_max, "
                    "date_posted, application_url, confidence, last_seen_at "
                    "FROM jobs WHERE id > :last AND norm_version < :v ORDER BY id LIMIT :n"
                ),
                {"last": last_id, "v": below_version, "n": batch},
            )
            .mappings()
            .all()
        )
        if not rows:
            break
        ids = [r["id"] for r in rows]
        skill_map: dict[int, list[str]] = {i: [] for i in ids}
        for jid, skill in conn.execute(
            text("SELECT job_id, skill FROM job_skills WHERE job_id = ANY(:ids)"), {"ids": ids}
        ):
            skill_map[jid].append(skill)
        for r in rows:
            last_id = r["id"]
            try:
                d = derive_job_fields(dict(r), skill_map[r["id"]])
            except Exception as exc:  # one odd row must never abort the whole migration
                print(f"backfill: vacancy {r['id']} skipped ({type(exc).__name__}); it is re-derived on its next scan", file=sys.stderr)
                conn.execute(
                    text("UPDATE jobs SET norm_version = :v, geo_status = 'unknown' WHERE id = :i"),
                    {"v": NORMALIZATION_VERSION, "i": r["id"]},
                )
                done += 1
                continue
            loc_ids, countries, regions = set_job_locations(conn, r["id"], d["places"], cache)
            conn.execute(text("DELETE FROM job_languages WHERE job_id = :j"), {"j": r["id"]})
            lang_rows = [
                {
                    "j": r["id"],
                    "l": x["language"],
                    "lv": x["level"],
                    "rk": CEFR_ORDER.get(x["level"] or ""),
                    "rq": x["requirement"],
                    "d": x["descriptor"],
                    "e": x["evidence"],
                    "c": x["confidence"],
                }
                for x in d["languages"]
            ]
            if lang_rows:
                conn.execute(
                    text(
                        "INSERT INTO job_languages (job_id, language, level, level_rank, requirement, descriptor, evidence, confidence) "
                        "VALUES (:j, :l, :lv, :rk, :rq, :d, :e, :c) ON CONFLICT DO NOTHING"
                    ),
                    lang_rows,
                )
            conn.execute(text("DELETE FROM job_specializations WHERE job_id = :j"), {"j": r["id"]})
            spec_rows = _spec_rows(r["id"], d["cls"])
            if spec_rows:
                conn.execute(
                    text(
                        "INSERT INTO job_specializations (job_id, specialization_id, is_primary, confidence) "
                        "VALUES (:j, :s, :p, :c) ON CONFLICT DO NOTHING"
                    ),
                    spec_rows,
                )
            conn.execute(
                text(
                    "UPDATE jobs SET geo_status = :gs, geo_detail = CAST(:gd AS jsonb), country_codes = :cc, "
                    "region_codes = :rc, location_ids = :li, work_model = :wm, remote_scope = :rs, "
                    "remote_countries = :rcn, employment_kinds = :ek, languages = CAST(:lg AS jsonb), "
                    "specialization_id = :sid, specialization_confidence = :sc, seniority = :sen, "
                    "salary_currency = :cur, salary_basis = :sb, salary_confidence = :sconf, "
                    "salary_min = COALESCE(salary_min, :smin), salary_max = COALESCE(salary_max, :smax), "
                    "salary_period = COALESCE(salary_period, :sper), salary_raw = COALESCE(salary_raw, :sraw), "
                    "quality_score = :qs, quality = CAST(:q AS jsonb), "
                    "last_checked_at = COALESCE(last_checked_at, last_seen_at), norm_version = :v "
                    "WHERE id = :id"
                ),
                {
                    "gs": d["geo_status"],
                    "gd": json.dumps(d["geo_detail"]),
                    "cc": countries,
                    "rc": regions,
                    "li": loc_ids,
                    "wm": d["work_model"],
                    "rs": d["remote_scope"],
                    "rcn": d["remote_countries"],
                    "ek": d["kinds"],
                    "lg": json.dumps(d["languages"], ensure_ascii=False),
                    "sid": d["cls"].primary_id,
                    "sc": d["cls"].confidence if d["cls"].primary_id else None,
                    "sen": d["cls"].seniority,
                    "cur": d["currency"],
                    "smin": (d["new_salary"] or {}).get("min"),
                    "smax": (d["new_salary"] or {}).get("max"),
                    "sper": (d["new_salary"] or {}).get("period"),
                    "sraw": (d["new_salary"] or {}).get("raw"),
                    "sb": d["salary_basis"],
                    "sconf": d["salary_confidence"],
                    "qs": d["quality_score"],
                    "q": json.dumps(d["quality"], ensure_ascii=False),
                    "v": NORMALIZATION_VERSION,
                    "id": r["id"],
                },
            )
            done += 1
            if limit and done >= limit:
                return done
    return done


def run_all(conn) -> dict:
    stats = seed_reference(conn)
    stats["companies_hq"] = backfill_companies(conn)
    stats["jobs"] = backfill_jobs(conn)
    return stats


def main() -> None:  # pragma: no cover - maintenance CLI
    """``python -m career_crawler.backfill`` re-derives vacancies stored by older normalization."""
    from gcc_core.config import get_settings

    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        print(run_all(conn))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
