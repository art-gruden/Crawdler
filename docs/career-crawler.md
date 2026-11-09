# Career crawler (`career_crawler`)

Adaptive vacancy crawler for the companies found by `company_discovery`. It reuses the existing
`companies` and `scan_requests` tables, the SSRF-safe client (`gcc_core.netsafe.safe_get`), the
Celery app and the dashboard. Existing modules, APIs, tables and tests are unchanged. Two small
backward-compatible extensions were made: `safe_get(extra_headers=…)` for conditional GETs, and new
routes/queues registered in `gcc_core.app` / `gcc_core.celery_app`.

Target: one 2 vCPU / 4 GB VPS with Docker Compose, no paid APIs.

## 1. Overview

```
 scan_requests (PostgreSQL, existing outbox)            crawl_runs.browser_pending (PostgreSQL)
        │ claim() SKIP LOCKED                                     │ claim SKIP LOCKED
        ▼                                                         ▼
 worker-crawl  (Celery -Q career_scan,extract,maintenance)   worker-browser (-Q browser_scan)
 ┌──────────────────────────────────────────────┐            ┌────────────────────────────────┐
 │ BatchRunner (asyncio, one process)           │            │ BrowserRenderer: 1 Chromium,    │
 │  ├ ResourceController ─► AdjustableLimiter   │            │ 1 context, 1 page               │
 │  ├ SiteScan × N companies in parallel        │  JS shell  │ every request ─► Fetcher        │
 │  │   ats ▸ jsonld ▸ html ▸ content/pdf ──────┼──────────▶ │ (SSRF-safe, robots, throttle)   │
 │  ├ Fetcher: robots, per-domain throttle,     │            │ SiteScan(seeds, rendered=True)  │
 │  │   429/503 + Retry-After, bot-wall stop    │            └────────────────────────────────┘
 │  └ store.persist: dedup, lifecycle, pages,   │
 │      errors, site fingerprint, next scan     │       Redis: Celery broker + cross-process
 └──────────────────────────────────────────────┘              per-domain throttle
 beat: career_scan 60 s · browser_scan 120 s · maintenance 300 s (messages expire, no pile-up)
```

| Module | Responsibility |
|---|---|
| `engine.py` | `SiteScan`: one company. Strategy ladder, frontier crawl, stop conditions, `ScanResult` (no DB access) |
| `fetch.py` | `Fetcher` on top of `safe_get`: robots.txt (Protego, Crawl-delay), throttle, global limiter, stop rules |
| `urls.py` | canonical URLs, tracking/session parameter removal, DE/EN career-link scoring, pagination |
| `discovery.py` | bounded priority frontier, sitemap/sitemap-index parsing (XXE-safe) |
| `extract/` | `jsonld.py` (JobPosting, @graph, ItemList, microdata, broken JSON), `html.py` (page classification, job lists, details, Trafilatura), `pdf.py`, `normalize.py`, `skills.py` |
| `ats/` | plugin interface + Personio XML, Greenhouse Job Board API, Lever postings API |
| `store.py` | persistence: sources, dedup, lifecycle, pages, errors, fingerprints, runs |
| `runner.py` | HTML worker batch runner on `scan_requests` |
| `browser.py` | Playwright renderer + browser queue |
| `resources.py` | resource controller (AIMD with hysteresis) |
| `schedule.py`, `maintenance.py` | adaptive recrawl, recovery, retention, optional LLM pass |
| `api.py` | `/api/jobs…`, `/api/crawler…` |
| `testing.py` | safe test mode: fixture transport + resolver |

## 2. Extraction strategies (priority order)

