"""Provider capabilities, search-parameter validation and scan-strategy selection.

Every ATS provider declares *what it can do* (full catalogue endpoint, server-side search, which
search parameters) and *how far that is verified* (``access_status``). The crawler uses this to
choose a strategy per account; the admin diagnostics endpoint publishes it.

Access statuses are deliberately conservative - a status is never upgraded because an adapter
passes its offline fixtures:

``live_validated``     a non-empty live response was validated against the adapter (diagnostics report)
``live_empty_only``    the endpoint answered with a valid but empty board; no real posting was ever parsed
``existing_unreviewed``in production use before the v3 diagnostics; not part of the v3 evidence
``fixture_only``       implemented against documented structures + offline fixtures, never seen live
``blocked``            robots.txt / authentication forbids automated access (evidence in ``access_evidence``)
``test_only``          mock provider used by the test-suite
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ACCESS_STATUSES = (
    "live_validated",
    "live_empty_only",
    "existing_unreviewed",
    "fixture_only",
    "blocked",
    "test_only",
)
LIVE_READY = {"live_validated"}


class SearchParamError(ValueError):
    """A search parameter failed validation; the query is never sent."""


_CTRL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class ParamSpec:
    name: str
    kind: str  # keyword | country | text
    max_len: int = 100

    def validate(self, value: object) -> str:
        if not isinstance(value, str):
            raise SearchParamError(f"{self.name}: expected text")
        v = value.strip()
        if not v:
            raise SearchParamError(f"{self.name}: empty")
        if len(v) > self.max_len:
            raise SearchParamError(f"{self.name}: longer than {self.max_len} characters")
        if _CTRL.search(v):
            raise SearchParamError(f"{self.name}: control characters")
        if self.kind == "country":
            if not re.fullmatch(r"[A-Za-z]{2}", v):
                raise SearchParamError(f"{self.name}: expected an ISO 3166-1 alpha-2 code")
            return v.upper()
        return v


@dataclass(frozen=True)
class SearchSpec:
    """Server-side search offered by a provider."""

    params: dict[str, ParamSpec]  # logical names: keyword, country, region, city
    max_page_size: int = 100
    max_window: int = 10_000  # results retrievable for one query (offset + limit)
    pagination: str = "offset"
    unverified_params: tuple[str, ...] = ()  # accepted by the documented API but never seen live

    def validate(self, q: SearchQuery) -> SearchQuery:
        """Reject unsupported or malformed parameters; returns the cleaned query."""
        clean: dict[str, str | None] = {}
        for name in ("keyword", "country", "region", "city"):
            value = getattr(q, name)
            if value is None:
                clean[name] = None
                continue
            spec = self.params.get(name)
            if spec is None:
                raise SearchParamError(f"provider has no '{name}' search parameter")
            clean[name] = spec.validate(value)
        return SearchQuery(**clean)

    def page_size(self, wanted: int) -> int:
        return max(1, min(int(wanted), self.max_page_size))


@dataclass(frozen=True)
class SearchQuery:
    keyword: str | None = None
    country: str | None = None
    region: str | None = None
    city: str | None = None

    def key(self) -> tuple:
        return (
            (self.keyword or "").lower(),
            (self.country or "").upper(),
            (self.region or "").lower(),
            (self.city or "").lower(),
        )

    def label(self) -> str:
        return " | ".join(f"{k}={v}" for k, v in self.__dict__.items() if v) or "(all)"


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    catalog: bool  # one endpoint lists (or pages through) every public posting
    catalog_paginated: bool
    search: SearchSpec | None
    access_status: str
    access_evidence: str
    notes: str = ""
    server_filters: tuple[str, ...] = ()  # structured filters on the catalogue (not keyword search)
    detail_in_list: bool = True  # the list response carries the job description
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        assert self.access_status in ACCESS_STATUSES, self.access_status

    def as_dict(self) -> dict:
        s = self.search
        return {
            "provider": self.provider,
            "catalog": self.catalog,
            "catalog_paginated": self.catalog_paginated,
            "search": None
            if s is None
            else {
                "params": sorted(s.params),
                "max_page_size": s.max_page_size,
                "max_window": s.max_window,
                "pagination": s.pagination,
                "unverified_params": list(s.unverified_params),
            },
            "server_filters": list(self.server_filters),
            "detail_in_list": self.detail_in_list,
            "access_status": self.access_status,
            "access_evidence": self.access_evidence,
            "live_ready": self.access_status in LIVE_READY,
            "notes": self.notes,
        }


_KW = ParamSpec("keyword", "keyword", 100)
_CC = ParamSpec("country", "country", 2)
_TX = ParamSpec("city", "text", 80)
_RG = ParamSpec("region", "text", 80)

CAPABILITIES: dict[str, ProviderCapabilities] = {
    "personio": ProviderCapabilities(
        "personio",
        catalog=True,
        catalog_paginated=False,
        search=None,
        access_status="existing_unreviewed",
        access_evidence="Used by the crawler before v3; the v3 diagnostics report did not probe Personio.",
        notes="Public XML feed lists every published position.",
    ),
    "greenhouse": ProviderCapabilities(
        "greenhouse",
        catalog=True,
        catalog_paginated=False,
        search=None,
        access_status="live_validated",
        access_evidence=(
            "ATS diagnostics 2026-11-10: N26 (34 postings, meta.total=34) and Bitpanda (45 postings, "
            "meta.total=45) answered HTTP 200 with valid JSON; detail probe valid for both."
        ),
        notes="Job Board API has no keyword search; the board is read in one request (content=true).",
    ),
    "lever": ProviderCapabilities(
        "lever",
        catalog=True,
        catalog_paginated=True,
        search=None,
        server_filters=("location", "team", "department", "commitment"),
        access_status="live_empty_only",
        access_evidence=(
            "ATS diagnostics 2026-11-10: the official example account answered HTTP 200 with an empty list "
            "(2 bytes). No non-empty live Lever board has been validated; run `python -m career_crawler.ats.probe`."
        ),
        notes="Structured filters exist but no keyword search; paged with skip/limit.",
    ),
    "smartrecruiters": ProviderCapabilities(
        "smartrecruiters",
        catalog=True,
        catalog_paginated=True,
        search=SearchSpec(
            params={"keyword": _KW, "country": ParamSpec("country", "country", 2), "region": _RG, "city": _TX},
            max_page_size=100,
            max_window=10_000,
            unverified_params=("keyword", "country", "region", "city"),
        ),
        access_status="blocked",
        access_evidence=(
            "ATS diagnostics 2026-11-10: robots.txt of api.smartrecruiters.com disallows "
            "/v1/companies/<id>/postings for BoschGroup and DeliveryHero (decision EXCLUDE). "
            "Implemented against the documented Posting API and offline fixtures only."
        ),
        notes="Disabled by default. Enabling does not bypass robots.txt: the safe client still refuses.",
        detail_in_list=False,
    ),
    "ashby": ProviderCapabilities(
        "ashby",
        catalog=True,
        catalog_paginated=False,
        search=None,
        access_status="blocked",
        access_evidence=(
            "ATS diagnostics 2026-11-10: robots.txt for the Ashby posting API answered HTTP 401 for deepl "
            "and smallpdf (treated as blocked, decision EXCLUDE). Implemented against the documented "
            "posting-api and offline fixtures only."
        ),
        notes="Disabled by default; the board JSON lists every listed job in one response.",
    ),
}


def capabilities(provider: str) -> ProviderCapabilities | None:
    return CAPABILITIES.get(provider)


def register_capabilities(caps: ProviderCapabilities) -> None:
    CAPABILITIES[caps.provider] = caps


def enabled_providers(setting: str) -> set[str]:
    return {p.strip().lower() for p in (setting or "").split(",") if p.strip()}


# --- strategy selection ---------------------------------------------------------------------


@dataclass(frozen=True)
class Strategy:
    name: str  # catalog | search | none
    reason: str


def select_strategy(caps: ProviderCapabilities, *, search_enabled: bool) -> Strategy:
    """Pick the first strategy for one account.

    A full catalogue is always preferred: it is the only strategy that can say "this posting is
    gone". Search is used when there is no catalogue, or *added* when the catalogue turned out to
    be larger than the configured cap (see :func:`needs_search_supplement`).
    """
    if caps.catalog:
        return Strategy("catalog", "provider offers a full catalogue")
    if caps.search is None:
        return Strategy("none", "provider has neither catalogue nor search")
    if not search_enabled:
        return Strategy("none", "search-only provider and CRAWLER_ATS_SEARCH_ENABLED is off")
    return Strategy("search", "provider has no catalogue; bounded keyword/location search")


def needs_search_supplement(caps: ProviderCapabilities, *, catalog_capped: bool, search_enabled: bool) -> bool:
    return bool(catalog_capped and search_enabled and caps.search is not None)
