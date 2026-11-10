"""Public ATS feed adapters: Personio XML, Greenhouse Job Board API, Lever postings API."""

from __future__ import annotations

import html as html_lib
import json
import re
from urllib.parse import parse_qs

from lxml import etree

from ..extract.normalize import RawJob
from .base import AtsMatch, FeedResult, valid_account

_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, recover=True)


class AtsFeedError(Exception):
    kind = "ats_feed_error"


def _json(page) -> object:
    if page.status != 200:
        raise AtsFeedError(f"feed answered HTTP {page.status}")
    if page.truncated:
        raise AtsFeedError("feed larger than the configured limit")
    try:
        return json.loads(page.body)
    except ValueError as exc:
        raise AtsFeedError(f"invalid JSON: {exc}") from exc


# --- Personio --------------------------------------------------------------------------------


class PersonioAdapter:
    """``https://<account>.jobs.personio.de/xml`` – public XML feed of all published positions."""

    name = "personio"
    _HOST = re.compile(r"^https?://([a-z0-9][a-z0-9-]{0,62})\.jobs\.personio\.(de|com)(?:[/?#:]|$)", re.I)

    def detect(self, url: str) -> AtsMatch | None:
        m = self._HOST.match(url.strip())
        if not m or not valid_account(m.group(1)):
            return None
        acc, tld = m.group(1).lower(), m.group(2).lower()
        base = f"https://{acc}.jobs.personio.{tld}"
        return AtsMatch("personio", acc, f"{base}/xml", base + "/", url[:300])

    async def fetch(self, fetcher, match: AtsMatch) -> FeedResult:
        page = await fetcher.get(match.feed_url, kind="feed")
        if page.status != 200:
            raise AtsFeedError(f"feed answered HTTP {page.status}")
        if page.truncated:
            raise AtsFeedError("feed larger than the configured limit")
        try:
            root = etree.fromstring(page.body, parser=_XML_PARSER)
        except etree.XMLSyntaxError as exc:
            raise AtsFeedError(f"invalid XML: {exc}") from exc
        if root is None or root.tag not in ("workzag-jobs", "jobs"):
            raise AtsFeedError(f"unexpected XML root {getattr(root, 'tag', None)!r}")
        base = match.board_url.rstrip("/")
        jobs = []
        for pos in root.iter("position"):

            def t(tag, _pos=pos):
                n = _pos.find(tag)
                return n.text.strip() if n is not None and n.text else None

            pid = t("id")
            title = t("name")
            if not pid or not title:
                continue
            offices = [t("office")] + [o.text.strip() for o in pos.findall("additionalOffices/office") if o.text]
            offices = [o for o in offices if o]
            parts = []
            for d in pos.findall("jobDescriptions/jobDescription"):
                name = (d.findtext("name") or "").strip()
                value = d.findtext("value") or ""
                parts.append(f"<h3>{html_lib.escape(name)}</h3>{value}" if name else value)
            schedule, emp = t("schedule"), t("employmentType")
            jobs.append(
                RawJob(
                    title=title,
                    source_url=f"{base}/job/{pid}",
                    method="ats",
                    application_url=f"{base}/job/{pid}#apply",
                    career_page_url=base + "/",
                    location_raw="; ".join(offices) or None,
                    locations=[{"city": o} for o in offices],
                    employment_raw=[x for x in (schedule, emp) if x],
                    date_posted_raw=t("createdAt"),
                    description_html="\n".join(parts),
                    ats_type="personio",
                    ats_account=match.account,
                    ats_job_id=pid,
                    confidence=0.95,
                    source_values={
                        "department": t("department"),
                        "seniority": t("seniority"),
                        "recruitingCategory": t("recruitingCategory"),
                        "subcompany": t("subcompany"),
                        "createdAt": t("createdAt"),
                        "schedule": schedule,
                        "employmentType": emp,
                        "yearsOfExperience": t("yearsOfExperience"),
                    },
                )
            )
        return FeedResult(jobs=jobs, complete=True, feed_url=match.feed_url)


# --- Greenhouse ------------------------------------------------------------------------------