| # | Strategy | When | Notes |
|---|---|---|---|
| 1 | `ats` | ATS account **evidenced** by a link, iframe, script URL, inline-script URL or redirect target on a verified company page, or remembered in the fingerprint | Personio `https://<acc>.jobs.personio.de/xml`; Greenhouse `boards-api[.eu].greenhouse.io/v1/boards/<token>/jobs?content=true`; Lever `api[.eu].lever.co/v0/postings/<acc>?mode=json` (paged). A feed is a *complete* list, so missing postings may count as misses. |
| 2 | `jsonld` | `application/ld+json` with `JobPosting` (also `@graph`, arrays, ItemList) or schema.org microdata | lenient parser for control characters and trailing commas |
| 3 | `html` | static HTML: list pages (job-like anchors, `(m/w/d)`, career path groups), pagination (`rel=next`, `?page=`, "Weiter"), detail pages | selectolax (lexbor) |
| 4 | `content` / `pdf` | detail pages with poor markup → Trafilatura main content; job-like PDF links → pypdf text | bounded: 5 PDFs, 5 MB, 6 pages |
| 5 | `browser` | a career-relevant page is a JS shell (little text + app root / "enable JavaScript" / several scripts) | separate worker; postings recorded as method `browser` |
| 6 | `llm` | optional (`CRAWLER_LLM_ENABLED`), career page found but nothing extracted | OpenAI-compatible endpoint (e.g. local Ollama). Titles must appear verbatim in the page text; LLM output never closes postings. |

**Classification.** Every HTML page gets a type: `job_detail`, `job_list`, `career_landing`,
`no_vacancies` ("derzeit keine offenen Stellen"), `js_shell`, `irrelevant` (stored in
`crawl_pages.page_type`).

**Strategy cache.** `site_fingerprints.strategy_stats` keeps attempts, successes, postings and
the success rate per strategy, and `best_strategy` is the one that produced most postings. Next
scan: a remembered ATS goes straight to the feed (no site crawl); a remembered browser-only site
goes straight to the browser queue; otherwise the remembered career URLs are crawled first. Every
7 days (or when the cached path fails, or via *Full rediscovery*) the full ladder runs again.

**Why not Scrapy / Crawl4AI.** The spec asks for Scrapy for static HTML and for Crawl4AI only when
it measurably helps. Neither is used:
- Scrapy brings its own Twisted downloader. That would bypass the existing SSRF guarantees (IP
  pinning, per-hop checks), and running a Twisted reactor inside Celery workers is fragile (the
  reactor cannot be restarted). The required functions (priority frontier, robots, throttling,
  dedup, depth/page budgets) are covered by about 300 lines on top of the existing secure client,
  in one asyncio process.
- Crawl4AI would bring a second browser stack and LLM-oriented pipelines; Playwright is already
  the single browser engine.

If you want Scrapy anyway, the place to plug it in is `Fetcher` (a Scrapy download handler
that calls `safe_get`).

## 3. URL discovery

- robots.txt per origin before the first request, and for every redirect hop. `Crawl-delay` is
  honoured (capped at 30 s). robots 5xx/429 means full disallow (RFC 9309) and the scan stops.
- Seeds: remembered career URLs, then the homepage. If nothing career-like is found: sitemaps
  from robots.txt or `/sitemap.xml` (index → career-looking child sitemaps first, max 3 files /
  20 000 URLs), then a few common paths (`/karriere`, `/jobs`, `/career(s)`).
- Links are scored with German and English career terms (positive) and shop/news/privacy/
  calendar/download/login terms (negative). Binary files are skipped; PDFs only when job-like.
- Canonical URLs: lower-case host, default ports, no fragment, sorted query, tracking and session
  parameters removed (`utm_*`, `gclid`, `fbclid`, `jsessionid`, …). Dedup against the frontier.
- Content hash of the normalized text. An unchanged job detail page is not re-extracted (it
  counts as "still online"). Known detail pages use conditional GET (`If-None-Match` /
  `If-Modified-Since`, 304).
- Budgets: 50 pages/company, depth 4, frontier ≤ 400 entries (bounded heap), 10 min/company.
- External hosts: only known job-board / ATS / `karriere.*` hosts, at most
  `CRAWLER_MAX_EXTERNAL_ATS_HOPS` (2) per company. Redirects into an ATS board are evidence.
- Early stop: after 12 pages without postings, if fewer than 15 % of pages were career-relevant.

## 4. Scheduling and resources

**Initial settings** (environment `CRAWLER_*`, these are tuning start values, not guarantees):
global HTTP concurrency 16, 1 request per domain at a time with 2 s spacing, 15 s timeout, browser
1 page / 1 context, one HTML and one browser worker process, 50 pages and depth 4 per company.

