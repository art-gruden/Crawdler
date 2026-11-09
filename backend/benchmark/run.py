"""Reproducible offline benchmark of the career crawler.

Runs every fixture website (benchmark/sites, ground truth in _expected.json) through the
same engine the workers use, in safe test mode: no request leaves the machine. Reports

* career-page discovery recall
* vacancy precision / recall (matched on normalized title)
* field accuracy over the fields given in the ground truth
* pages processed per second, successful postings per hour (wall clock of this run)
* peak RSS memory and CPU time of this process
* browser fallback share (companies that needed the browser)

The numbers describe *these fixtures on this machine*. They are not a forecast for live
websites, where politeness delays (2 s per domain) and remote latency dominate.

    python -m benchmark.run [--browser] [--repeat 3] [--json out.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import resource
import shutil
import sys
import time
from pathlib import Path

from career_crawler.config import CrawlerSettings
from career_crawler.engine import CompanyInfo, SiteScan
from career_crawler.extract.normalize import normalize_title
from career_crawler.fetch import Fetcher, MemoryThrottle
from career_crawler.testing import FixtureTransport, fixture_resolver
from gcc_core.netsafe import NetPolicy
from gcc_core.urls import domain_key

SITES = Path(__file__).parent / "sites"
FIELDS = (
    "city",
    "postcode",
    "remote_type",
    "date_posted",
    "salary_min",
    "salary_max",
    "employment_types",
    "ats_job_id",
)


def _value(job, field):
    v = getattr(job, field)
    if field == "date_posted":
        return v.isoformat() if v else None
    return v


def _field_ok(exp, job, field) -> bool:
    if field == "employment_types":
        return set(exp[field]) <= set(job.employment_types)
    return _value(job, field) == exp[field]


async def run_once(use_browser: bool, executable: str | None) -> dict:
    expected = json.loads((SITES / "_expected.json").read_text("utf-8"))
    s = CrawlerSettings(
        per_domain_delay_s=0.0, browser_enabled=use_browser, browser_executable=executable, early_stop_after=8
    )
    t = FixtureTransport(SITES)
    f = Fetcher(
        s,
        policy=NetPolicy(),
        user_agent="GCC-Benchmark/1.0",
        transport=t,
        resolver=fixture_resolver(t),
        throttle=MemoryThrottle(),
    )
    renderer = None
    if use_browser:
        from career_crawler.browser import BrowserFetcher, BrowserRenderer

        renderer = BrowserRenderer(s, f)
        await renderer.start()
    per_site = {}
    tp = fp = fn = 0
    field_hits = field_total = 0
    career_hit = career_total = 0
    browser_sites = 0
    pages = 0
    t0 = time.perf_counter()
    try:
        for i, (host, exp) in enumerate(expected.items()):
            info = CompanyInfo(i, host, domain_key(host), f"https://{host}/")
            r = await SiteScan(f, s, info).run()
            jobs = list(r.jobs)
            pages += r.pages_fetched
            if r.browser_urls and renderer is not None:
                browser_sites += 1
                br = await SiteScan(
                    BrowserFetcher(renderer, f),
                    s,
                    info,
                    seeds=r.browser_urls,
                    rendered=True,
                    max_pages=s.browser_max_urls_per_company * 3,
                ).run()
                jobs += br.jobs
                pages += br.pages_fetched
            elif r.browser_urls:
                browser_sites += 1
            found = {normalize_title(j.job.title): j.job for j in jobs}
            want = {normalize_title(j["title"]): j for j in exp["jobs"]}
            s_tp = len(found.keys() & want.keys())
            tp += s_tp
            fp += len(found.keys() - want.keys())
            fn += len(want.keys() - found.keys())
            for key, e in want.items():
                if key not in found:
                    continue
                for field in FIELDS:
                    if field in e:
                        field_total += 1
                        field_hits += _field_ok(e, found[key], field)
            # career-page discovery: expected career URL among the sources found
            srcs = {u.split("?")[0].rstrip("/") for u in [*r.sources.keys(), *r.browser_urls]}
            for u in exp["career_pages"]:
                career_total += 1
                career_hit += u.split("?")[0].rstrip("/") in srcs
            per_site[host] = {
                "outcome": r.outcome,
                "expected_outcome": exp["outcome"],
                "found": len(found),
                "expected": len(want),
                "true_positive": s_tp,
                "pages": r.pages_fetched,
                "strategy": r.best_strategy,
                "seconds": r.elapsed_s,
            }
    finally:
        if renderer is not None:
            await renderer.close()
        await f.aclose()
    wall = time.perf_counter() - t0
    return {
        "wall_s": wall,
        "pages": pages,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "field_hits": field_hits,
        "field_total": field_total,
        "career_hit": career_hit,
        "career_total": career_total,
        "browser_sites": browser_sites,
        "sites": len(expected),
        "per_site": per_site,
        "requests": len(t.requests),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--browser", action="store_true", help="also render JS career pages with Chromium")
    ap.add_argument("--executable", default=os.environ.get("CRAWLER_BROWSER_EXECUTABLE"))
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--json", help="write the full report to this file")
    args = ap.parse_args(argv)
    if not (SITES / "_expected.json").exists():
        from .build_fixtures import build

        build()
    exe = args.executable or (shutil.which("chromium") or shutil.which("google-chrome") if args.browser else None)
    cpu0 = time.process_time()
    runs = [asyncio.run(run_once(args.browser, exe)) for _ in range(args.repeat)]
    cpu = time.process_time() - cpu0
    last = runs[-1]
    walls = sorted(r["wall_s"] for r in runs)
    median_wall = walls[len(walls) // 2]
    precision = last["tp"] / max(1, last["tp"] + last["fp"])
    recall = last["tp"] / max(1, last["tp"] + last["fn"])
    peak_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # Linux: KiB
    report = {
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "cpus": os.cpu_count(),
            "browser": bool(args.browser),
            "repeat": args.repeat,
            "mode": "offline fixtures",
        },
        "sites": last["sites"],
        "career_page_discovery_recall": round(last["career_hit"] / max(1, last["career_total"]), 3),
        "vacancy_precision": round(precision, 3),
        "vacancy_recall": round(recall, 3),
        "field_accuracy": round(last["field_hits"] / max(1, last["field_total"]), 3),
        "field_checks": last["field_total"],
        "browser_fallback_share": round(last["browser_sites"] / max(1, last["sites"]), 3),
        "pages_per_run": last["pages"],
        "median_wall_s": round(median_wall, 3),
        "pages_per_second": round(last["pages"] / median_wall, 1),
        "jobs_per_hour": round(last["tp"] / median_wall * 3600),
        "cpu_s_per_run": round(cpu / args.repeat, 3),
        "cpu_utilization": round(cpu / sum(r["wall_s"] for r in runs), 3),
        "peak_rss_mb": round(peak_rss_mb, 1),
        "per_site": last["per_site"],
        "note": "Offline fixtures without politeness delays; not a forecast for live websites.",
    }
    print(json.dumps({k: v for k, v in report.items() if k != "per_site"}, indent=2))
    print("\nper site:")
    for host, r in report["per_site"].items():
        flag = "" if r["outcome"] == r["expected_outcome"] and r["true_positive"] == r["expected"] else "  <-- check"
        print(
            f"  {host:30} {r['outcome']:20} {r['true_positive']}/{r['expected']} (found {r['found']}) "
            f"pages={r['pages']} strategy={r['strategy']}{flag}"
        )
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