class GreenhouseAdapter:
    """Greenhouse Job Board API: ``boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true``."""

    name = "greenhouse"
    _PATTERNS = [
        re.compile(
            r"^https?://(?:boards|job-boards)(?P<eu>\.eu)?\.greenhouse\.io/embed/job_board(?:/js)?\?(?P<q>[^#]*)", re.I
        ),
        re.compile(r"^https?://boards-api(?P<eu>\.eu)?\.greenhouse\.io/v1/boards/(?P<acc>[A-Za-z0-9_-]+)", re.I),
        re.compile(
            r"^https?://(?:boards|job-boards)(?P<eu>\.eu)?\.greenhouse\.io/(?P<acc>[A-Za-z0-9_-]+)(?:[/?#]|$)", re.I
        ),
    ]

    def detect(self, url: str) -> AtsMatch | None:
        url = url.strip()
        for rx in self._PATTERNS:
            m = rx.match(url)
            if not m:
                continue
            acc = m.groupdict().get("acc")
            if not acc and m.groupdict().get("q") is not None:
                acc = (parse_qs(m.group("q")).get("for") or [None])[0]
            if not valid_account(acc):
                return None
            eu = bool(m.group("eu"))
            api = f"https://boards-api{'.eu' if eu else ''}.greenhouse.io/v1/boards/{acc}/jobs?content=true"
            board = f"https://job-boards{'.eu' if eu else ''}.greenhouse.io/{acc}"
            return AtsMatch("greenhouse", acc, api, board, url[:300], region="eu" if eu else None)
        return None

    async def fetch(self, fetcher, match: AtsMatch) -> FeedResult:
        no_content = False
        page = await fetcher.get(match.feed_url, kind="feed")
        if page.status == 200 and page.truncated:
            # The list with descriptions is bigger than the byte limit: the *list* is still the
            # complete catalogue, so fall back to it without bodies instead of dropping the board.
            page = await fetcher.get(match.feed_url.replace("?content=true", ""), kind="feed")
            no_content = True
        data = _json(page)
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise AtsFeedError("unexpected Greenhouse response")
        jobs = []
        seen: set[str] = set()
        for j in data["jobs"]:
            if not isinstance(j, dict) or not j.get("id") or not j.get("title"):
                continue
            jid = str(j["id"])
            if jid in seen:  # two postings with the same *title* are fine; the same *id* twice is not
                continue
            seen.add(jid)
            loc = (j.get("location") or {}).get("name") if isinstance(j.get("location"), dict) else None
            offices = [o for o in j.get("offices") or [] if isinstance(o, dict)]
            office_names = [str(o.get("name")).strip() for o in offices if o.get("name")]
            office_locs = [
                str(o.get("location") or o.get("name")).strip() for o in offices if o.get("location") or o.get("name")
            ]
            content = html_lib.unescape(j.get("content") or "")
            url = j.get("absolute_url") or f"{match.board_url}/jobs/{jid}"
            meta = {m.get("name"): _meta_value(m.get("value")) for m in j.get("metadata") or [] if isinstance(m, dict)}
            emp = [str(v) for k, v in meta.items() if k and re.search(r"employment|type|schedule", k, re.I) and v]
            remote = next(
                (str(v) for k, v in meta.items() if k and re.search(r"remote|workplace|work model", k, re.I) and v),
                None,
            )
            jobs.append(
                RawJob(
                    title=j["title"].strip(),
                    source_url=url,
                    method="ats",
                    application_url=url,
                    career_page_url=match.board_url,
                    location_raw=loc or ("; ".join(office_names) or None),
                    locations=[{"city": x} for x in dict.fromkeys(office_locs)][:10],
                    remote_raw=remote,
                    employment_raw=emp,
                    # updated_at is *not* a publication date; only first_published is used.
                    date_posted_raw=j.get("first_published"),
                    description_html=content,
                    salary=_greenhouse_pay(j.get("pay_input_ranges")),
                    ats_type="greenhouse",
                    ats_account=match.account,
                    ats_job_id=jid,
                    confidence=0.95,
                    source_values={
                        "updated_at": j.get("updated_at"),
                        "first_published": j.get("first_published"),
                        "requisition_id": j.get("requisition_id"),
                        "internal_job_id": j.get("internal_job_id"),
                        "language": j.get("language"),
                        "departments": [d.get("name") for d in j.get("departments") or [] if isinstance(d, dict)],
                        "offices": office_names,
                        "no_content": no_content or None,
                    },
                )
            )
        complete = True
        total = (data.get("meta") or {}).get("total") if isinstance(data.get("meta"), dict) else None
        if isinstance(total, int) and total != len(data["jobs"]):
            complete = False  # the board says it has more/fewer postings than the list returned
        return FeedResult(
            jobs=jobs,
            complete=complete,
            feed_url=match.feed_url,
            reported_total=total if isinstance(total, int) else None,
        )


def _meta_value(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v if x) or None
    if isinstance(v, dict):
        return None
    return str(v)


def _greenhouse_pay(ranges) -> dict | None:
    """``pay_input_ranges`` (only present when the employer enabled pay transparency)."""
    if not isinstance(ranges, list):
        return None
    for r in ranges:
        if not isinstance(r, dict):
            continue
        lo, hi = r.get("min_cents"), r.get("max_cents")
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and hi >= lo > 0:
            return {
                "min": lo / 100.0,
                "max": hi / 100.0,
                "currency": r.get("currency_type") or None,
                "period": None,
                "raw": json.dumps({k: r.get(k) for k in ("min_cents", "max_cents", "currency_type", "title")})[:200],
            }
    return None


