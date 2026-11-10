"""Ashby public job-board adapter.

``GET https://api.ashbyhq.com/posting-api/job-board/<board>?includeCompensation=true`` returns every
*listed* job of the board in one response (no search, no pagination).

STATUS: fixture-only. The v3 ATS diagnostics could not clear robots.txt for the probed boards
(HTTP 401 for deepl and smallpdf, treated as blocked), so the provider is **not enabled by default**
and the shared fetcher refuses the request while robots.txt is unavailable or disallowing.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote

from ..extract.normalize import RawJob
from .base import AtsMatch, FeedResult, valid_account

_EMPLOYMENT = {
    "fulltime": "Full-time",
    "parttime": "Part-time",
    "intern": "Internship",
    "contract": "Contract",
    "temporary": "Temporary",
}
_INTERVAL = {"1 year": "year", "1 month": "month", "1 hour": "hour", "1 week": "week", "1 day": "day"}


class AshbyAdapter:
    name = "ashby"
    _PATTERNS = [
        re.compile(r"^https?://api\.ashbyhq\.com/posting-api/job-board/(?P<acc>[A-Za-z0-9_.%-]+)", re.I),
        re.compile(r"^https?://jobs\.ashbyhq\.com/(?P<acc>[A-Za-z0-9_.%-]+)(?:[/?#]|$)", re.I),
    ]
    _NOT = {"api", "embed", "robots.txt", "sitemap.xml"}

    def detect(self, url: str) -> AtsMatch | None:
        for rx in self._PATTERNS:
            m = rx.match(url.strip())
            if m and valid_account(m.group("acc")) and m.group("acc").lower() not in self._NOT:
                acc = m.group("acc")
                return AtsMatch(
                    "ashby",
                    acc,
                    f"https://api.ashbyhq.com/posting-api/job-board/{quote(acc)}?includeCompensation=true",
                    f"https://jobs.ashbyhq.com/{quote(acc)}",
                    url[:300],
                )
        return None

    async def fetch(self, fetcher, match: AtsMatch) -> FeedResult:
        from .adapters import AtsFeedError, _json

        data = _json(await fetcher.get(match.feed_url, kind="feed"))
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise AtsFeedError("unexpected Ashby response")
        jobs: list[RawJob] = []
        seen: set[str] = set()
        for j in data["jobs"]:
            if not isinstance(j, dict) or not j.get("id") or not j.get("title"):
                continue
            if j.get("isListed") is False:  # unlisted postings are not public
                continue
            jid = str(j["id"])
            if jid in seen:
                continue
            seen.add(jid)
            jobs.append(self._job(match, j, jid))
        return FeedResult(
            jobs=jobs,
            complete=True,
            feed_url=match.feed_url,
            reported_total=len(data["jobs"]),
            detail={"strategy": "catalog", "unlisted_skipped": len(data["jobs"]) - len(jobs)},
        )

    def _job(self, match: AtsMatch, j: dict, jid: str) -> RawJob:
        addr = ((j.get("address") or {}).get("postalAddress") or {}) if isinstance(j.get("address"), dict) else {}
        primary = {
            k: str(v).strip()
            for k, v in (
                ("city", addr.get("addressLocality")),
                ("region", addr.get("addressRegion")),
                ("country", addr.get("addressCountry")),
            )
            if v
        }
        locs = [primary] if primary else []
        names = [str(j["location"]).strip()] if j.get("location") else []
        for sec in j.get("secondaryLocations") or []:
            if isinstance(sec, dict) and sec.get("location"):
                names.append(str(sec["location"]).strip())
                sa = (
                    ((sec.get("address") or {}).get("postalAddress") or {})
                    if isinstance(sec.get("address"), dict)
                    else {}
                )
                one = {
                    k: str(v).strip()
                    for k, v in (
                        ("city", sa.get("addressLocality")),
                        ("region", sa.get("addressRegion")),
                        ("country", sa.get("addressCountry")),
                    )
                    if v
                }
                locs.append(one or {"city": str(sec["location"]).strip()})
        if not locs:
            locs = [{"city": n} for n in names]
        wp = (j.get("workplaceType") or "").strip()
        remote = "remote" if j.get("isRemote") else (wp.lower() if wp.lower() in ("remote", "hybrid") else None)
        emp = _EMPLOYMENT.get(str(j.get("employmentType") or "").lower())
        url = j.get("jobUrl") or f"{match.board_url}/{jid}"
        return RawJob(
            title=str(j["title"]).strip(),
            source_url=url,
            method="ats",
            application_url=j.get("applyUrl") or url,
            career_page_url=match.board_url,
            location_raw="; ".join(dict.fromkeys(names)) or None,
            locations=locs[:10],
            remote_raw=remote,
            employment_raw=[emp] if emp else [],
            date_posted_raw=j.get("publishedAt"),
            description_html=j.get("descriptionHtml") or None,
            description_text=None if j.get("descriptionHtml") else (j.get("descriptionPlain") or None),
            salary=_salary(j.get("compensation")),
            ats_type="ashby",
            ats_account=match.account,
            ats_job_id=jid,
            confidence=0.95,
            source_values={
                "department": j.get("department"),
                "team": j.get("team"),
                "workplaceType": wp or None,
                "employmentType": j.get("employmentType"),
                "publishedAt": j.get("publishedAt"),
            },
        )


def _salary(comp) -> dict | None:
    if not isinstance(comp, dict):
        return None
    for c in comp.get("summaryComponents") or []:
        if not isinstance(c, dict) or str(c.get("compensationType") or "").lower() != "salary":
            continue
        lo, hi = c.get("minValue"), c.get("maxValue")
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and hi >= lo > 0:
            return {
                "min": float(lo),
                "max": float(hi),
                "currency": c.get("currencyCode") or None,
                "period": _INTERVAL.get(str(c.get("interval") or "").lower()),
                "raw": json.dumps({k: c.get(k) for k in ("minValue", "maxValue", "currencyCode", "interval")})[:200],
            }
    return None
