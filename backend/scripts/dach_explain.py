"""Synthetic-data benchmark + EXPLAIN plans for the bot search (spec §9).

Usage (on a scratch database, never production):
    DATABASE_URL=postgresql+psycopg://gcc:gcc@localhost:5432/gcc_scratch \
    python scripts/dach_explain.py --jobs 100000

Loads N synthetic jobs (DACH locations, skills, languages, salaries) with plain SQL, runs ANALYZE,
then prints the plan summary and timing for representative queries.
"""

from __future__ import annotations

import argparse
import random
import time

from sqlalchemy import event, text

from career_crawler import search as srch
from career_crawler.extract import taxonomy
from career_crawler.extract.skills import CEFR_ORDER
from gcc_core.db import SessionLocal, get_engine

CITIES = [("DE", "DE-SN", "Dresden"), ("DE", "DE-BE", "Berlin"), ("DE", "DE-BY", "München"), ("AT", "AT-9", "Wien"),
          ("AT", "AT-6", "Graz"), ("CH", "CH-ZH", "Zürich"), ("CH", "CH-GE", "Genève"), ("DE", "DE-HH", "Hamburg")]
SKILLS = ["Python", "Java", "Kubernetes", "Docker", "React", "SQL", "Go", "TypeScript", "AWS", "Linux"]
LANGS = [("de", "B2"), ("de", "C1"), ("en", "B2"), ("en", "C1"), ("fr", "B1")]


def load(n: int) -> None:
    rnd = random.Random(7)
    leaves = [x.id for x in taxonomy.LEAVES]
    with get_engine().begin() as c:
        c.execute(text("TRUNCATE jobs, companies, job_skills, job_locations, job_languages, job_specializations, job_events RESTART IDENTITY CASCADE"))
        c.execute(text("INSERT INTO companies (name, name_key, website_url, domain, verification_status) SELECT 'Co '||g, 'co'||g, 'https://co'||g||'.de/', 'co'||g||'.de', 'verified' FROM generate_series(1, 500) g"))
        locs = {}
        for cc, rc, city in CITIES:
            lid = c.execute(text("INSERT INTO geo_locations (loc_key, country_code, region_code, city, search_name) VALUES (:k,:c,:r,:n,lower(:n)) ON CONFLICT (loc_key) DO UPDATE SET city=EXCLUDED.city RETURNING id"),
                            {"k": f"{cc}|{rc}|{city.lower()}", "c": cc, "r": rc, "n": city}).scalar()
            locs[(cc, rc, city)] = lid
        rows = []
        for i in range(1, n + 1):
            cc, rc, city = rnd.choice(CITIES)
            title = f"{rnd.choice(['Senior', 'Junior', '', 'Lead'])} {rnd.choice(['Python', 'Java', 'DevOps', 'Data', 'Frontend'])} Engineer {i}".strip()
            rows.append({
                "c": rnd.randint(1, 500), "dk": f"k{i}", "t": title, "tn": title.lower(), "u": f"https://co.de/j/{i}", "city": city, "cc": [cc], "rc": [rc],
                "l": [locs[(cc, rc, city)]], "wm": rnd.choice(["REMOTE", "HYBRID", "ONSITE", "UNKNOWN"]), "ek": [rnd.choice(["FULL_TIME", "PART_TIME", "UNKNOWN"])],
                "sp": rnd.choice(leaves), "sen": rnd.choice(["JUNIOR", "MID", "SENIOR", "UNKNOWN"]),
                "smin": (smin := rnd.choice([None, 50000.0, 70000.0, 100000.0])), "per": None if smin is None else "year", "cur": "CHF" if cc == "CH" else "EUR",
                "st": "open" if rnd.random() < 0.9 else "closed", "d": f"engineer {title} {rnd.choice(SKILLS)} {rnd.choice(SKILLS)} beschreibung",
            })
        c.execute(text(
            "INSERT INTO jobs (company_id, dedup_key, title, title_norm, source_url, extraction_method, description, city, country_codes, region_codes, location_ids, "
            "work_model, employment_kinds, specialization_id, seniority, salary_min, salary_max, salary_currency, salary_period, status, confidence, "
            "discovered_at, last_seen_at, norm_version, geo_status) "
            "VALUES (:c,:dk,:t,:tn,:u,'html',:d,:city,:cc,:rc,:l,:wm,:ek,:sp,:sen,:smin,:smin,:cur,:per,:st,0.9,"
            "now() - random() * interval '90 days', now(), 2, 'resolved')"), rows)
        c.execute(text("INSERT INTO job_skills (job_id, skill, category) SELECT j.id, s, 'x' FROM jobs j, LATERAL (SELECT unnest((ARRAY['Python','Java','Kubernetes','Docker','React','SQL','Go','TypeScript','AWS','Linux'])[1 + (j.id % 5): 3 + (j.id % 5)]) s) q ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO job_languages (job_id, language, level, level_rank, requirement, confidence) SELECT id, CASE WHEN id % 3 = 0 THEN 'de' ELSE 'en' END, CASE WHEN id % 2 = 0 THEN 'B2' ELSE 'C1' END, CASE WHEN id % 2 = 0 THEN :b2 ELSE :c1 END, 'required', 0.9 FROM jobs"), {"b2": CEFR_ORDER["B2"], "c1": CEFR_ORDER["C1"]})
        c.execute(text("INSERT INTO job_locations (job_id, location_id, is_primary, confidence, source) SELECT id, location_ids[1], true, 0.9, 'structured' FROM jobs"))
        c.execute(text("INSERT INTO job_specializations (job_id, specialization_id, is_primary, confidence) SELECT id, specialization_id, true, 0.8 FROM jobs WHERE specialization_id IS NOT NULL"))
        c.execute(text("ANALYZE"))