# --- Lever -----------------------------------------------------------------------------------


class LeverAdapter:
    """Lever postings API: ``api.lever.co/v0/postings/<account>?mode=json`` (EU: ``api.eu.lever.co``)."""

    name = "lever"
    _PATTERNS = [
        re.compile(r"^https?://jobs(?P<eu>\.eu)?\.lever\.co/(?P<acc>[A-Za-z0-9_.-]+)(?:[/?#]|$)", re.I),
        re.compile(r"^https?://api(?P<eu>\.eu)?\.lever\.co/v0/postings/(?P<acc>[A-Za-z0-9_.-]+)", re.I),
    ]
    PAGE = 100
    MAX_PAGES = 10

    def detect(self, url: str) -> AtsMatch | None:
        for rx in self._PATTERNS:
            m = rx.match(url.strip())
            if m and valid_account(m.group("acc")):
                acc, eu = m.group("acc"), bool(m.group("eu"))
                host = "api.eu.lever.co" if eu else "api.lever.co"
                board = f"https://jobs{'.eu' if eu else ''}.lever.co/{acc}"
                return AtsMatch(
                    "lever",
                    acc,
                    f"https://{host}/v0/postings/{acc}?mode=json",
                    board,
                    url[:300],
                    region="eu" if eu else None,
                )
        return None

    async def fetch(self, fetcher, match: AtsMatch) -> FeedResult:
        jobs = []
        complete = True
        seen: set[str] = set()
        pages = 0
        for page_no in range(self.MAX_PAGES):
            url = f"{match.feed_url}&skip={page_no * self.PAGE}&limit={self.PAGE}"
            data = _json(await fetcher.get(url, kind="feed"))
            pages += 1
            if not isinstance(data, list):
                raise AtsFeedError("unexpected Lever response")
            for p in data:
                if not isinstance(p, dict) or not p.get("id") or not p.get("text"):
                    continue
                if p["id"] in seen:  # pages can overlap when postings change while we page
                    continue
                seen.add(p["id"])
                cats = p.get("categories") or {}
                locs = cats.get("allLocations") or ([cats.get("location")] if cats.get("location") else [])
                country = (p.get("country") or "").strip().upper() or None
                parts = [p.get("description") or ""]
                for lst in p.get("lists") or []:
                    if isinstance(lst, dict):
                        parts.append(
                            f"<h3>{html_lib.escape(lst.get('text') or '')}</h3><ul>{lst.get('content') or ''}</ul>"
                        )
                parts.append(p.get("additional") or "")
                if p.get("salaryDescription"):  # free text; the normaliser parses salaries out of the body
                    parts.append(
                        f"<p>{p['salaryDescription']}</p>"
                        if "<" not in p["salaryDescription"]
                        else p["salaryDescription"]
                    )
                sal = p.get("salaryRange") if isinstance(p.get("salaryRange"), dict) else None
                salary = None
                if sal and sal.get("min") and sal.get("max"):
                    period = {"per-year-salary": "year", "per-month-salary": "month", "per-hour-wage": "hour"}.get(
                        sal.get("interval") or ""
                    )
                    salary = {
                        "min": float(sal["min"]),
                        "max": float(sal["max"]),
                        "currency": sal.get("currency") or None,
                        "period": period,
                        "raw": json.dumps(sal)[:200],
                    }
                jobs.append(
                    RawJob(
                        title=p["text"],
                        source_url=p.get("hostedUrl") or f"{match.board_url}/{p['id']}",
                        method="ats",
                        application_url=p.get("applyUrl"),
                        career_page_url=match.board_url,
                        location_raw="; ".join(str(x) for x in locs if x) or None,
                        locations=[
                            ({"city": str(x), "country": country} if country and len(locs) == 1 else {"city": str(x)})
                            for x in locs
                            if x
                        ],
                        remote_raw=p.get("workplaceType"),
                        employment_raw=[cats["commitment"]] if cats.get("commitment") else [],
                        date_posted_raw=p.get("createdAt"),
                        description_html="\n".join(parts),
                        salary=salary,
                        ats_type="lever",
                        ats_account=match.account,
                        ats_job_id=p["id"],
                        confidence=0.95,
                        source_values={
                            "team": cats.get("team"),
                            "department": cats.get("department"),
                            "workplaceType": p.get("workplaceType"),
                            "createdAt": p.get("createdAt"),
                            "country": country,
                        },
                    )
                )
            if len(data) < self.PAGE:
                break
        else:
            complete = False  # more postings than we are willing to page through
        return FeedResult(jobs=jobs, complete=complete, feed_url=match.feed_url, pages=pages)
