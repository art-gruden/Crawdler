"""Adaptive per-company crawl.

Strategy ladder (cheapest and most reliable first):

1. ``ats``      – public ATS feed (Personio XML, Greenhouse, Lever) when an account is
                  evidenced on the site or remembered in the site fingerprint;
2. ``jsonld``   – schema.org JobPosting in the fetched HTML;
3. ``html``     – static HTML: job lists, pagination, job detail pages (selectolax);
4. ``content``  – Trafilatura main-content extraction for detail pages with poor markup;
   ``pdf``      – job descriptions published as PDF;
5. ``browser``  – only for career pages that are JS shells; handed to the browser worker;
6. ``llm``      – optional, separately configured fallback (extract queue).

The engine is I/O-only (HTTP via :class:`Fetcher`) and returns a :class:`ScanResult`;
persistence lives in :mod:`career_crawler.store`. A checkpoint callback receives the
state every few pages so a crashed worker can resume.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from gcc_core.netsafe import FetchError
from gcc_core.urls import domain_key

from . import ats
from .ats import capabilities as caps_mod
from .ats import searchcrawl
from .config import CrawlerSettings
from .discovery import Frontier, career_urls_from_sitemap, parse_sitemap, rank_sitemap_children
from .extract import html as h
from .extract.jsonld import extract_jsonld
from .extract.normalize import Job, RawJob, clean_text, normalize
from .extract.pdf import extract_pdf
from .fetch import AccessRestricted, CrawlStop, Fetcher, Page, RobotsDisallowed
from .urls import canonicalize, host_of, link_score, same_site

STRATEGY_ORDER = ("ats", "jsonld", "html", "content", "pdf", "browser", "llm")
EXTERNAL_JOB_HOSTS = re.compile(
    r"(softgarden\.io|dvinci(-easy)?\.(com|de)|rexx-systems\.com|onlyfy\.jobs|prescreen\.io|b-ite\.(com|de)|"
    r"concludis\.de|recruitee\.com|smartrecruiters\.com|myworkdayjobs\.com|successfactors\.(eu|com)|jobs\.sap\.com|"
    r"join\.com|kenjo\.io|jobbase\.io|umantis\.com|heyrecruit\.de|coveto\.de|perbility\.de|teamtailor\.com|"
    r"workable\.com|bamboohr\.com|personio\.(de|com)|greenhouse\.io|lever\.co|jobs\.ashbyhq\.com|"
    r"career\.[a-z0-9-]+\.(de|com)|karriere\.[a-z0-9-]+\.(de|com))$",
    re.I,
)
COMMON_CAREER_PATHS = ("/karriere", "/jobs", "/karriere/stellenangebote", "/career", "/careers")


@dataclass
class CompanyInfo:
    id: int
    name: str
    domain: str
    website_url: str


@dataclass
class KnownPage:
    page_type: str | None
    content_hash: str | None
    etag: str | None
    last_modified: str | None


@dataclass
class FingerprintView:
    best_strategy: str | None = None
    ats_type: str | None = None
    ats_account: str | None = None
    career_urls: list[str] = field(default_factory=list)
    needs_browser: bool = False
    full_discovery_due: bool = True


@dataclass
class FoundJob:
    job: Job
    source_url: str  # career source this posting belongs to (feed URL or list/landing page)
    source_kind: str


@dataclass
class SourceFound:
    url: str
    kind: str
    ats_type: str | None = None
    ats_account: str | None = None
    evidence: dict = field(default_factory=dict)
    job_count: int = 0
    ok: bool = True
    complete: bool = False
    error: str | None = None
    # ATS scan diagnostics (persisted on career_sources)
    strategy: str | None = None  # catalog | search | catalog+search | disabled
    reported_total: int | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class PageRecord:
    url: str
    page_type: str | None
    status: int | None
    content_type: str | None
    content_hash: str | None
    etag: str | None
    last_modified: str | None
    strategy: str | None
    jobs_found: int
    fetch_ms: int | None
    bytes: int | None
    changed: bool
    raw: bytes | None = None


@dataclass
class ErrorRecord:
    stage: str
    kind: str
    url: str | None
    message: str
    http_status: int | None = None
    strategy: str | None = None


@dataclass
class ScanResult:
    jobs: list[FoundJob] = field(default_factory=list)
    sources: dict[str, SourceFound] = field(default_factory=dict)
    pages: list[PageRecord] = field(default_factory=list)
    unchanged_job_urls: list[str] = field(default_factory=list)
    errors: list[ErrorRecord] = field(default_factory=list)
    strategy_stats: dict[str, dict] = field(default_factory=dict)
    browser_urls: list[str] = field(default_factory=list)
    llm_candidates: list[str] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)
    complete: bool = False
    outcome: str = "unknown"
    stop: CrawlStop | None = None
    pages_fetched: int = 0
    pages_unchanged: int = 0
    elapsed_s: float = 0.0
    career_found: bool = False
    explicit_no_vacancies: bool = False

    def note(self, strategy: str, *, ok: bool, jobs: int = 0) -> None:
        s = self.strategy_stats.setdefault(strategy, {"attempts": 0, "ok": 0, "jobs": 0})
        s["attempts"] += 1
        s["ok"] += int(ok)
        s["jobs"] += jobs

    @property
    def best_strategy(self) -> str | None:
        best = None
        for name in STRATEGY_ORDER:
            st = self.strategy_stats.get(name)
            if st and st["jobs"] > 0 and (best is None or st["jobs"] > self.strategy_stats[best]["jobs"]):
                best = name
        return best


CheckpointFn = Callable[[dict], Awaitable[None]]


def text_hash(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", text).strip().encode("utf-8")).hexdigest()


class SiteScan:
    def __init__(
        self,
        fetcher: Fetcher,
        settings: CrawlerSettings,
        company: CompanyInfo,
        *,
        fingerprint: FingerprintView | None = None,
        known_pages: dict[str, KnownPage] | None = None,
        resume: dict | None = None,
        checkpoint: CheckpointFn | None = None,
        clock: Callable[[], float] = time.monotonic,
        seeds: list[str] | None = None,
        rendered: bool = False,
        max_pages: int | None = None,
    ):
        self.f = fetcher
        self.s = settings
        self.c = company
        self.fp = fingerprint or FingerprintView()
        self.known = known_pages or {}
        self.checkpoint = checkpoint
        self.clock = clock
        self.r = ScanResult()
        self.frontier = Frontier(settings.frontier_max)
        self.ats_done: set[tuple[str, str]] = set()
        self.ats_skipped: set[tuple[str, str]] = set()  # detected but not fetched (provider disabled / no strategy)
        self.external_hosts: set[str] = set()
        self.list_entries: dict[str, tuple[RawJob, str]] = {}  # job url -> (raw, list page)
        self.detail_done: set[str] = set()
        self.relevant_pages = 0
        self.fetch_errors_in_row = 0
        self.access_denied = 0
        self.pdfs = 0
        self.budget_hit = False
        self.early_stopped = False
        self._resume = resume or {}
        self._seen_job_keys: set[str] = set()
        self.seeds = seeds
        self.rendered = rendered  # pages come from the browser: postings are recorded as method "browser"
        self.max_pages = max_pages or settings.max_pages_per_company

    # --- public -------------------------------------------------------------------------

    async def run(self) -> ScanResult:
        t0 = self.clock()
        try:
            await self._run()
        except CrawlStop as stop:
            self.r.stop = stop
            self.r.errors.append(ErrorRecord("fetch", stop.kind, stop.url, str(stop), stop.status))
            self.r.outcome = stop.kind
        finally:
            self._flush_list_entries()
            self.r.elapsed_s = round(self.clock() - t0, 3)
        if self.r.stop is None:
            self.r.outcome = self._outcome()
        return self.r

    # --- orchestration ------------------------------------------------------------------

    async def _run(self) -> None:
        if self.seeds is not None:  # browser pass (or targeted rescan): only these pages and their postings
            for u in self.seeds:
                cu = canonicalize(u)
                if cu:
                    self.frontier.push(cu, 10.0, 1, reason="seed")
            await self._crawl(discover=False)
            return
        # 1) remembered ATS feed: cheapest and complete
        if self.fp.ats_type and self.fp.ats_account and not self.fp.full_discovery_due:
            match = self._remembered_match()
            if match and await self._run_feed(match, evidence={"from": "fingerprint"}):
                self.r.complete = all(s.complete for s in self.r.sources.values())
                return
        # 2) remembered browser-only site
        if (
            self.fp.needs_browser
            and self.fp.best_strategy == "browser"
            and self.fp.career_urls
            and not self.fp.full_discovery_due
        ):
            self.r.browser_urls = self.fp.career_urls[: self.s.browser_max_urls_per_company]
            self.r.career_found = True
            return
        # 3) crawl
        for u in self.fp.career_urls[:10]:
            cu = canonicalize(u)
            if cu:
                self.frontier.push(cu, 10.0, 1, reason="remembered")
        home = canonicalize(self.c.website_url) or f"https://{self.c.domain}/"
        self.frontier.push(home, 5.0, 0, reason="homepage")
        for u in self._resume.get("visited", []):
            self.frontier.mark_seen(u)
        await self._crawl()

    def _remembered_match(self):
        board = ats.board_url(self.fp.ats_type, self.fp.ats_account)
        if not board:
            return None
        for u in self.fp.career_urls:  # keep the original host variant (.com, EU region)
            m = ats.detect_url(u)
            if m and m.ats_type == self.fp.ats_type and m.account.lower() == (self.fp.ats_account or "").lower():
                return m
        return ats.detect_url(board)

    async def _crawl(self, *, discover: bool = True) -> None:
        sitemaps_done = guessed = not discover
        deadline = self.clock() + self.s.scan_time_budget_s
        while True:
            if self.r.pages_fetched >= self.max_pages:
                self.budget_hit = len(self.frontier) > 0
                break
            if self.clock() > deadline:
                self.budget_hit = True
                self.r.errors.append(ErrorRecord("discovery", "time_budget", None, "scan time budget exhausted"))
                break
            if self._should_stop_early():
                self.early_stopped = True
                break
            item = self.frontier.pop()
            if item is None:
                if not sitemaps_done and not self.r.career_found:
                    sitemaps_done = True
                    await self._sitemaps()
                    continue
                if not guessed and not self.r.career_found:
                    guessed = True
                    for p in COMMON_CAREER_PATHS:
                        u = canonicalize(p, f"https://{host_of(self.c.website_url) or self.c.domain}/")
                        if u:
                            self.frontier.push(u, 2.0, 1, reason="common_path")
                    continue
                break
            await self._visit(item)
            if self.checkpoint and self.r.pages_fetched and self.r.pages_fetched % self.s.checkpoint_every_pages == 0:
                await self.checkpoint(self._state())
        drained = len(self.frontier) == 0 or (self.frontier.peek_priority() or 0) < 1.0
        html_complete = (
            drained
            and not self.budget_hit
            and not self.early_stopped
            and not any(e.stage == "fetch" and e.kind not in ("robots_disallowed", "http_404") for e in self.r.errors)
        )
        feeds_complete = all(s.complete for s in self.r.sources.values() if s.kind == "ats_feed")
        self.r.complete = html_complete and feeds_complete

    def _should_stop_early(self) -> bool:
        n = self.r.pages_fetched
        if n < self.s.early_stop_after or self.r.jobs:
            return False
        ratio = self.relevant_pages / max(1, n)
        return ratio < self.s.early_stop_min_ratio

    def _state(self) -> dict:
        return {
            "visited": sorted({p.url for p in self.r.pages})[-500:],
            "frontier": self.frontier.snapshot(50),
            "pages": self.r.pages_fetched,
            "jobs": len(self.r.jobs),
        }

    # --- one URL --------------------------------------------------------------------------

    async def _visit(self, item) -> None:
        url = item.url
        is_seed = item.reason in ("homepage", "remembered", "seed")
        known = self.known.get(url)
        kind = "pdf" if urlsplit(url).path.lower().endswith(".pdf") else "html"
        if kind == "pdf":
            if self.pdfs >= self.s.max_pdfs_per_company:
                return
            self.pdfs += 1
        conditional = known is not None and known.page_type == "job_detail"
        try:
            page = await self.f.get(
                url,
                kind=kind,
                etag=known.etag if conditional else None,
                last_modified=known.last_modified if conditional else None,
            )
        except RobotsDisallowed as exc:
            self.r.errors.append(ErrorRecord("robots", "robots_disallowed", url, str(exc)))
            if item.reason == "homepage":
                raise CrawlStop("homepage disallowed by robots.txt", url) from exc
            return
        except AccessRestricted as exc:
            self.access_denied += 1
            if is_seed or self.access_denied >= 3:
                raise
            self.r.errors.append(ErrorRecord("fetch", exc.kind, url, str(exc), exc.status))
            return
        except FetchError as exc:
            self.fetch_errors_in_row += 1
            self.r.errors.append(ErrorRecord("fetch", exc.kind, url, str(exc)[:300]))
            if self.fetch_errors_in_row >= 3 or item.reason == "homepage":
                raise CrawlStop(f"site unreachable: {exc.kind}", url) from exc
            return
        self.fetch_errors_in_row = 0
        self.r.pages_fetched += 1
        final = canonicalize(page.final_url) or url
        for hop in page.redirects:
            self.frontier.mark_seen(canonicalize(hop) or hop)
        self.frontier.mark_seen(final)
        # A redirect may land on an ATS board: that is evidence.
        for u in [page.final_url, *page.redirects]:
            m = ats.detect_url(u)
            if m:
                await self._run_feed(m, evidence={"redirect": u, "from_page": url})

        if page.status == 304:
            self.r.pages_unchanged += 1
            self.r.unchanged_job_urls.append(url)
            self._record(
                url,
                page,
                known.page_type if known else None,
                known.content_hash if known else None,
                None,
                0,
                changed=False,
            )
            return
        if page.status >= 400:
            kind_code = f"http_{page.status}"
            self.r.errors.append(ErrorRecord("fetch", kind_code, url, f"HTTP {page.status}", page.status))
            self._record(url, page, "error", None, None, 0, changed=False)
            return
        if page.is_pdf:
            await self._handle_pdf(page, item)
            return
        if not page.is_html:
            self._record(url, page, "other", None, None, 0, changed=False)
            return
        await self._handle_html(page, final, item, known)

    async def _handle_pdf(self, page: Page, item) -> None:
        raw = extract_pdf(page.body, page.final_url, career_page=item.parent, anchor=item.anchor)
        n = self._add(raw, item.parent or page.final_url, "career_page") if raw else 0
        self.r.note("pdf", ok=n > 0, jobs=n)
        self._record(item.url, page, "pdf_job" if n else "pdf", None, "pdf", n, changed=True)

    async def _handle_html(self, page: Page, url: str, item, known: KnownPage | None) -> None:
        markup = page.text()
        tree = h.parse(markup)
        visible = clean_text(tree.body.text(separator="\n")) if tree.body is not None else ""
        chash = text_hash(visible)
        changed = known is None or known.content_hash != chash
        all_links = h.links(tree, url)
        srcs, inline = h.script_sources(tree)
        # ATS evidence on this page
        matches = ats.detect([ln.url for ln in all_links] + [canonicalize(s, url) or s for s in srcs], inline)
        for m in matches:
            await self._run_feed(m, evidence={"page": url})
        # Unchanged job detail page: no re-extraction, just "still there"
        if not changed and known is not None and known.page_type == "job_detail":
            self.r.pages_unchanged += 1
            self.r.unchanged_job_urls.append(url)
            self.relevant_pages += 1
            self._record(item.url, page, "job_detail", chash, None, 0, changed=False)
            return
        ld_jobs, has_ld = extract_jsonld(tree, url)
        sig = h.classify(tree, url, all_links, has_jobposting=bool(ld_jobs))
        self.r.technologies = sorted(set(self.r.technologies) | set(sig.technologies))
        strategy, n = None, 0
        if has_ld:
            for raw in ld_jobs:
                raw.career_page_url = raw.career_page_url or item.parent
                n += self._add(raw, item.parent or url, "career_page")
            self.r.note("jsonld", ok=n > 0, jobs=n)
            strategy = "jsonld" if n else None
            if n:
                self.detail_done.add(url)
        if sig.page_type in ("job_list", "career_landing", "no_vacancies", "js_shell", "job_detail"):
            self.relevant_pages += 1
        if sig.page_type in ("job_list", "career_landing", "no_vacancies"):
            self.r.career_found = True
            src = self.r.sources.setdefault(
                url,
                SourceFound(
                    url,
                    "job_list" if sig.page_type == "job_list" else "career_page",
                    evidence={"title": sig.title[:120]},
                ),
            )
            if sig.page_type == "no_vacancies":
                self.r.explicit_no_vacancies = True
                src.complete = True
        if sig.page_type == "job_list":
            ats_hosts = {h_ for (t, a) in self.ats_done for h_ in (t,)}
            entries = h.list_entries(tree, url, sig.job_links, url)
            for raw in entries:
                if raw.source_url not in self.detail_done:
                    self.list_entries.setdefault(raw.source_url, (raw, url))
            for ln in sig.job_links:
                if ats_hosts and ats.ATS_HOSTS.search(host_of(ln.url)):
                    continue  # covered by the feed
                self._enqueue(ln.url, 8.0, item.depth + 1, "job_link", ln.anchor, url)
            for p in sig.pagination[:10]:
                if same_site(p, self.c.domain) or host_of(p) == host_of(url):
                    self._enqueue(p, 7.5, item.depth, "pagination", "", item.parent or url)
        elif sig.page_type == "job_detail" and not ld_jobs:
            raw = h.extract_detail(tree, url, markup, career_page=item.parent)
            if raw is not None:
                prev = self.list_entries.pop(url, None)
                if prev and not raw.location_raw:
                    raw.location_raw = prev[0].location_raw
                k = self._add(raw, item.parent or url, "career_page")
                n += k
                self.detail_done.add(url)
                self.r.note(raw.method, ok=k > 0, jobs=k)
                strategy = raw.method
            else:
                self.r.note("html", ok=False)
        elif sig.page_type == "js_shell" and self.rendered:
            self.r.errors.append(
                ErrorRecord("browser", "render_empty", url, "page still empty after rendering", strategy="browser")
            )
        elif sig.page_type == "js_shell" and (item.priority >= 3.0 or link_score(url, sig.title) >= 3.0):
            if url not in self.r.browser_urls and len(self.r.browser_urls) < self.s.browser_max_urls_per_company:
                self.r.browser_urls.append(url)
                self.r.career_found = True
        elif sig.page_type == "career_landing" and not sig.job_links and not matches and not has_ld:
            if self.s.llm_enabled and len(visible) > 400:
                self.r.llm_candidates.append(url)
        # links for further discovery
        if sig.page_type != "job_detail" or item.depth == 0:
            for ln in all_links:
                if ln.tag == "iframe":
                    continue
                score = link_score(ln.url, ln.anchor, ln.context)
                if score <= 0:
                    continue
                self._enqueue(ln.url, score, item.depth + 1, "link", ln.anchor, url)
        page_type = sig.page_type if not (n and sig.page_type not in ("job_list", "job_detail")) else "job_detail"
        self._record(
            item.url,
            page,
            page_type,
            chash,
            strategy,
            n,
            changed=changed,
            raw=page.body if self.s.keep_raw_html else None,
        )

    # --- helpers ---------------------------------------------------------------------------

    def _enqueue(self, url: str, priority: float, depth: int, reason: str, anchor: str, parent: str | None) -> None:
        if depth > self.s.max_depth:
            return
        host = host_of(url)
        if not same_site(url, self.c.domain):
            if not EXTERNAL_JOB_HOSTS.search(host) or ats.detect_url(url):
                return  # unrelated site, or an ATS already handled through its feed
            if host not in self.external_hosts:
                if len(self.external_hosts) >= self.s.max_external_ats_hops:
                    return
                self.external_hosts.add(host)
        self.frontier.push(url, priority, depth, reason=reason, anchor=anchor, parent=parent)

    async def _run_feed(self, match, *, evidence: dict) -> bool:
        key = (match.ats_type, match.account.lower())
        if key in self.ats_done or key in self.ats_skipped:
            return False
        adapter = ats.adapter(match.ats_type)
        caps = caps_mod.capabilities(match.ats_type)
        if adapter is None or caps is None:
            return False
        src_evidence = {
            **evidence,
            "evidence_url": match.evidence,
            "board": match.board_url,
            "access_status": caps.access_status,
        }
        # Provider gate: detected accounts of a disabled provider are recorded, never fetched.
        if match.ats_type not in caps_mod.enabled_providers(self.s.ats_enabled_providers):
            self.ats_skipped.add(key)
            self.r.sources.setdefault(
                match.feed_url,
                SourceFound(
                    match.feed_url,
                    "ats_feed",
                    match.ats_type,
                    match.account,
                    evidence=src_evidence,
                    ok=False,
                    complete=False,
                    error="provider_disabled",
                    strategy="disabled",
                ),
            )
            return False
        strategy = caps_mod.select_strategy(caps, search_enabled=self.s.ats_search_enabled)
        if strategy.name == "none":
            self.ats_skipped.add(key)
            self.r.sources.setdefault(
                match.feed_url,
                SourceFound(
                    match.feed_url,
                    "ats_feed",
                    match.ats_type,
                    match.account,
                    evidence=src_evidence,
                    ok=False,
                    complete=False,
                    error="search_disabled",
                    strategy="disabled",
                    detail={"reason": strategy.reason},
                ),
            )
            return False
        if len(self.ats_done) >= self.s.max_external_ats_hops + 1:
            return False
        self.ats_done.add(key)
        src = self.r.sources.setdefault(
            match.feed_url,
            SourceFound(match.feed_url, "ats_feed", match.ats_type, match.account, evidence=src_evidence),
        )
        configure = getattr(adapter, "configure", None)
        if configure:
            configure(self.s)
        stop: CrawlStop | None = None
        try:
            if strategy.name == "catalog":
                res = await adapter.fetch(self.f, match)
                res.detail.setdefault("strategy", "catalog")
                if caps_mod.needs_search_supplement(
                    caps, catalog_capped=bool(res.detail.get("capped")), search_enabled=self.s.ats_search_enabled
                ) and hasattr(adapter, "search_url"):
                    outcome = await self._search(adapter, match, {str(j.ats_job_id) for j in res.jobs})
                    res.jobs = [*res.jobs, *outcome.jobs]
                    res.complete = False  # a capped catalogue plus a partial search is never complete
                    res.detail = {**res.detail, "strategy": "catalog+search", "search": outcome.detail()}
                    stop = outcome.stop
            else:
                outcome = await self._search(adapter, match, set())
                res = searchcrawl.to_feed_result(outcome, match)
                stop = outcome.stop
                if not any(not q.error for q in outcome.queries):  # nothing answered at all
                    first = next((q.error for q in outcome.queries if q.error), None) or "no_queries"
                    src.ok, src.error, src.strategy, src.detail = False, first, "search", res.detail
                    self.r.errors.append(
                        ErrorRecord("ats", first, match.feed_url, "search returned no usable answer", strategy="ats")
                    )
                    self.r.note("ats", ok=False)
                    if stop:
                        raise stop
                    return False
        except RobotsDisallowed as exc:
            src.ok, src.error = False, "robots_disallowed"
            src.strategy = strategy.name
            self.r.errors.append(ErrorRecord("ats", "robots_disallowed", match.feed_url, str(exc), strategy="ats"))
            self.r.note("ats", ok=False)
            return False
        except (ats.AtsFeedError, FetchError) as exc:
            src.ok, src.error = False, getattr(exc, "kind", "ats_feed_error")
            src.strategy = strategy.name
            self.r.errors.append(ErrorRecord("ats", src.error, match.feed_url, str(exc)[:300], strategy="ats"))
            self.r.note("ats", ok=False)
            return False
        n = 0
        self.r.pages_fetched += max(1, res.pages)
        for raw in res.jobs:
            n += self._add(raw, match.feed_url, "ats_feed")
        src.job_count, src.complete = n, res.complete
        src.strategy = str(res.detail.get("strategy") or strategy.name)
        src.reported_total, src.detail = res.reported_total, res.detail
        if res.detail.get("aborted"):
            src.error = f"search_{res.detail['aborted']}"  # partial but usable: ok stays True
        self.r.career_found = True
        self.r.note("ats", ok=True, jobs=n)
        if stop:
            raise stop  # rate limit / access restriction after a partial search: keep what was found
        return True

    async def _search(self, adapter, match, already_have: set[str]):
        return await searchcrawl.run_search(
            self.f,
            adapter,
            match,
            keywords=[k.strip() for k in self.s.ats_search_keywords.split(",") if k.strip()],
            countries=[c.strip().upper() for c in self.s.ats_search_countries.split(",") if c.strip()],
            limits=searchcrawl.SearchLimits(
                max_queries=self.s.ats_search_max_queries,
                max_pages_per_query=self.s.ats_search_max_pages,
                page_size=self.s.ats_search_page_size,
                max_jobs=self.s.ats_search_max_jobs,
            ),
            already_have=already_have,
        )

    def _add(self, raw: RawJob, source_url: str, source_kind: str) -> int:
        job = normalize(raw, max_chars=self.s.description_max_chars)
        if job is None:
            return 0
        if self.rendered and job.method != "ats":
            job.source_values["extractor"] = job.method
            job.method = "browser"
        key = (
            f"ats:{job.ats_type}:{job.ats_account}:{job.ats_job_id}"
            if job.ats_type and job.ats_job_id
            else f"url:{job.source_url}:{job.title_norm}"
        )
        if key in self._seen_job_keys:
            return 0
        self._seen_job_keys.add(key)
        self.r.jobs.append(FoundJob(job, source_url, source_kind))
        if source_url in self.r.sources and source_kind != "ats_feed":
            self.r.sources[source_url].job_count += 1
        return 1

    def _flush_list_entries(self) -> None:
        """Postings seen on list pages whose detail page was not read (budget/errors)."""
        n = 0
        for url, (raw, list_page) in list(self.list_entries.items()):
            if url in self.detail_done:
                continue
            n += self._add(raw, list_page, "career_page")
        if n:
            self.r.note("html", ok=True, jobs=n)
        self.list_entries.clear()

    def _record(self, url, page: Page, page_type, chash, strategy, jobs_found, *, changed: bool, raw=None) -> None:
        self.r.pages.append(
            PageRecord(
                url=url,
                page_type=page_type,
                status=page.status,
                content_type=page.content_type[:80] or None,
                content_hash=chash,
                etag=page.headers.get("etag"),
                last_modified=page.headers.get("last-modified"),
                strategy=strategy,
                jobs_found=jobs_found,
                fetch_ms=page.elapsed_ms,
                bytes=len(page.body),
                changed=changed,
                raw=raw,
            )
        )

    async def _sitemaps(self) -> None:
        home = f"https://{host_of(self.c.website_url) or self.c.domain}/"
        try:
            listed = await self.f.robots.sitemaps(home)
        except Exception:
            listed = []
        queue = listed[:5] or [urlunsplit(("https", host_of(home), "/sitemap.xml", "", ""))]
        files = 0
        found: list[tuple[str, float]] = []
        while queue and files < self.s.max_sitemap_files:
            sm = queue.pop(0)
            if not same_site(sm, self.c.domain):
                continue
            files += 1
            try:
                page = await self.f.get(sm, kind="sitemap")
            except (FetchError, RobotsDisallowed, AccessRestricted) as exc:
                self.r.errors.append(
                    ErrorRecord("discovery", getattr(exc, "kind", "sitemap_error"), sm, str(exc)[:200])
                )
                continue
            self.r.pages_fetched += 1
            if page.status != 200:
                continue
            children, urls = parse_sitemap(page.body, max_urls=self.s.max_sitemap_urls)
            queue.extend(rank_sitemap_children(children))
            found.extend(career_urls_from_sitemap([u for u in urls if same_site(u, self.c.domain)]))
        for u, score in sorted(found, key=lambda x: -x[1])[:30]:
            cu = canonicalize(u)
            if cu:
                self.frontier.push(cu, score, 1, reason="sitemap")

    def _outcome(self) -> str:
        if self.r.jobs:
            return "jobs_found"
        if self.r.browser_urls:
            return "needs_browser"
        if self.r.explicit_no_vacancies:
            return "no_vacancies"
        if self.r.career_found:
            return "career_page_no_jobs"
        return "no_career_page"


def strategy_counts(jobs: list[FoundJob]) -> dict[str, int]:
    return dict(Counter(j.job.method for j in jobs))


def registrable(url: str) -> str:
    return domain_key(host_of(url)) or host_of(url)