QUERIES = {
    "newest, country DE + region": dict(countries=["DE"], regions=["DE-SN"]),
    "newest, CH + specialization group IT": dict(countries=["CH"], specializations=taxonomy.expand(["information-technology"])),
    "text 'python' + remote/hybrid": dict(q="python", work_models=["REMOTE", "HYBRID"]),
    "skills ALL (Python, Kubernetes) + city": dict(skills=["Python", "Kubernetes"], skills_mode="all", countries=["AT"]),
    "language fit de:C1,en:B2 strict": dict(languages={"de": "C1", "en": "B2"}, lang_mode="strict", countries=["DE"]),
    "salary_min 70k EUR, sort salary_desc": dict(salary_min=70000, currency="EUR", sort="salary_desc"),
    "relevance q+skills+lang": dict(q="engineer", skills=["Python"], languages={"de": "B2"}, sort="relevance"),
    "no filters (newest, open)": dict(),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=100_000)
    ap.add_argument("--skip-load", action="store_true")
    a = ap.parse_args()
    if not a.skip_load:
        t = time.time()
        load(a.jobs)
        print(f"loaded {a.jobs} jobs in {time.time() - t:.1f}s")
    weights = srch.load_weights("")
    s = SessionLocal()
    for name, kw in QUERIES.items():
        p = srch.SearchParams(**kw)
        grabbed: list = []

        def grab(conn, cursor, statement, parameters, context, executemany):
            grabbed.append((statement, parameters))

        event.listen(get_engine(), "before_cursor_execute", grab)
        t = time.time()
        page = srch.run_search(s, p, limit=25, cursor=None, weights=weights)
        ms = (time.time() - t) * 1000
        event.remove(get_engine(), "before_cursor_execute", grab)
        sql, params = grabbed[-1]
        plan = [r[0] for r in s.connection().exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " + sql, params)]
        scans = []
        for ln in plan:
            if "Scan" in ln:
                node = ln.strip().lstrip("-> ").split("  (cost")[0]
                if node not in scans:
                    scans.append(node)
        total = next((ln for ln in plan if ln.startswith("Execution Time")), "")
        print(f"\n== {name}: {len(page.ids)} rows, {ms:.0f} ms wall; {total.strip()}")
        for sc in scans:
            print("   ", sc)
    s.close()


if __name__ == "__main__":
    main()