**Per-domain politeness.** `RedisThrottle` (Lua script) reserves the next slot per registrable
domain across *all* processes. Shared ATS hosts are therefore spaced too. If Redis is unavailable
the throttle falls back to an in-process throttle. 429/503 cool the domain down for `Retry-After`
(default 10/5 min, max 24 h), stop the scan, and set `site_fingerprints.rate_limited_until`. The
scheduler waits at least that long.

**ResourceController** (`resources.py`), sampled every 5 s by the batch runner:

| Signal | Source |
|---|---|
| host MemAvailable, CPU % | psutil (inside a container: what the kernel reports for the host) |
| container memory, limit | cgroup v2 `memory.current`/`memory.max` (v1 fallback) |
| active HTTP requests / browser pages | in-process counters |
| queue sizes | `scan_requests` pending, browser and LLM queues |
| latency | EWMA vs. a slowly adapting baseline |
| 429/503 share | sliding 120 s window |

Policy: multiplicative decrease ×0.7 on pressure (CPU > 85 %, MemAvailable < 450 MB, container
> 85 % of its limit, latency > 3× baseline, 429/503 > 5 %), at most once per 10 s. Additive
increase +1 only after 3 consecutive clearly healthy samples (CPU < 60 %, MemAvailable > 900 MB,
429/503 < 1 %). Between the bands nothing changes (hysteresis). The browser pauses below 600 MB
MemAvailable (or container > 85 %) and resumes above 900 MB. Limits act on new work; running
requests finish.

**Backpressure.** No unbounded in-memory queue anywhere. The runner claims from PostgreSQL only
as many companies as it has free slots (`min(CRAWLER_COMPANIES_IN_PARALLEL, http limit)`). The
browser queue lives in PostgreSQL (`crawl_runs.browser_pending`) and is claimed one company at a
time. Beat messages expire, so a busy worker cannot accumulate a backlog of ticks.

## 5. Celery and recovery

| Task | Queue | Worker | Does |
|---|---|---|---|
| `career_crawler.tasks.career_scan` | `career_scan` | worker-crawl | batch runner, time budget 15 min |
| `career_crawler.tasks.extract_llm` | `extract` | worker-crawl | optional LLM pass |
| `career_crawler.tasks.maintenance` | `maintenance` | worker-crawl | recovery, adaptive scheduling, hourly retention |
| `career_crawler.tasks.browser_scan` | `browser_scan` | worker-browser | renders queued JS career pages |

The existing `worker-discovery` and `worker-verify` are unchanged. One crawl worker process serves
three queues, so no queue needs its own process.

- **Idempotent:** all work is claimed in PostgreSQL with `FOR UPDATE SKIP LOCKED`. A duplicated
  or redelivered message finds nothing or other work.
- **No concurrent duplicates:** `scan_requests` keeps at most one open request per company
  (existing unique index), and `crawl_runs` has a unique partial index (`status='running'`), so a
  second scan of the same company cannot start even from another worker.
- **Checkpointing:** every 5 pages the run heartbeats `crawl_runs.checkpoint` (visited URLs,
  frontier head). Maintenance marks runs without heartbeat for 45 min as `abandoned`, puts their
  request back to `pending`, and the next scan resumes, skipping visited pages.
- **Bounded retries:** internal errors are retried up to `CRAWLER_MAX_SCAN_ATTEMPTS` (3), then
  the request is `failed` and the error is stored. Site-side stops (429, bot wall, robots 5xx) are
  not retried immediately; the adaptive scheduler picks them up later.
- **Timeouts:** `soft_time_limit`/`time_limit` on every task, per-company time budget, per-request
  timeout, browser page timeout.
- **Browser isolation:** separate container and process. A Chromium crash restarts the renderer
  and returns the company to the browser queue (max 3 attempts); the HTTP crawler is unaffected.

## 6. Vacancy data

