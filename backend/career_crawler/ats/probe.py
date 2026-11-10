"""Live validation probe for ONE ATS board (operator tool; read-only, writes nothing to the database).

    python -m career_crawler.ats.probe <url-or-board-url> [--save DIR] [--json]

It uses the same SSRF-safe client as the crawler (robots.txt, per-domain delay, size limits, the
crawler User-Agent) and the production adapter, makes the minimum number of requests for one board,
and prints what a live validation needs: HTTP outcome, posting count vs the provider's own total,
field coverage and a verdict. Use it to validate Lever (or any provider) against an active employer
with a non-empty public board *before* trusting the adapter, e.g.::

    python -m career_crawler.ats.probe https://jobs.lever.co/<account>

A provider whose robots.txt disallows the API (or answers 401/403 on strict hosts) is reported as
``blocked`` - the probe never tries to get around it. ``--save DIR`` stores the raw response bodies
so they can become offline fixtures.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .. import ats
from ..extract.normalize import normalize
from ..fetch import CrawlStop, RobotsDisallowed


class _Recorder:
    """Wraps the fetcher and keeps the raw bodies of the feed requests."""

    def __init__(self, fetcher):
        self._f = fetcher
        self.pages: list = []

    async def get(self, url, **kw):
        page = await self._f.get(url, **kw)
        self.pages.append(page)
        return page


async def probe(url: str, *, save: str | None = None) -> dict:
    from ..config import get_crawler_settings
    from ..runner import build_fetcher
    from . import capabilities as caps_mod

    settings = get_crawler_settings()
    match = ats.detect_url(url)
    if match is None:
        return {"verdict": "unsupported", "reason": "no known ATS board in this URL"}
    adapter = ats.adapter(match.ats_type)
    caps = caps_mod.capabilities(match.ats_type)
    out: dict = {
        "provider": match.ats_type,
        "account": match.account,
        "feed_url": match.feed_url,
        "access_status_in_code": caps.access_status if caps else None,
        "enabled_in_crawler": match.ats_type in caps_mod.enabled_providers(settings.ats_enabled_providers),
    }
    fetcher = build_fetcher(settings)
    rec = _Recorder(fetcher)
    try:
        configure = getattr(adapter, "configure", None)
        if configure:
            configure(settings)
        res = await adapter.fetch(rec, match)
    except RobotsDisallowed as exc:
        return {**out, "verdict": "blocked", "reason": str(exc)}
    except CrawlStop as exc:
        return {**out, "verdict": "stopped", "reason": f"{exc.kind}: {exc}"}
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not crash
        return {**out, "verdict": "error", "reason": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        await fetcher.aclose()
    jobs = [normalize(j, max_chars=settings.description_max_chars) for j in res.jobs]
    jobs = [j for j in jobs if j is not None]

    def pct(pred) -> float:
        return round(sum(1 for j in jobs if pred(j)) / len(jobs), 3) if jobs else 0.0

    out.update(
        http_requests=len(rec.pages),
        http_status=[p.status for p in rec.pages],
        bytes=sum(len(p.body) for p in rec.pages),
        postings=len(res.jobs),
        normalized=len(jobs),
        unique_job_ids=len({j.ats_job_id for j in res.jobs}),
        reported_total=res.reported_total,
        complete=res.complete,
        coverage={
            "description": pct(lambda j: bool(j.description)),
            "location": pct(lambda j: bool(j.city or j.country)),
            "date_posted": pct(lambda j: j.date_posted is not None),
            "employment_type": pct(lambda j: bool(j.employment_types)),
            "salary": pct(lambda j: j.salary_min is not None),
        },
        sample_titles=[j.title for j in jobs[:5]],
    )
    if not jobs:
        out["verdict"] = "valid_but_empty"
        out["reason"] = "the board answered correctly but lists no postings: parsing is NOT validated by this probe"
    elif res.complete and out["unique_job_ids"] == len(res.jobs):
        out["verdict"] = "validated_nonempty"
        out["reason"] = "non-empty live board parsed; compare the sample titles with the public career page"
    else:
        out["verdict"] = "partial"
        out["reason"] = "board parsed but the listing is not provably complete (total mismatch, cap or duplicates)"
    if save:
        d = Path(save)
        d.mkdir(parents=True, exist_ok=True)
        for i, p in enumerate(rec.pages):
            (d / f"{match.ats_type}_{match.account}_{i}.json").write_bytes(p.body)
        out["saved_to"] = str(d)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("url", help="board URL, e.g. https://jobs.lever.co/<account>")
    ap.add_argument("--save", metavar="DIR", help="store the raw response bodies (future fixtures)")
    args = ap.parse_args(argv)
    result = asyncio.run(probe(args.url, save=args.save))
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("verdict") in ("validated_nonempty", "valid_but_empty", "partial") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
