"""Database side of the geography: seeding the gazetteer and get-or-create of location rows.

All functions take a SQLAlchemy ``Session`` *or* ``Connection`` (they only use ``execute``), so the
same code runs inside migrations, the crawler and tests. Seeding is idempotent.
"""

from __future__ import annotations

from sqlalchemy import text

from .geo import Place, fold, location_key
from .geo_data import CITIES, COUNTRIES, OTHER_COUNTRIES, REGIONS


def seed_geo(conn) -> dict[str, int]:
    """Insert/refresh countries, regions and the city gazetteer. Safe to run repeatedly."""
    countries = [
        {"c": code, "n": d["name"], "l": d["name_local"], "d": True, "a": list(d["aliases"])}
        for code, d in COUNTRIES.items()
    ] + [
        {"c": code, "n": names[0], "l": names[0], "d": False, "a": list(names)}
        for code, names in OTHER_COUNTRIES.items()
    ]
    conn.execute(
        text(
            "INSERT INTO geo_countries (code, name, name_local, in_dach, aliases) "
            "VALUES (:c, :n, :l, :d, CAST(:a AS text[])) "
            "ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, name_local = EXCLUDED.name_local, "
            "in_dach = EXCLUDED.in_dach, aliases = EXCLUDED.aliases"
        ),
        countries,
    )
    regions = [
        {"c": code, "cc": code.split("-")[0], "n": name, "l": local, "k": kind, "a": list(aliases)}
        for code, (name, local, kind, aliases) in REGIONS.items()
    ]
    conn.execute(
        text(
            "INSERT INTO geo_regions (code, country_code, name, name_local, kind, aliases) "
            "VALUES (:c, :cc, :n, :l, :k, CAST(:a AS text[])) "
            "ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, name_local = EXCLUDED.name_local, "
            "kind = EXCLUDED.kind, aliases = EXCLUDED.aliases"
        ),
        regions,
    )
    cities = [
        {
            "k": location_key(region.split("-")[0], region, city),
            "cc": region.split("-")[0],
            "r": region,
            "city": city,
            "sn": fold(city),
            "a": list(aliases),
        }
        for city, region, aliases in CITIES
    ]
    conn.execute(
        text(
            "INSERT INTO geo_locations (loc_key, country_code, region_code, city, search_name, aliases, source, confidence) "
            "VALUES (:k, :cc, :r, :city, :sn, CAST(:a AS text[]), 'gazetteer', 1) "
            "ON CONFLICT (loc_key) DO UPDATE SET aliases = EXCLUDED.aliases, city = EXCLUDED.city, "
            "source = 'gazetteer', confidence = 1"
        ),
        cities,
    )
    return {"countries": len(countries), "regions": len(regions), "cities": len(cities)}


def ensure_location(conn, place: Place, cache: dict | None = None) -> int:
    """Id of the ``geo_locations`` row for ``place`` (created when missing). One row per place."""
    if not place.country:
        raise ValueError("a location needs a country")
    key = location_key(place.country, place.region, place.city)
    if cache is not None and key in cache:
        return cache[key]
    sn = fold(place.city or "")
    loc_id = conn.execute(text("SELECT id FROM geo_locations WHERE loc_key = :k"), {"k": key}).scalar()
    if loc_id is None and place.city:
        if place.region:
            # a region-less row for the same town (created from weaker evidence) is upgraded, not duplicated
            old = conn.execute(
                text("SELECT id FROM geo_locations WHERE loc_key = :k"),
                {"k": location_key(place.country, None, place.city)},
            ).scalar()
            if old is not None:
                conn.execute(
                    text("UPDATE geo_locations SET loc_key = :k, region_code = :r WHERE id = :id"),
                    {"k": key, "r": place.region, "id": old},
                )
                loc_id = old
        else:
            rows = conn.execute(
                text("SELECT id FROM geo_locations WHERE country_code = :c AND search_name = :sn AND city IS NOT NULL LIMIT 2"),
                {"c": place.country, "sn": sn},
            ).all()
            if len(rows) == 1:
                loc_id = rows[0][0]
    if loc_id is None:
        loc_id = conn.execute(
            text(
                "INSERT INTO geo_locations (loc_key, country_code, region_code, city, search_name, source, confidence) "
                "VALUES (:k, :c, :r, :city, :sn, :src, :conf) "
                "ON CONFLICT (loc_key) DO UPDATE SET loc_key = EXCLUDED.loc_key RETURNING id"
            ),
            {
                "k": key,
                "c": place.country,
                "r": place.region,
                "city": place.city,
                "sn": sn,
                "src": place.source[:24],
                "conf": place.confidence,
            },
        ).scalar_one()
    if cache is not None:
        cache[key] = loc_id
    return loc_id


def set_job_locations(conn, job_id: int, places: list[Place], cache: dict | None = None) -> tuple[list[int], list[str], list[str]]:
    """Replace the work locations of one job. Returns (location ids, country codes, region codes)."""
    conn.execute(text("DELETE FROM job_locations WHERE job_id = :j"), {"j": job_id})
    ids: list[int] = []
    rows = []
    for i, p in enumerate(places):
        loc_id = ensure_location(conn, p, cache)
        if loc_id in ids:
            continue
        ids.append(loc_id)
        rows.append(
            {"j": job_id, "l": loc_id, "p": i == 0, "pc": p.postcode, "c": p.confidence, "s": p.source[:16]}
        )
    if rows:
        conn.execute(
            text(
                "INSERT INTO job_locations (job_id, location_id, is_primary, postcode, confidence, source) "
                "VALUES (:j, :l, :p, :pc, :c, :s) ON CONFLICT DO NOTHING"
            ),
            rows,
        )
    countries = sorted({p.country for p in places if p.country})
    regions = sorted({p.region for p in places if p.region})
    return ids, countries, regions


def set_company_location(conn, company_id: int, place: Place, kind: str = "hq", cache: dict | None = None) -> int:
    loc_id = ensure_location(conn, place, cache)
    conn.execute(
        text(
            "INSERT INTO company_locations (company_id, location_id, kind, source, confidence) "
            "VALUES (:c, :l, :k, :s, :conf) ON CONFLICT DO NOTHING"
        ),
        {"c": company_id, "l": loc_id, "k": kind, "s": place.source[:24], "conf": place.confidence},
    )
    return loc_id
