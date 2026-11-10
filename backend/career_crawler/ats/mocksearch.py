"""Mock search-only ATS used by the test-suite (never registered in production).

``MockSearchAdapter`` speaks a made-up search API (``q``, ``country``, ``city``, ``offset``, ``limit``)
served by :class:`MockSearchTransport`, an httpx transport that wraps the offline fixture
transport. Requests still run through the real SSRF-safe fetcher (DNS pinning, robots.txt,
throttling, size limits) - only the bytes come from memory. Failure injection makes it possible to
test partial and failed searches end to end without touching a real provider.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx

from ..extract.normalize import RawJob
from ..testing import FixtureTransport
from .base import AtsMatch, FeedResult, valid_account
from .capabilities import (
    ParamSpec,
    ProviderCapabilities,
    SearchQuery,
    SearchSpec,
    register_capabilities,
)
from .searchcrawl import SearchPage

HOST = "search.mock-ats.example.com"

CAPS = ProviderCapabilities(
    "mocksearch",
    catalog=False,
    catalog_paginated=False,
    search=SearchSpec(
        params={
            "keyword": ParamSpec("keyword", "keyword", 60),
            "country": ParamSpec("country", "country", 2),
            "city": ParamSpec("city", "text", 60),
        },
        max_page_size=10,
        max_window=40,
    ),
    access_status="test_only",
    access_evidence="In-memory mock provider for the automated tests.",
)
register_capabilities(CAPS)


class MockSearchAdapter:
    name = "mocksearch"
    capabilities = CAPS

    def detect(self, url: str) -> AtsMatch | None:
        parts = urlsplit(url.strip())
        if parts.hostname != HOST:
            return None
        seg = [x for x in parts.path.split("/") if x]
        acc = seg[1] if seg[:1] == ["api"] and len(seg) > 1 else (seg[0] if seg else None)
        if not valid_account(acc):
            return None
        return AtsMatch("mocksearch", acc, f"https://{HOST}/api/{acc}/search", f"https://{HOST}/{acc}", url[:300])

    async def fetch(self, fetcher, match: AtsMatch) -> FeedResult:  # pragma: no cover - never called
        from .adapters import AtsFeedError

        raise AtsFeedError("mocksearch has no catalogue endpoint")

    def search_url(self, match: AtsMatch, q: SearchQuery, offset: int, limit: int) -> str:
        from urllib.parse import urlencode

        params = {"offset": offset, "limit": limit}
        for k, v in (("q", q.keyword), ("country", q.country), ("city", q.city)):
            if v:
                params[k] = v
        return f"{match.feed_url}?{urlencode(params)}"

    def parse_search(self, match: AtsMatch, data: object) -> SearchPage:
        from .adapters import AtsFeedError

        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise AtsFeedError("unexpected mock response")
        jobs = []
        for it in data["items"]:
            if not isinstance(it, dict) or not it.get("id") or not it.get("title"):
                continue
            jobs.append(
                RawJob(
                    title=it["title"],
                    source_url=f"{match.board_url}/{it['id']}",
                    method="ats",
                    career_page_url=match.board_url,
                    location_raw=", ".join(x for x in (it.get("city"), it.get("country")) if x) or None,
                    locations=[{k: it[k] for k in ("city", "country") if it.get(k)}],
                    remote_raw="remote" if it.get("remote") else None,
                    description_html=it.get("description"),
                    ats_type="mocksearch",
                    ats_account=match.account,
                    ats_job_id=str(it["id"]),
                    confidence=0.9,
                )
            )
        total = data.get("total")
        return SearchPage(jobs, total if isinstance(total, int) else None, raw_count=len(data["items"]))


class MockSearchTransport(FixtureTransport):
    """Fixture transport + a dynamic search API for ``HOST``.

    ``boards`` maps account -> list of job dicts (id, title, city, country, remote, description).
    ``fail_when(url)`` -> status code (e.g. 500/429) lets a test break specific queries.
    """

    def __init__(self, root, boards: dict[str, list[dict]], *, fail_when=None, **kw):
        super().__init__(root, **kw)
        self.boards = boards
        self.fail_when = fail_when
        self.api_calls: list[str] = []

    def hosts(self) -> list[str]:
        return sorted({*super().hosts(), HOST})

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "").split(":")[0].lower()
        if host != HOST:
            return await super().handle_async_request(request)
        parts = urlsplit(str(request.url))
        self.requests.append(f"{host}{parts.path}" + (f"?{parts.query}" if parts.query else ""))
        if parts.path == "/robots.txt":
            return httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"User-agent: *\nAllow: /\n", request=request
            )
        seg = [x for x in parts.path.split("/") if x]
        if len(seg) == 3 and seg[0] == "api" and seg[2] == "search" and seg[1] in self.boards:
            self.api_calls.append(str(request.url))
            if self.fail_when and (code := self.fail_when(str(request.url))):
                headers = {"retry-after": "120"} if code == 429 else {}
                return httpx.Response(code, headers=headers, content=b"error", request=request)
            qs = {k: v[0] for k, v in parse_qs(parts.query).items()}
            items = self._filter(self.boards[seg[1]], qs)
            offset, limit = int(qs.get("offset", 0)), min(int(qs.get("limit", 10)), 10)
            body = {"total": len(items), "items": items[offset : offset + limit]}
            return httpx.Response(
                200, headers={"content-type": "application/json"}, content=json.dumps(body).encode(), request=request
            )
        return httpx.Response(404, headers={"content-type": "text/html"}, content=b"nope", request=request)

    @staticmethod
    def _filter(jobs: list[dict], qs: dict) -> list[dict]:
        kw = (qs.get("q") or "").lower()
        out = []
        for j in jobs:
            if kw and kw not in f"{j.get('title', '')} {j.get('description', '')}".lower():
                continue
            if qs.get("country") and (j.get("country") or "").upper() != qs["country"].upper():
                continue
            if qs.get("city") and (j.get("city") or "").lower() != qs["city"].lower():
                continue
            out.append(j)
        return out
