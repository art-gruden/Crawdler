"""SmartRecruiters Posting API adapter (catalogue + server-side search).

``GET https://api.smartrecruiters.com/v1/companies/<id>/postings?destination=PUBLIC&limit=&offset=``
optionally with ``q`` (keyword), ``country``, ``region``, ``city``.

STATUS: fixture-only. The v3 ATS diagnostics found robots.txt disallowing this API for the two
probed tenants (BoschGroup, DeliveryHero), so the provider is **not enabled by default** and the
shared fetcher refuses the request while robots.txt disallows it. The list endpoint carries no
job description; descriptions would need one extra request per posting, which is intentionally not
done (see docs/ATS.md).
"""

from __future__ import annotations

import re
from urllib.parse import quote, urlencode

from ..extract.normalize import RawJob
from .base import AtsMatch, FeedResult, valid_account
from .capabilities import CAPABILITIES, SearchQuery
from .searchcrawl import SearchPage


class SmartRecruitersAdapter:
    name = "smartrecruiters"
    capabilities = CAPABILITIES["smartrecruiters"]
    PAGE = 100
    MAX_JOBS = 2000  # catalogue cap; the engine overrides it from CRAWLER_ATS_CATALOG_MAX_JOBS

    _PATTERNS = [
        re.compile(
            r"^https?://(?:jobs|careers)\.smartrecruiters\.com/oneclick-ui/company/(?P<acc>[A-Za-z0-9_.-]+)", re.I
        ),
        re.compile(r"^https?://api\.smartrecruiters\.com/v1/companies/(?P<acc>[A-Za-z0-9_.-]+)", re.I),
        re.compile(r"^https?://(?:jobs|careers)\.smartrecruiters\.com/(?P<acc>[A-Za-z0-9_.-]+)(?:[/?#]|$)", re.I),
    ]
    _NOT = {"oneclick-ui", "company", "sitemap.xml", "robots.txt", "api"}

    def configure(self, settings) -> None:
        self.MAX_JOBS = int(getattr(settings, "ats_catalog_max_jobs", self.MAX_JOBS))

    def detect(self, url: str) -> AtsMatch | None:
        for rx in self._PATTERNS:
            m = rx.match(url.strip())
            if m and valid_account(m.group("acc")) and m.group("acc").lower() not in self._NOT:
                acc = m.group("acc")
                return AtsMatch(
                    "smartrecruiters",
                    acc,
                    f"https://api.smartrecruiters.com/v1/companies/{quote(acc)}/postings?destination=PUBLIC",
                    f"https://jobs.smartrecruiters.com/{quote(acc)}",
                    url[:300],
                )
        return None

    # --- parsing -------------------------------------------------------------------------------

    def parse_search(self, match: AtsMatch, data: object) -> SearchPage:
        from .adapters import AtsFeedError

        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise AtsFeedError("unexpected SmartRecruiters response")
        jobs: list[RawJob] = []
        for p in data["content"]:
            if not isinstance(p, dict) or not p.get("id") or not p.get("name"):
                continue
            jobs.append(self._job(match, p))
        total = data.get("totalFound")
        return SearchPage(jobs, total if isinstance(total, int) else None, raw_count=len(data["content"]))

    def _job(self, match: AtsMatch, p: dict) -> RawJob:
        loc = p.get("location") if isinstance(p.get("location"), dict) else {}
        city, region = (loc.get("city") or "").strip(), (loc.get("region") or "").strip()
        country = (loc.get("country") or "").strip().upper()
        full = (loc.get("fullLocation") or "").strip()
        remote = "remote" if loc.get("remote") else ("hybrid" if loc.get("hybrid") else None)
        label = lambda k: (p.get(k) or {}).get("label") if isinstance(p.get(k), dict) else None  # noqa: E731
        pid = str(p["id"])
        url = f"{match.board_url}/{quote(pid)}"
        struct = {k: v for k, v in (("city", city), ("region", region), ("country", country)) if v}
        return RawJob(
            title=str(p["name"]).strip(),
            source_url=url,
            method="ats",
            application_url=url,
            career_page_url=match.board_url,
            location_raw=full or ", ".join(x for x in (city, region, country) if x) or None,
            locations=[struct] if struct else [],
            remote_raw=remote,
            employment_raw=[x for x in (label("typeOfEmployment"),) if x],
            date_posted_raw=p.get("releasedDate"),
            description_html=None,  # not part of the list response
            ats_type="smartrecruiters",
            ats_account=match.account,
            ats_job_id=pid,
            hiring_org=(p.get("company") or {}).get("name") if isinstance(p.get("company"), dict) else None,
            confidence=0.85,  # no description in the list
            source_values={
                "department": label("department"),
                "function": label("function"),
                "experienceLevel": label("experienceLevel"),
                "refNumber": p.get("refNumber"),
                "language": (p.get("language") or {}).get("code") if isinstance(p.get("language"), dict) else None,
                "releasedDate": p.get("releasedDate"),
                "detail_pending": True,
            },
        )

    # --- catalogue -----------------------------------------------------------------------------

    def _page_url(self, match: AtsMatch, offset: int, limit: int, extra: dict | None = None) -> str:
        params = {"destination": "PUBLIC", "limit": limit, "offset": offset, **(extra or {})}
        return f"{match.feed_url.split('?')[0]}?{urlencode(params)}"

    async def fetch(self, fetcher, match: AtsMatch) -> FeedResult:
        from .adapters import _json

        jobs: list[RawJob] = []
        seen: set[str] = set()
        total: int | None = None
        pages = 0
        reached_end = False
        capped = False
        offset = 0
        while True:
            data = _json(await fetcher.get(self._page_url(match, offset, self.PAGE), kind="feed"))
            pages += 1
            sp = self.parse_search(match, data)
            total = sp.total if sp.total is not None else total
            for j in sp.jobs:
                if j.ats_job_id not in seen:
                    seen.add(j.ats_job_id)
                    jobs.append(j)
            offset += self.PAGE
            if sp.raw_count < self.PAGE or (total is not None and offset >= total):
                reached_end = True
                break
            if len(jobs) >= self.MAX_JOBS:
                capped = True
                break
        complete = reached_end and (total is None or len(seen) == total)
        return FeedResult(
            jobs=jobs[: self.MAX_JOBS],
            complete=complete,
            feed_url=match.feed_url,
            reported_total=total,
            pages=pages,
            detail={"strategy": "catalog", "capped": capped, "unique_jobs": len(seen)},
        )

    # --- search --------------------------------------------------------------------------------

    def search_url(self, match: AtsMatch, q: SearchQuery, offset: int, limit: int) -> str:
        extra = {}
        if q.keyword:
            extra["q"] = q.keyword
        if q.country:
            extra["country"] = q.country.lower()
        if q.region:
            extra["region"] = q.region
        if q.city:
            extra["city"] = q.city
        return self._page_url(match, offset, limit, extra)
