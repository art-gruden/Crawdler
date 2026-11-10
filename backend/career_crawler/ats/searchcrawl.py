"""Search-aware crawling: bounded query planning and paginated execution against an ATS search API.

Used by the existing crawler engine (no second crawler): a provider with a search endpoint is
queried with a *bounded* set of keyword x location queries, either because it has no full
catalogue or because its catalogue was larger than the configured cap.

Safety rules (the reason a search can never close a vacancy):

* a search scan is **never complete** - the engine marks the source ``complete=False``, so the job
  lifecycle does not count misses for it;
* a failed, rate-limited or capped query only makes the result *smaller*; it never changes state
  of postings that were not returned;
* every request goes through the shared SSRF-safe fetcher (robots.txt, throttle, size limits);
* parameters are validated against the provider's :class:`SearchSpec` before a URL is built.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from gcc_core.netsafe import FetchError

from ..fetch import CrawlStop, RobotsDisallowed
from .base import AtsMatch, FeedResult
from .capabilities import ProviderCapabilities, SearchParamError, SearchQuery

if TYPE_CHECKING:  # pragma: no cover
    from ..extract.normalize import RawJob
    from ..fetch import Fetcher

# Cities used to split a query that returned more results than we could page through.
REFINE_CITIES: dict[str, tuple[str, ...]] = {
    "DE": (
        "Berlin",
        "München",
        "Hamburg",
        "Köln",
        "Frankfurt am Main",
        "Stuttgart",
        "Düsseldorf",
        "Leipzig",
        "Dresden",
    ),
    "AT": ("Wien", "Graz", "Linz", "Salzburg", "Innsbruck"),
    "CH": ("Zürich", "Genf", "Basel", "Bern", "Lausanne"),
}


@dataclass
class SearchPage:
    jobs: list[RawJob]
    total: int | None = None  # provider's total for this query, when reported
    raw_count: int = 0  # entries on the page before our own filtering


class SearchAdapter(Protocol):
    name: str
    capabilities: ProviderCapabilities

    def search_url(self, match: AtsMatch, q: SearchQuery, offset: int, limit: int) -> str: ...

    def parse_search(self, match: AtsMatch, data: object) -> SearchPage: ...


@dataclass
class SearchLimits:
    max_queries: int = 40
    max_pages_per_query: int = 5
    page_size: int = 100
    max_jobs: int = 5000
    max_consecutive_errors: int = 3


@dataclass
class QueryStat:
    label: str
    pages: int = 0
    returned: int = 0
    new: int = 0
    total: int | None = None
    saturated: bool = False
    error: str | None = None


@dataclass
class SearchOutcome:
    jobs: list[RawJob] = field(default_factory=list)
    queries: list[QueryStat] = field(default_factory=list)
    planned: int = 0
    skipped_by_budget: int = 0
    pages: int = 0
    aborted: str | None = None
    stop: CrawlStop | None = None
    job_cap_hit: bool = False

    def detail(self) -> dict:
        return {
            "strategy": "search",
            "queries_planned": self.planned,
            "queries_run": len(self.queries),
            "queries_failed": sum(1 for q in self.queries if q.error),
            "queries_saturated": sum(1 for q in self.queries if q.saturated),
            "skipped_by_budget": self.skipped_by_budget,
            "pages": self.pages,
            "aborted": self.aborted,
            "job_cap_hit": self.job_cap_hit,
            "unique_jobs": len(self.jobs),
            "per_query": [
                {
                    "q": q.label,
                    "pages": q.pages,
                    "returned": q.returned,
                    "new": q.new,
                    "total": q.total,
                    "saturated": q.saturated,
                    "error": q.error,
                }
                for q in self.queries[:60]
            ],
        }


class SearchPlanner:
    """Breadth-first, budgeted query plan: base queries first, refinements only for saturated ones."""

    def __init__(
        self,
        caps: ProviderCapabilities,
        *,
        keywords: list[str],
        countries: list[str],
        max_queries: int,
    ):
        assert caps.search is not None
        self.spec = caps.search
        self.max_queries = max(1, max_queries)
        self._seen: set[tuple] = set()
        self._queue: deque[SearchQuery] = deque()
        self.issued = 0
        self.skipped = 0
        kws = [k for k in keywords if k] if "keyword" in self.spec.params else []
        ccs = [c for c in countries if c] if "country" in self.spec.params else []
        for c in ccs or [None]:
            for k in kws or [None]:
                self._push(SearchQuery(keyword=k, country=c))

    def _push(self, q: SearchQuery) -> None:
        k = q.key()
        if k in self._seen:
            return
        self._seen.add(k)
        self._queue.append(q)

    def refine(self, q: SearchQuery) -> int:
        """Split a saturated query by city (only for providers that can filter by city)."""
        if "city" not in self.spec.params or q.city:
            return 0
        cities = REFINE_CITIES.get((q.country or "").upper(), ())
        n = 0
        for city in cities:
            before = len(self._queue)
            self._push(SearchQuery(keyword=q.keyword, country=q.country, region=q.region, city=city))
            n += len(self._queue) - before
        return n

    def __iter__(self):
        return self

    def __next__(self) -> SearchQuery:
        if not self._queue:
            raise StopIteration
        if self.issued >= self.max_queries:
            self.skipped = len(self._queue)
            raise StopIteration
        self.issued += 1
        return self._queue.popleft()

    @property
    def pending(self) -> int:
        return len(self._queue)


async def run_search(
    fetcher: Fetcher,
    adapter: SearchAdapter,
    match: AtsMatch,
    *,
    keywords: list[str],
    countries: list[str],
    limits: SearchLimits,
    already_have: set[str] | None = None,
    parse_json=None,
) -> SearchOutcome:
    """Run the bounded plan; returns every unique posting found (deduplicated by provider job id)."""
    from .adapters import AtsFeedError, _json  # local import: adapters import this module's types

    parse_json = parse_json or _json
    spec = adapter.capabilities.search
    assert spec is not None
    planner = SearchPlanner(
        adapter.capabilities, keywords=keywords, countries=countries, max_queries=limits.max_queries
    )
    out = SearchOutcome()
    have: set[str] = set(already_have or ())
    page_size = spec.page_size(limits.page_size)
    errors_in_a_row = 0
    for raw_q in planner:
        try:
            q = spec.validate(raw_q)
        except SearchParamError as exc:
            out.queries.append(QueryStat(raw_q.label(), error=f"invalid_param: {exc}"))
            continue
        stat = QueryStat(q.label())
        out.queries.append(stat)
        offset = 0
        try:
            for _ in range(limits.max_pages_per_query):
                if offset >= spec.max_window:
                    break
                url = adapter.search_url(match, q, offset, page_size)
                data = parse_json(await fetcher.get(url, kind="feed"))
                sp = adapter.parse_search(match, data)
                out.pages += 1
                stat.pages += 1
                stat.returned += sp.raw_count or len(sp.jobs)
                if sp.total is not None:
                    stat.total = sp.total
                for job in sp.jobs:
                    jid = str(job.ats_job_id)
                    if jid in have:
                        continue
                    have.add(jid)
                    out.jobs.append(job)
                    stat.new += 1
                    if len(out.jobs) >= limits.max_jobs:
                        out.job_cap_hit = True
                        break
                if out.job_cap_hit:
                    break
                offset += page_size
                if (sp.raw_count or len(sp.jobs)) < page_size:
                    break  # short page: this query is exhausted
                if sp.total is not None and offset >= sp.total:
                    break
            reachable = min(spec.max_window, limits.max_pages_per_query * page_size)
            stat.saturated = bool(stat.total is not None and stat.total > reachable)
            if stat.saturated:
                planner.refine(q)
            errors_in_a_row = 0
        except RobotsDisallowed:
            stat.error = "robots_disallowed"
            out.aborted = "robots_disallowed"
            break
        except CrawlStop as stop:  # rate limit / access restriction: keep what we have, stop politely
            stat.error = getattr(stop, "kind", "stop")
            out.aborted = stat.error
            out.stop = stop
            break
        except (AtsFeedError, FetchError) as exc:
            stat.error = getattr(exc, "kind", "ats_feed_error")
            errors_in_a_row += 1
            if errors_in_a_row >= limits.max_consecutive_errors:
                out.aborted = "too_many_errors"
                break
        if out.job_cap_hit:
            out.aborted = "job_cap"
            break
    out.planned = planner.issued + planner.pending
    out.skipped_by_budget = planner.pending if planner.issued >= limits.max_queries else 0
    return out


def to_feed_result(outcome: SearchOutcome, match: AtsMatch) -> FeedResult:
    """A search result is by definition partial: ``complete`` is always False."""
    return FeedResult(
        jobs=outcome.jobs,
        complete=False,
        feed_url=match.feed_url,
        pages=outcome.pages,
        detail=outcome.detail(),
    )