Fields (table `jobs`): company, title (+ normalized), source URL, canonical URL (+ alternates),
application URL, career page URL, location (raw, city, postcode, region, country), remote type
(`onsite|hybrid|remote|unknown`), employment types, **publication date (`date_posted`, only from
the source)**, discovery date, last successful observation (`last_seen_at`), requirements,
responsibilities, technology skills (`job_skills`), language requirements with CEFR level and
required/optional, salary (min/max/currency/period/raw), status (`open|closed|expired`), extraction
confidence, extraction method, content hash, and `source_values` (original values such as
`datePosted`, `employmentType`, `updated_at`, `createdAt`).

Rules:
- `date_posted` is never filled from the discovery time. Placeholders (1970, future dates) are
  rejected. Greenhouse `updated_at` is kept in `source_values` but not used as a publication date.
- Descriptions are stored as normalized **plain text** (≤ 40 000 chars); HTML never reaches the
  database or the dashboard.
- **Deduplication (conservative):** `dedup_key` = ATS id (`<company>:ats:<type>:<account>:<id>`)
  › JSON-LD identifier › canonical posting URL › content key (when one URL carries several
  postings). Fallback merge only within the same company, for the same normalized title and city,
  when content hashes match or one side lacks the strong id (list entry first, feed later). A
  stronger key replaces a weaker one.
- **Lifecycle:** a posting is never closed after a single failed request. Misses are counted only
  after a *complete* read (frontier drained, no budget/early stop, no fetch errors; or a complete
  ATS feed for that source). It closes after `CRAWLER_CLOSE_AFTER_MISSES` (3) complete misses
  *and* `CRAWLER_CLOSE_MIN_DAYS` (3) days unseen. It reopens on the same row if it comes back.
  `validThrough` in the past → `expired`. Rendered postings are judged only by the browser pass.

## 7. Database (migration `0002`)

New tables:

| Table | Key / constraints | Purpose |
|---|---|---|
| `career_sources` | unique (company_id, url); index (ats_type, ats_account) | career pages and ATS feeds with evidence, last success/error, failures |
| `jobs` | unique `dedup_key`; checks on status/remote_type; GIN full-text `search` (generated tsvector, weights title › location › description); indexes for company/status, status/last_seen, discovered, date_posted, canonical URL, employment types (GIN) | vacancies |
| `job_skills` | PK (job_id, skill), index skill | skills per vacancy |
| `crawl_runs` | unique partial (company_id) where running; browser queue index | run lock, heartbeat, checkpoint, results, browser/LLM queue |
| `crawl_pages` | unique (company_id, url_hash) | incremental metadata: type, status, content hash, ETag/Last-Modified, change count, optional raw-HTML path with expiry |
| `crawl_metrics` | PK (bucket minute, worker) | counters + resource samples for the dashboard |
| `site_fingerprints` | PK company_id; index next_scan_at | strategy cache, ATS, career URLs, browser need, latency, rate limit, schedule |
| `extraction_errors` | index (company, created) | why something failed (stage, kind, message, URL, status) |

`companies` and `scan_requests` are reused unchanged.

**Migration plan.** `alembic upgrade head` (the API container runs it on start) adds the tables;
no existing table is altered, so it is safe on a live database and `alembic downgrade 0001`
removes it cleanly. Deploy order: pull → `docker compose up -d --build` (the API migrates, then
the workers start). Rollback: stop `worker-crawl`/`worker-browser`, `alembic downgrade 0001`,
deploy the previous image.

**Growth limits.** Raw HTML is off by default (`CRAWLER_KEEP_RAW_HTML`); when on it is kept 7 days
and the directory is capped at 500 MB. Retention (hourly): errors 30 d, metrics 30 d, page
metadata 120 d unseen, closed postings 365 d, at most 20 runs per company. Descriptions ≤ 40 000
chars, `source_values` truncated, error messages ≤ 1 000 chars, ≤ 50 errors per run.

## 8. Adaptive recrawling

`schedule.next_interval_h` after every scan (±10 % jitter). Maintenance queues due, verified
companies via `scan_queue.request_scan(source="schedule")`.

| Situation | Interval |
|---|---|
| new postings now or in the last 7 days | 18 h (12–24 h band) |
| postings, nothing new | 60 h (2–3 days); unchanged for 3 weeks → doubled, max 7 d |
| career page, no postings / "no vacancies" | 7 days |
| no career page | 21 days (14–30 d band) |
| 429/503 | ≥ max(Retry-After, 6 h) × 2^errors |
| timeouts / unreachable / robots 5xx | 12–24 h × 2^errors (max 30 d) |
| login wall / bot challenge | 30 days (never bypassed) |

