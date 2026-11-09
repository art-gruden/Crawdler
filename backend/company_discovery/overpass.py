"""OpenStreetMap Overpass adapter.

Usage policy (overpass-api.de): stay well below ~10 000 requests and ~1 GB per day,
and far below that for recurring jobs. This client therefore

* runs at most one request at a time per deployment (PostgreSQL advisory lock),
* waits ``OVERPASS_MIN_INTERVAL_S`` between requests,
* counts requests and bytes per UTC day and stops at the configured budget,
* caches identical queries for ``OVERPASS_CACHE_HOURS``,
* honours HTTP 429 by asking ``/api/status`` when the next slot is free, and backs
  off exponentially on 504/timeouts,
* sends an identifying User-Agent.

Data from OSM is © OpenStreetMap contributors and licensed under the ODbL 1.0.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from gcc_core.log import get_logger

from .industries import Industry, classify_tags, render_condition
from .regions import Region

log = get_logger(__name__)

OSM_LICENSE = "ODbL-1.0"
OSM_ATTRIBUTION = "© OpenStreetMap contributors (https://www.openstreetmap.org/copyright)"
ADVISORY_LOCK_KEY = 0x6F7665727061  # "overpa"
WEBSITE_KEYS = ("website", "contact:website")

# Only these tags are stored. Phone numbers, e-mail addresses and opening hours are
# deliberately dropped (data minimisation: sole traders are natural persons).
KEEP_TAGS = frozenset(
    {
        "name",
        "official_name",
        "short_name",
        "brand",
        "operator",
        "website",
        "contact:website",
        "addr:street",
        "addr:housenumber",
        "addr:postcode",
        "addr:city",
        "office",
        "industrial",
        "man_made",
        "company",
        "industry",
        "amenity",
        "craft",
        "wikidata",
        "check_date",
    }
)


class OverpassError(Exception):
    pass


class OverpassBudgetExceeded(OverpassError):
    pass


class OverpassQueryError(OverpassError):
    pass


class RegionNotFound(OverpassError):
    pass


# --------------------------------------------------------------------------- query


def build_query(region: Region, industries: list[Industry], *, timeout_s: int, maxsize_mb: int) -> str:
    website = '[~"^(website|contact:website)$"~"."]'
    lines = [
        f"[out:json][timeout:{int(timeout_s)}][maxsize:{int(maxsize_mb) * 1024 * 1024}];",
        region.overpass_area("a"),
        ".a out ids;",
        "(",
    ]
    for industry in industries:
        for selector in industry.selectors:
            conds = "".join(render_condition(c) for c in selector)
            lines.append(f'  nwr{conds}["name"]{website}(area.a);')
    lines += [");", "out tags center;"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- parse


@dataclass
class OsmCompany:
    osm_type: str
    osm_id: int
    name: str
    websites: list[str]
    tags: dict[str, str]
    lat: float | None
    lon: float | None
    industries: list[str]

    @property
    def record_id(self) -> str:
        return f"{self.osm_type}/{self.osm_id}"

    @property
    def url(self) -> str:
        return f"https://www.openstreetmap.org/{self.osm_type}/{self.osm_id}"


@dataclass
class ParsedResult:
    companies: list[OsmCompany]
    area_ids: list[int]
    osm_base: dt.datetime | None
    skipped: int = 0
    remark: str | None = None
    raw_elements: int = 0


def parse_response(payload: dict, industries: list[Industry]) -> ParsedResult:
    remark = payload.get("remark")
    if remark and re.search(r"runtime error|out of memory|timed out", remark, re.I):
        raise OverpassQueryError(f"Overpass aborted the query: {remark.strip()[:300]}")
    osm_base = None
    ts = (payload.get("osm3s") or {}).get("timestamp_osm_base")
    if ts:
        try:
            osm_base = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            osm_base = None
    seen: set[tuple[str, int]] = set()
    companies: list[OsmCompany] = []
    area_ids: list[int] = []
    skipped = 0
    elements = payload.get("elements") or []
    for el in elements:
        etype = el.get("type")
        if etype == "area":
            area_ids.append(int(el["id"]))
            continue
        if etype not in ("node", "way", "relation"):
            continue
        key = (etype, int(el["id"]))
        if key in seen:
            continue
        seen.add(key)
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        websites = [tags[k] for k in WEBSITE_KEYS if tags.get(k)]
        if not name or not websites:
            skipped += 1
            continue
        center = el.get("center") or {}
        lat = el.get("lat", center.get("lat"))
        lon = el.get("lon", center.get("lon"))
        companies.append(
            OsmCompany(
                osm_type=etype,
                osm_id=int(el["id"]),
                name=name,
                websites=websites,
                tags={k: v for k, v in tags.items() if k in KEEP_TAGS},
                lat=float(lat) if lat is not None else None,
                lon=float(lon) if lon is not None else None,
                industries=classify_tags(tags, industries),
            )
        )
    return ParsedResult(companies, area_ids, osm_base, skipped, remark, len(elements))


# --------------------------------------------------------------------------- client


@dataclass
class FetchMeta:
    from_cache: bool
    bytes: int
    attempts: int
    waited_s: float = 0.0
    statuses: list[int] = field(default_factory=list)


class OverpassClient:
    def __init__(
        self,
        settings,
        session_factory: Callable[[], Session],
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        cancelled: Callable[[], bool] = lambda: False,
    ):
        self.s = settings
        self.session_factory = session_factory
        self.http = http or httpx.Client(timeout=httpx.Timeout(settings.overpass_query_timeout_s + 30, connect=15))
        self.sleep = sleep
        self.cancelled = cancelled
        self.cache_dir = Path(settings.data_dir) / "overpass-cache"

    # -- cache
    def _cache_path(self, query: str) -> Path:
        return self.cache_dir / (hashlib.sha256(query.encode()).hexdigest() + ".json")

    def _cache_get(self, query: str) -> dict | None:
        path = self._cache_path(query)
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > self.s.overpass_cache_hours * 3600:
            return None
        try:
            return json.loads(path.read_bytes())
        except (OSError, ValueError):
            return None

    def _cache_put(self, query: str, body: bytes) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path(query).with_suffix(".tmp")
            tmp.write_bytes(body)
            tmp.replace(self._cache_path(query))
        except OSError as exc:
            log.warning("overpass.cache_write_failed", error=str(exc))

    # -- budget
    def usage_today(self, session: Session) -> dict:
        row = session.execute(
            text("SELECT requests, bytes, last_request_at FROM external_api_usage WHERE service='overpass' AND day=:d"),
            {"d": dt.datetime.now(dt.UTC).date()},
        ).first()
        return {
            "requests": row.requests if row else 0,
            "bytes": row.bytes if row else 0,
            "max_requests": self.s.overpass_max_requests_per_day,
            "max_bytes": self.s.overpass_max_mb_per_day * 1024 * 1024,
            "last_request_at": row.last_request_at.isoformat() if row and row.last_request_at else None,
        }

    def _reserve(self, session: Session) -> None:
        """Count one request against today's budget and enforce the minimum interval."""
        today = dt.datetime.now(dt.UTC).date()
        row = session.execute(
            text(
                """
                INSERT INTO external_api_usage (service, day, requests, bytes)
                VALUES ('overpass', :d, 0, 0)
                ON CONFLICT (service, day) DO UPDATE SET service = EXCLUDED.service
                RETURNING requests, bytes
                """
            ),
            {"d": today},
        ).one()
        if row.requests >= self.s.overpass_max_requests_per_day:
            raise OverpassBudgetExceeded(
                f"Overpass daily request budget reached ({row.requests}/{self.s.overpass_max_requests_per_day}). "
                "The job can be started again tomorrow; finished regions are served from cache."
            )
        if row.bytes >= self.s.overpass_max_mb_per_day * 1024 * 1024:
            raise OverpassBudgetExceeded(f"Overpass daily download budget reached ({row.bytes // (1024 * 1024)} MB).")
        last = session.execute(
            text("SELECT max(last_request_at) FROM external_api_usage WHERE service='overpass'")
        ).scalar()
        if last is not None:
            wait = self.s.overpass_min_interval_s - (dt.datetime.now(dt.UTC) - last).total_seconds()
            if wait > 0:
                self._sleep(wait)
        session.execute(
            text(
                "UPDATE external_api_usage SET requests = requests + 1, last_request_at = now() "
                "WHERE service='overpass' AND day=:d"
            ),
            {"d": today},
        )
        session.commit()

    def _add_bytes(self, session: Session, n: int) -> None:
        session.execute(
            text("UPDATE external_api_usage SET bytes = bytes + :n WHERE service='overpass' AND day=:d"),
            {"n": n, "d": dt.datetime.now(dt.UTC).date()},
        )
        session.commit()

    def _sleep(self, seconds: float) -> None:
        """Sleep in 5 s steps so that a cancelled job stops waiting."""
        left = seconds
        while left > 0:
            if self.cancelled():
                raise OverpassError("cancelled")
            step = min(left, 5.0)
            self.sleep(step)
            left -= step

    def _slot_wait(self) -> float | None:
        """Ask /api/status when the next slot is free (does not count against the budget)."""
        try:
            r = self.http.get(self.s.overpass_status_url, headers={"User-Agent": self.s.crawler_user_agent}, timeout=15)
        except httpx.HTTPError:
            return None
        waits = [int(m) for m in re.findall(r"in (\d+) seconds", r.text)]
        if "slots available now" in r.text and not waits:
            return 0.0
        return float(min(waits)) if waits else None

    # -- main entry
    def run(self, query: str) -> tuple[dict, FetchMeta]:
        cached = self._cache_get(query)
        if cached is not None:
            return cached, FetchMeta(from_cache=True, bytes=0, attempts=0)

        meta = FetchMeta(from_cache=False, bytes=0, attempts=0)
        max_bytes = self.s.overpass_max_response_mb * 1024 * 1024
        with self.session_factory() as lock_session:
            lock_session.execute(text("SELECT pg_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
            try:
                for attempt in range(self.s.overpass_max_retries + 1):
                    meta.attempts = attempt + 1
                    with self.session_factory() as s:
                        self._reserve(s)
                    try:
                        status, body = self._post(query, max_bytes)
                    except httpx.TimeoutException:
                        status, body = 504, b""
                    except httpx.TransportError as exc:
                        log.warning("overpass.transport_error", error=str(exc), attempt=attempt)
                        status, body = 599, b""
                    meta.statuses.append(status)
                    meta.bytes += len(body)
                    with self.session_factory() as s:
                        self._add_bytes(s, len(body))
                    if status == 200:
                        try:
                            payload = json.loads(body)
                        except ValueError as exc:
                            raise OverpassQueryError("Overpass returned invalid JSON") from exc
                        if not (payload.get("remark") or "").strip():
                            self._cache_put(query, body)
                        return payload, meta
                    if status == 400:
                        raise OverpassQueryError(f"Overpass rejected the query: {_error_text(body)}")
                    if status in (413,):
                        raise OverpassQueryError("Overpass response exceeds OVERPASS_MAX_RESPONSE_MB")
                    if attempt == self.s.overpass_max_retries:
                        break
                    if status == 429:
                        wait = self._slot_wait()
                        wait = 60.0 * 2**attempt if wait is None else min(max(wait, 1.0) + 5, 600.0)
                    else:  # 504 gateway timeout / server load, 5xx, transport errors
                        wait = min(60.0 * 2**attempt, 900.0)
                    log.info("overpass.retry", status=status, wait_s=wait, attempt=attempt + 1)
                    meta.waited_s += wait
                    self._sleep(wait)
                raise OverpassError(f"Overpass unavailable after {meta.attempts} attempts (HTTP {meta.statuses})")
            finally:
                lock_session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
                lock_session.commit()

    def _post(self, query: str, max_bytes: int) -> tuple[int, bytes]:
        headers = {"User-Agent": self.s.crawler_user_agent, "Accept": "application/json"}
        with self.http.stream("POST", self.s.overpass_url, data={"data": query}, headers=headers) as r:
            chunks = bytearray()
            for chunk in r.iter_bytes():
                chunks.extend(chunk)
                if len(chunks) > max_bytes:
                    return 413, bytes(chunks[:1024])
            return r.status_code, bytes(chunks)


def _error_text(body: bytes) -> str:
    text_ = body.decode("utf-8", errors="replace")
    m = re.findall(r"<strong[^>]*>Error</strong>:?\s*(.*?)</p>", text_, re.S)
    msg = " ".join(re.sub(r"<[^>]+>", "", x).strip() for x in m) or text_[:300]
    return re.sub(r"\s+", " ", msg)[:500]
