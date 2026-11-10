"""JavaScript rendering fallback (Playwright/Chromium), used only for career pages that are
JS shells. One browser, one context, one page at a time.

Network isolation: Chromium never talks to the network itself.

* Every request of the context is intercepted (``context.route("**/*")``) and answered
  with ``route.fulfill`` from :class:`~career_crawler.fetch.Fetcher` — so DNS checks,
  public-IP enforcement, IP pinning (DNS-rebinding protection), redirect re-validation and
  size limits apply to the document *and* to every subresource and XHR.
* Main-frame navigations additionally pass robots.txt and the per-domain throttle;
  CAPTCHA/login walls stop the scan (never solved or bypassed).
* Chromium is started with a black-hole proxy, so anything that would escape interception
  (prefetch, DNS prefetch, a future API) fails closed. WebSockets are closed, service
  workers blocked, downloads refused, popups closed, non-GET requests aborted, images,
  media and fonts skipped, and the number of requests per page is capped.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import select, update

from gcc_core.db import session_scope
from gcc_core.log import get_logger

from .config import CrawlerSettings, get_crawler_settings
from .engine import SiteScan
from .fetch import CrawlStop, Fetcher, Page, RobotsDisallowed, RobotsUnavailable
from .models import CrawlRun, ExtractionError
from .resources import ResourceController
from .store import load_context, persist

log = get_logger(__name__)

DATA_TYPES = frozenset({"xhr", "fetch"})

CHROMIUM_ARGS = [
    "--proxy-server=http://127.0.0.1:9",  # fail closed: nothing may bypass request interception
    "--proxy-bypass-list=<-loopback>",
    "--dns-prefetch-disable",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-domain-reliability",
    "--disable-sync",
    "--disable-extensions",
    "--disable-features=NetworkPrediction,InterestFeedContentSuggestions,MediaRouter,OptimizationHints",
    "--no-pings",
    "--no-first-run",
    "--mute-audio",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--js-flags=--max-old-space-size=256",
]
BLOCKED_TYPES = {"image", "media", "font", "manifest", "texttrack", "eventsource", "websocket", "ping", "other"}
PASS_HEADERS = {"content-type", "cache-control", "expires", "last-modified", "etag", "content-language"}


class RenderError(Exception):
    kind = "render_error"


class BrowserRenderer:
    def __init__(self, settings: CrawlerSettings, fetcher: Fetcher, *, on_page=None):
        self.s = settings
        self.f = fetcher
        self.on_page = on_page
        self._pw = None
        self._browser = None
        self._context = None
        self.active_pages = 0
        self.crashed = False

    async def __aenter__(self) -> BrowserRenderer:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=CHROMIUM_ARGS,
            executable_path=self.s.browser_executable or None,
            chromium_sandbox=False,
        )
        self._browser.on("disconnected", lambda *_: setattr(self, "crashed", True))
        self._context = await self._browser.new_context(
            service_workers="block",
            accept_downloads=False,
            java_script_enabled=True,
            locale="de-DE",
            user_agent=self.f.user_agent,
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=False,
        )
        self._context.on("page", self._on_new_page)
        if hasattr(self._context, "route_web_socket"):
            await self._context.route_web_socket("**/*", lambda ws: asyncio.ensure_future(ws.close()))
        self.crashed = False

    async def close(self) -> None:
        for obj in (self._context, self._browser):
            try:
                if obj is not None:
                    await obj.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._pw = self._browser = self._context = None

    async def restart(self) -> None:
        await self.close()
        await self.start()

    def _on_new_page(self, page) -> None:
        if getattr(self, "_main_page", None) is not None and page is not self._main_page:
            asyncio.ensure_future(page.close())  # popups / window.open

    async def render(self, url: str) -> Page:
        if self.crashed or self._context is None:
            await self.restart()
        state = {"requests": 0, "stop": None, "main": None, "cache": {}}
        page = await self._context.new_page()
        self._main_page = page
        self.active_pages += 1

        async def handle(route, request):
            try:
                await self._handle(route, request, page, state)
            except Exception as exc:  # never let a handler error hang the page
                state.setdefault("handler_errors", []).append(repr(exc)[:200])
                try:
                    await route.abort("failed")
                except Exception:
                    pass

        await page.route("**/*", handle)
        try:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.s.browser_page_timeout_s * 1000)
            except Exception as exc:
                if state["stop"] is not None:
                    raise state["stop"] from None
                raise RenderError(f"navigation failed: {str(exc)[:200]}") from exc
            if state["stop"] is not None:
                raise state["stop"]
            try:
                await page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass  # long-polling pages never go idle; take what is rendered
            html = await page.content()
            main: Page | None = state["main"]
            if self.on_page:
                self.on_page()
            body = html.encode("utf-8")[: self.s.max_html_bytes]
            return Page(
                url=url,
                final_url=page.url if page.url.startswith("http") else url,
                status=main.status if main else 200,
                content_type="text/html",
                body=body,
                truncated=len(html) > self.s.max_html_bytes,
                elapsed_ms=main.elapsed_ms if main else 0,
                headers=main.headers if main else _empty_headers(),
            )
        finally:
            self.active_pages -= 1
            self._main_page = None
            try:
                await page.close()
            except Exception:
                pass

    async def _handle(self, route, request, page, state) -> None:
        url = request.url
        if not url.startswith(("http://", "https://")):
            await route.abort("blockedbyclient")
            return
        if request.method != "GET" or request.resource_type in BLOCKED_TYPES:
            await route.abort("blockedbyclient")
            return
        state["requests"] += 1
        if state["requests"] > self.s.browser_max_requests_per_page:
            await route.abort("blockedbyclient")
            return
        is_nav = request.is_navigation_request() and request.frame == page.main_frame
        if is_nav:
            cached = state["cache"].pop(url, None)
            try:
                res = cached or await self.f.get(url, kind="html")
            except (CrawlStop, RobotsDisallowed) as exc:
                state["stop"] = exc
                await route.abort("blockedbyclient")
                return
            final = res.final_url
            if final.split("#")[0] != url.split("#")[0] and cached is None:
                state["cache"][final] = res  # let the browser follow so relative URLs resolve correctly
                await route.fulfill(status=302, headers={"location": final}, body="")
                return
            state["main"] = res
        else:
            if request.frame != page.main_frame:  # iframes: only same-document helpers, no third-party widgets
                await route.abort("blockedbyclient")
                return
            try:
                res = await self.f.get_subresource(
                    url,
                    accept=request.headers.get("accept", "*/*"),
                    check_robots=request.resource_type in DATA_TYPES,  # data requests obey robots.txt too
                )
            except (RobotsDisallowed, RobotsUnavailable):  # this data request only; the page goes on
                await route.abort("blockedbyclient")
                return
        headers = {k: v for k, v in res.headers.items() if k.lower() in PASS_HEADERS}
        await route.fulfill(status=res.status, headers=headers, body=res.body)


def _empty_headers():
    import httpx

    return httpx.Headers({"content-type": "text/html"})


class BrowserFetcher:
    """Fetcher facade for SiteScan: HTML pages are rendered, feeds/PDFs fetched directly."""

    def __init__(self, renderer: BrowserRenderer, base: Fetcher):
        self.r = renderer
        self.base = base
        self.robots = base.robots
        self.user_agent = base.user_agent

    async def get(self, url: str, *, kind: str = "html", etag=None, last_modified=None) -> Page:
        if kind != "html" or url.lower().split("?")[0].endswith(".pdf"):
            return await self.base.get(url, kind=kind)
        return await self.r.render(url)

    async def get_subresource(self, url: str, **kw) -> Page:
        return await self.base.get_subresource(url, **kw)


# --- browser queue (PostgreSQL: crawl_runs.browser_pending) --------------------------------------------


def claim_browser_run(stale_minutes: int = 30) -> tuple[int, int, list[str]] | None:
    now = dt.datetime.now(dt.UTC)
    with session_scope() as s:
        run = s.execute(
            select(CrawlRun)
            .where(CrawlRun.browser_pending.is_(True))
            .where(
                (CrawlRun.browser_claimed_at.is_(None))
                | (CrawlRun.browser_claimed_at < now - dt.timedelta(minutes=stale_minutes))
            )
            .order_by(CrawlRun.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if run is None:
            return None
        run.browser_claimed_at = now
        run.browser_attempts += 1
        return run.id, run.company_id, list(run.browser_urls or [])


def _browser_done(
    run_id: int,
    *,
    ok: bool,
    stats: dict | None = None,
    error: str | None = None,
    company_id: int | None = None,
    retry: bool = False,
) -> None:
    with session_scope() as s:
        run = s.get(CrawlRun, run_id)
        if run is None:
            return
        if retry and run.browser_attempts < 3:
            run.browser_claimed_at = None  # back to the queue
        else:
            run.browser_pending = False
            run.browser_done_at = dt.datetime.now(dt.UTC)
        run.stats = {**(run.stats or {}), "browser": {**(stats or {}), "ok": ok, "error": error}}
        if error and company_id:
            s.add(
                ExtractionError(
                    company_id=company_id,
                    run_id=run_id,
                    stage="browser",
                    kind="render_error",
                    message=error[:1000],
                    strategy="browser",
                )
            )


async def run_browser_batch(
    settings: CrawlerSettings | None = None,
    *,
    fetcher: Fetcher | None = None,
    controller: ResourceController | None = None,
    time_budget_s: float = 600,
    max_companies: int | None = None,
    metrics=None,
) -> dict:
    from .runner import build_fetcher

    s = settings or get_crawler_settings()
    ctrl = controller or ResourceController(s)
    if not s.browser_enabled:
        return {"companies": 0, "skipped": "browser disabled"}
    if not ctrl.decide().browser_allowed:
        return {"companies": 0, "paused": "memory pressure"}
    own = fetcher is None
    base = fetcher or build_fetcher(
        s, on_stat=(lambda st: (metrics.on_fetch(st), ctrl.observe(st.status, st.elapsed_ms))) if metrics else None
    )
    loop_deadline = asyncio.get_running_loop().time() + time_budget_s
    done = 0
    try:
        async with BrowserRenderer(s, base, on_page=metrics.browser_page if metrics else None) as renderer:
            while asyncio.get_running_loop().time() < loop_deadline - 30:
                if max_companies is not None and done >= max_companies:
                    break
                ctrl.browser_active = renderer.active_pages
                if not ctrl.decide().browser_allowed:
                    log.warning("browser paused: memory pressure", **ctrl.snapshot())
                    break
                claim = await asyncio.to_thread(claim_browser_run)
                if claim is None:
                    break
                run_id, company_id, urls = claim
                await _render_company(s, renderer, base, run_id, company_id, urls, metrics)
                done += 1
    finally:
        if own:
            await base.aclose()
    return {"companies": done}


async def _render_company(s, renderer, base, run_id, company_id, urls, metrics) -> None:
    def ctx():
        with session_scope() as db:
            return load_context(db, company_id, s)

    try:
        info, _view, known, _resume = await asyncio.to_thread(ctx)
    except LookupError as exc:
        await asyncio.to_thread(_browser_done, run_id, ok=False, error=str(exc))
        return
    scan = SiteScan(
        BrowserFetcher(renderer, base),
        s,
        info,
        known_pages={},
        seeds=urls,
        rendered=True,
        max_pages=s.browser_max_urls_per_company * 3,
    )
    try:
        result = await asyncio.wait_for(
            scan.run(), timeout=s.browser_page_timeout_s * s.browser_max_urls_per_company * 4
        )
    except Exception as exc:  # renderer crash or timeout: the HTTP crawler is unaffected
        log.warning("browser render failed", company_id=company_id, error=repr(exc)[:300])
        try:
            await renderer.restart()
        except Exception:
            pass
        await asyncio.to_thread(
            _browser_done, run_id, ok=False, error=repr(exc)[:500], company_id=company_id, retry=True
        )
        return
    total = {
        "attempts": sum(v["attempts"] for v in result.strategy_stats.values()) or 1,
        "ok": int(bool(result.jobs)),
        "jobs": len(result.jobs),
    }
    result.strategy_stats = {"browser": total}
    result.browser_urls = []
    render_errors = [e for e in result.errors if e.stage in ("browser", "fetch")]
    result.complete = result.complete and not render_errors

    def save():
        with session_scope() as db:
            return persist(db, info, None, result, s, full_discovery=False, lifecycle_methods=("browser",))

    stats = await asyncio.to_thread(save)
    if metrics:
        metrics.company_done(result.strategy_stats, len(result.jobs), stats["jobs_new"])
    await asyncio.to_thread(
        _browser_done,
        run_id,
        ok=bool(result.jobs) or result.outcome == "no_vacancies",
        stats={**stats, "outcome": result.outcome, "pages": result.pages_fetched},
        company_id=company_id,
    )
    with session_scope() as db:  # the HTTP run's status reflects the browser pass now
        db.execute(
            update(CrawlRun)
            .where(CrawlRun.id == run_id, CrawlRun.status == "partial")
            .values(jobs_found=CrawlRun.jobs_found + len(result.jobs), jobs_new=CrawlRun.jobs_new + stats["jobs_new"])
        )