## 9. Security

- **HTTP:** everything goes through `safe_get`: http/https only, ports 80/443, every resolved
  address must be public (no loopback, RFC 1918, link-local / cloud metadata 169.254.169.254,
  CGNAT, ULA, multicast, IPv4-mapped), connection pinned to the checked IP (DNS rebinding
  protection), every redirect re-checked (max 5), body size limits (HTML 2 MB, feeds/sitemaps
  8 MB, PDF 5 MB, gzip bounded), no proxy environment.
- **Browser:** Chromium gets `--proxy-server=127.0.0.1:9` (black hole). Every request of the
  context is intercepted and answered via `route.fulfill` from the same `Fetcher`, so documents,
  scripts, XHR/fetch and redirects all get the same IP checks and pinning. Navigations also pass
  robots.txt and the throttle. Non-GET requests, third-party iframes, images/media/fonts,
  WebSockets, service workers, downloads and popups are blocked. Requests per page are capped
  (120). Covered by a test: a page loading `169.254.169.254`, `10.0.0.1` and a host that resolves
  to `192.168.1.10` gets none of them.
- **No bypassing:** 401/403/407, CAPTCHA/bot challenges and robots.txt disallows stop the crawl
  of that site and are recorded; nothing is retried around them.
- **Parsers:** XML with `resolve_entities=False, no_network=True, load_dtd=False` (XXE test);
  JSON-LD size-capped; no evaluation of page scripts outside the sandboxed browser.
- **Dashboard:** postings are plain text rendered as text (React escapes); external links go
  through the existing `SafeLink` (http/https only, `noopener noreferrer`). The CSP is unchanged.
- **Personal data:** e-mail addresses, phone numbers and contact persons are not extracted into
  fields. The description text is stored as published, so a contact line inside it stays part of
  the text; it is deleted with the posting by retention. Raw HTML is off by default.
- **LLM:** off by default. The endpoint is operator-configured, page text only goes into the
  prompt, and the output is verified against the page text.

## 10. Test mode and benchmark

`CRAWLER_TEST_MODE=true` + `CRAWLER_FIXTURES_DIR=/app/benchmark/sites` makes every worker serve
requests from local fixture websites through the full stack (DNS check, pinning, robots,
redirects, size limits). No employer website is contacted.

```bash
cd backend
python -m benchmark.build_fixtures        # regenerate fixtures (committed already)
python -m benchmark.run --repeat 3        # HTTP strategies
python -m benchmark.run --browser         # + Chromium for the JS site
pytest -q                                 # unit + integration tests, no network
```

The 16 fixture sites cover static HTML, JSON-LD, Personio XML, Greenhouse JSON (embed script),
Lever JSON (EU), an external ATS redirect, a JavaScript-rendered career page, a paginated list,
a PDF job description, "no vacancies", 429 with Retry-After, a timeout, malformed HTML with
broken JSON-LD, a site without a career page, a career page only in the sitemap, and a bot
challenge. See [benchmark/README.md](../backend/benchmark/README.md) for the measured results and
their limits.

## 11. Operations

| Want | Do |
|---|---|
| Crawl a company now | dashboard → company → *Rescan* (or `POST /api/crawler/companies/{id}/rescan`) |
| Forget a wrong cached strategy | *Full rediscovery* (`{"full": true}`) |
| Why no postings? | Companies → details → *Career crawl*, or Crawler → slow/failing domains |
| Slower / gentler | `CRAWLER_HTTP_CONCURRENCY`, `CRAWLER_COMPANIES_IN_PARALLEL` down; `CRAWLER_PER_DOMAIN_DELAY_S` up |
| No browser | `CRAWLER_BROWSER_ENABLED=false` and `docker compose stop worker-browser` |
| Debug a page | `CRAWLER_KEEP_RAW_HTML=true` (7-day retention, files under `/data/crawl-raw`) |
| Logs | `docker compose logs -f worker-crawl worker-browser` (structured, one line per company) |
