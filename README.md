# Germany Career Crawler: company discovery (Sachsen) + adaptive career crawler

Self-hosted platform for finding German employers and their vacancies, built to run on a single 2 vCPU / 4 GB server. Two modules share one core (`companies` table, scan queue, SSRF-safe HTTP client, dashboard):

**`company_discovery`**: finds employers.

1. Finds companies with a website in **Sachsen (DE-SN)** via the **OpenStreetMap Overpass API** (`website`, `contact:website`), filtered by **region** (all of Sachsen, Dresden, Leipzig, Chemnitz or any of the 10 Landkreise) and **industry**.
2. Imports **CSV files** from company directories, including a profile for the WFS **Firmendatenbank Sachsen** (`MICRO!`, `MASCHINE!`, `LIFE SCIENCES!`, WZ/NACE).
3. Normalizes URLs and **deduplicates by registrable domain**, keeps **provenance** for every record, **verifies websites** (robots.txt first, no bypassing) and queues verified ones for crawling.

**`career_crawler`**: finds their vacancies ([docs/career-crawler.md](docs/career-crawler.md)).

1. **Adaptive strategy ladder per site:** public ATS feeds (Personio XML, Greenhouse, Lever), schema.org **JobPosting** JSON-LD, static HTML (lists, pagination, detail pages), Trafilatura content extraction and PDFs, **Chromium only for JavaScript career pages**, and an optional LLM fallback. The best strategy per site is cached with its success rate.
2. **Evidence-based ATS detection** (links, iframes, scripts, redirects). Account IDs are never guessed.
3. **Prioritized URL discovery** with robots.txt, sitemaps, German/English career terms, page/depth budgets, canonical URLs, content hashes, conditional GET, bounded external hops and early stopping.
4. **Resource-aware scheduling:** per-domain politeness across processes, a resource controller (CPU, RAM, container memory, latency, 429/503) with hysteresis, a browser pause under memory pressure, and PostgreSQL-backed queues with backpressure, checkpoints and crash recovery.
5. **Vacancy database:** normalized location, remote/hybrid, employment type, publication date (source only), skills, language levels, salary, confidence and method. Conservative dedup; postings are never closed after a single failed request.
6. **Adaptive recrawl** (12 h … 30 days), a dashboard with crawler metrics and vacancy search, and per-company failure diagnosis.
7. **Safe test mode and an offline benchmark** with 16 fixture websites ([backend/benchmark](backend/benchmark/README.md)).

```
            ┌──────────── Caddy (HTTPS, static dashboard, /api proxy) ────────────┐
Browser ───▶│  React dashboard                       FastAPI  /api/...            │
            └─────────────────────────────────────────────┬────────────────────────┘
          ┌───────────────── Redis (Celery broker, per-domain throttle) ◀──┤
          ▼                    ▼                     ▼                     ▼
  worker-discovery (-c 1)  worker-verify (-c 2)  worker-crawl (-c 1)    worker-browser (-c 1)
  Overpass, CSV imports    robots + homepage     career_scan, extract,  browser_scan:
                                                 maintenance (asyncio,  Chromium, every request
                                                 up to 16 sites)        via the SSRF-safe client
                                   │                     │                     │
                                   └──────── PostgreSQL ◀┴─────────────────────┘
          companies · scan_requests · jobs · career_sources · crawl_runs · crawl_pages ·
          site_fingerprints · crawl_metrics · extraction_errors · discovery tables
  beat: verify due sites 10 min · career_scan 60 s · browser_scan 120 s · maintenance 5 min
```

## Install on a new Ubuntu server (crawdler.duckdns.org)

From your Mac, copy the project to the server (`.env` is excluded so a later copy never overwrites the server's secrets):

```bash
rsync -av --exclude node_modules --exclude dist --exclude .env --exclude 'deploy/caddy.d/*.caddy' \
      ./ root@<server-ip>:/opt/germany-career-crawler/
ssh root@<server-ip>
cd /opt/germany-career-crawler
sudo ./install.sh --duckdns-token <your-duckdns-token> --email <you@example.net>
```

`install.sh` (Ubuntu 22.04/24.04/26.04) does the following:
- installs Docker with the Compose plugin, and adds 2 GB swap on servers with less than 2 GB RAM;
- with `--duckdns-token`, points `crawdler.duckdns.org` to the server and keeps it updated (systemd timer every 5 min);
- checks that the domain resolves to this server and that ports 80/443 are free, and opens them in ufw if ufw is active;
- creates `.env` with random secrets and admin password (first run only);
- builds and starts the stack, then waits until the API is healthy and Let's Encrypt has issued the certificate.

At the end it prints the URL and the admin password (shown once, stored in `.env`). Run it again after copying new code to update; secrets and data are kept. Options: `./install.sh --help`. Without a DuckDNS token, set the record at duckdns.org yourself. Any other domain works with `--domain`.

Also allow inbound TCP 80 and 443 in your provider's cloud firewall if it has one. Let's Encrypt needs port 80 for the HTTP challenge.

### Manual alternative

```bash
cp .env.example .env
# set POSTGRES_PASSWORD, SECRET_KEY (openssl rand -hex 32), ADMIN_PASSWORD, SITE_ADDRESS,
# CRAWLER_USER_AGENT (with a URL or e-mail where site owners can reach you)
echo "email you@example.net" > deploy/caddy.d/acme.caddy   # optional: certificate notices
docker compose up -d --build
docker compose ps            # all services "healthy"
```

Open `https://<SITE_ADDRESS>/` and sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`. Caddy gets the certificate automatically and redirects port 80 to HTTPS. To try it locally, use `SITE_ADDRESS=localhost` (self-signed certificate from Caddy's internal CA).

Database migrations run when the API container starts (`alembic upgrade head`). The API docs are at `/api/docs`, and the OpenAPI file is also in [docs/openapi.json](docs/openapi.json).

## Using it

**Discovery → OpenStreetMap discovery:** pick regions and industries, then press *Start discovery*. "All of Sachsen" runs one Overpass request per Kreis (13). The job table shows progress per Kreis. Results are split into **new domains**, **duplicate domains** (already known, or found twice in this job), **invalid / no website** (Facebook page instead of a website, …) and **seen before**.

**Discovery → Import CSV:** upload a file and choose a format. Columns are recognised by name (`Firma`/`Name`, `Webseite`/`URL`/`Internet`, `PLZ`, `Ort`, `Landkreis`, `Branche`, `WZ 2008`, `Technologiefelder`, `Firmen-ID`, …). Encodings UTF-8 and Windows-1252 are supported, as are the delimiters `,` `;` tab and `|`. Give a **source name** for each directory so record IDs from different files don't collide, and set licence and attribution. Examples: [examples/](examples/).

**Vacancies:** full-text search (German/English, e.g. `kubernetes -werkstudent`), filters for city, work model, employment type, skill, extraction method, "new this week" and status. The details show responsibilities, requirements, languages, salary, source links and how the posting was extracted. The publication date is shown only when the source states one.

**Export:** above the table, *CSV* and *CSV (;)* (semicolon: opens directly in German Excel) download **all** vacancies matching the current filters (the table columns, UTF-8 with BOM, max. 10 000 rows; text that looks like a spreadsheet formula is prefixed with `'`). *YAML (full)* downloads the same selection with **every stored field and the full description**, plus the company and skills; the file starts with the filters used and says if it was truncated. A single posting: *Download YAML* in its details. Same via the API: `/api/jobs/export.csv`, `/api/jobs/export.yaml`, `/api/jobs/{id}/export.yaml` accept the filters of `/api/jobs`.

**Crawler:** active and completed scans, domains per hour, pages per second, new postings, ATS vs. HTML vs. browser extraction with success and failure rates, queue lengths, browser usage, CPU and RAM per worker, slow or failing domains. Click a company to see *why* it yields no postings (diagnosis, cached strategy, career sources, last scans, errors) and to *Rescan* or run a *Full rediscovery*.

**Companies:** filter by text, region, industry, website status, scan status, source or job. You can export the filtered list as CSV (every row carries its sources, licences and attribution), open the details (all source records, OSM links, verification result), and **queue selected companies for vacancy scanning**. Only verified websites can be queued. Unverified ones are verified first and queued automatically if verification succeeds.

### Firmendatenbank Sachsen (firmen.standort-sachsen.de)

For DevOps/SRE roles, the most useful technology fields there are `MICRO!` (microelectronics, software, electronics), `MASCHINE!` (mechanical engineering, sensors) and `LIFE SCIENCES!`, plus IT service providers (WZ 62/63). The import profile `standort_sachsen` maps exactly these.

There is **no scraper for that site**, on purpose. When I checked on 9 November 2026, the directory offered neither a CSV export nor an API: its iFrame export is reserved for WFS staff, who refer other people to info@wfs.saxony.de. I found no terms of use permitting automated extraction, and the database as a whole is protected under the German sui-generis database right (§ 87b UrhG). The clean route is to ask WFS for an export of these categories and import it here. The template is [examples/standort_sachsen_template.csv](examples/standort_sachsen_template.csv).

## Responsible use built in

| Concern | What the code does |
|---|---|
| Overpass usage policy | One request at a time (PostgreSQL advisory lock, discovery worker concurrency 1). At least `OVERPASS_MIN_INTERVAL_S` (30 s) between requests. Daily budget of 50 requests / 200 MB, counted in `external_api_usage`. Identical queries are cached 24 h. On HTTP 429 the client asks `/api/status` for the next free slot. 504s back off at 60/120/240 s. Identifying User-Agent. |
| ODbL / attribution | Every OSM-derived record stores `ODbL-1.0` and "© OpenStreetMap contributors" in `company_sources`. The dashboard footer links to openstreetmap.org/copyright. CSV exports include licence and attribution per row and an `X-Data-Attribution` header. OSM-derived data stays identifiable so it can be offered under the ODbL if you ever make the database public. |
| Personal data | Only whitelisted OSM tags are stored (name, address, website, industry tags). Phone numbers, e-mail addresses and opening hours are dropped. Uploaded CSV files are deleted after import; their SHA-256 is kept. |
| robots.txt | Fetched before the homepage and checked for every redirect hop. `Disallow` means the site is never fetched or crawled. A 5xx answer means "try again later" (RFC 9309). |
| Access restrictions | 401/403, Cloudflare/DDoS-Guard challenges and CAPTCHA pages are recorded as `access_restricted`. They are not retried and not worked around. |
| SSRF | Only http/https on ports 80/443. Every resolved address must be public: no loopback, RFC 1918, link-local/metadata, CGNAT, ULA or IPv4-mapped. The connection is pinned to the checked IP, with SNI/Host set to the real name. No proxy environment. Redirects are re-checked, and redirects to social networks are not followed. |
| Crawl scope | Only `verified` companies can enter `scan_requests`. |
| Career crawler | The same SSRF-safe client for every page, feed, PDF and **every browser subresource** (Chromium only gets a black-hole proxy; requests are fulfilled by the client). robots.txt for every host and redirect, 1 request per domain every 2 s across all workers, `Retry-After` honoured, login walls and bot challenges stop the crawl of that site, size limits, XXE-safe XML. Postings are stored and shown as plain text. Details: [docs/career-crawler.md](docs/career-crawler.md#9-security). |
| Auth | Single admin login, HttpOnly + SameSite=Strict session cookie, `X-Requested-With` header required for state-changing requests, HTTPS via Caddy, CSP. |

This is not legal advice. Check the licence terms of every directory you import yourself.

## Crawler identity and the bot page

Every outgoing request (Overpass, website verification, `robots.txt`, career pages, ATS feeds and the browser's page loads) carries the single `CRAWLER_USER_AGENT` from `.env`; there is no second setting. `robots.txt` is matched against the **product token** derived from it, not against the whole string, so a Mozilla-style value works too:

| `CRAWLER_USER_AGENT` | robots.txt token |
|---|---|
| `GermanyCareerCrawler/0.1 (+https://host/bot)` | `GermanyCareerCrawler` |
| `Mozilla/5.0 (compatible; GermanyCareerCrawler/0.1; +https://host/bot)` | `GermanyCareerCrawler` |

Website owners can therefore use `User-agent: GermanyCareerCrawler` with `Disallow:` and `Crawl-delay:`. Disallowed pages are not fetched, which also holds for data requests (XHR/fetch) of the browser. A `Crawl-delay` is honoured up to 30 s.

**Public page for website owners:** `https://<SITE_ADDRESS>/bot` (no login). It explains the purpose, what is collected, the crawl policy, the active User-Agent and token, how to ask for a lower rate or exclusion, and shows `BOT_CONTACT_EMAIL`. The data comes from the public endpoint `/api/public/bot`, which exposes nothing else. Put the URL in `CRAWLER_USER_AGENT` and set `BOT_CONTACT_EMAIL`. Without a contact address the page says that none is published; it never invents one.

**Check the active configuration** (prints no secrets):

```bash
docker compose exec api python -m gcc_core.botinfo
curl -s https://<SITE_ADDRESS>/api/public/bot | jq
```

After changing `.env`: `docker compose up -d` (containers are recreated and read it again).

## Configuration

All settings come from environment variables; [.env.example](.env.example) lists them with defaults. The most important ones:

| Variable | Default | Meaning |
|---|---|---|
| `OVERPASS_MAX_REQUESTS_PER_DAY` / `OVERPASS_MAX_MB_PER_DAY` | 50 / 200 | Daily budget. When it is reached the job fails with a clear message. Finished Kreise come from the cache on the next run. |
| `OVERPASS_MIN_INTERVAL_S` | 30 | Pause between Overpass requests |
| `OVERPASS_URL` | overpass-api.de | Use another instance or your own (recommended for heavy use) |
| `VERIFY_CONCURRENCY` / `VERIFY_TIMEOUT_S` | 8 / 15 | Website checks in parallel, per-request timeout |
| `VERIFY_RECHECK_DAYS` | 30 | Re-verify verified sites after this time |
| `AUTO_QUEUE_VERIFIED` | true | Queue newly verified companies for career crawling |
| `IMPORT_MAX_MB` / `IMPORT_MAX_ROWS` | 20 / 50 000 | CSV limits |
| `CRAWLER_USER_AGENT` | GermanyCareerCrawler/0.1 (+…/bot) | The one User-Agent of every outgoing request; robots.txt token derived from it. See [Crawler identity](#crawler-identity-and-the-bot-page). |
| `BOT_CONTACT_EMAIL` | empty | Contact address shown on the public `/bot` page |
| `CRAWLER_HTTP_CONCURRENCY` / `CRAWLER_COMPANIES_IN_PARALLEL` | 16 / 16 | Upper bounds; the resource controller adapts below them |
| `CRAWLER_PER_DOMAIN_DELAY_S` | 2 | Minimum spacing of requests to one domain (all workers) |
| `CRAWLER_MAX_PAGES_PER_COMPANY` / `CRAWLER_MAX_DEPTH` | 50 / 4 | Crawl budget per company |
| `CRAWLER_BROWSER_ENABLED` | true | JavaScript rendering fallback (worker-browser) |
| `CRAWLER_CLOSE_AFTER_MISSES` / `CRAWLER_CLOSE_MIN_DAYS` | 3 / 3 | Complete scans without a posting and days unseen before it is closed |
| `CRAWLER_LLM_ENABLED` / `CRAWLER_LLM_URL` | false / – | Optional LLM fallback via an OpenAI-compatible endpoint (e.g. local Ollama) |
| `CRAWLER_TEST_MODE` / `CRAWLER_FIXTURES_DIR` | false / – | Serve every request from local fixture websites |

All `CRAWLER_*` settings: [backend/career_crawler/config.py](backend/career_crawler/config.py).

## Development

```bash
# backend (Python 3.12+), needs PostgreSQL and Redis
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
export DATABASE_URL=postgresql+psycopg://gcc:gcc@localhost:5432/gcc SECRET_KEY=$(openssl rand -hex 32) \
       ADMIN_PASSWORD=dev-password-123 COOKIE_SECURE=false DATA_DIR=./data
alembic upgrade head
uvicorn gcc_core.main:app --reload --port 8000
celery -A gcc_core.celery_app worker -Q discovery,verify,default -c 2     # second terminal
celery -A gcc_core.celery_app worker -Q career_scan,extract,maintenance,browser_scan -c 1  # crawler
celery -A gcc_core.celery_app beat                                         # schedules
python -m playwright install chromium-headless-shell                       # once, for the browser fallback

# frontend
cd frontend && npm ci && npm run dev      # http://localhost:5173, proxies /api to :8000

# tests: real PostgreSQL, no network needed (Overpass and websites are faked)
TEST_DATABASE_URL=postgresql+psycopg://gcc:gcc@localhost:5432/gcc_test pytest
ruff check .
python -m benchmark.run --repeat 3        # offline crawler benchmark (add --browser for Chromium)
```

## What was tested, and what not

### Career crawler

**Tested** (Python 3.13, PostgreSQL 16, Redis 7, Chrome 2 vCPU sandbox):
- 64 new automated tests (173 in total, all passing): extraction and normalization (dates never taken from discovery, placeholders rejected; skills, CEFR language levels, salary, location, remote/hybrid, employment types; JSON-LD with `@graph`, broken JSON and microdata; XXE-safe sitemaps), evidence-only ATS detection, the engine on all 16 fixture websites with field-level checks, robots.txt disallow, 429 + Retry-After, bot challenge, remembered ATS (no site crawl), fallback when a cached ATS disappears, SSRF (company host resolving to 10.0.0.5 / 169.254.169.254), the external-hop limit, page budgets, early stop, unchanged pages and 304s, the throttle and limiter. With PostgreSQL: the batch runner end to end, dedup on rescan, lifecycle (no miss on a failed page, closing only after 3 complete misses *and* 3 days, reopening), one running scan per company, crash recovery with checkpoint resume, bounded retries, adaptive scheduling, retention, the resource controller's hysteresis and browser pause, all new API endpoints, and two real Chromium tests: an end-to-end browser pass, and a page that tries to load `169.254.169.254`, `10.0.0.1` and a host rebinding to `192.168.1.10` (all blocked, no request reaches them).
- Migration `0002`: `upgrade` → `alembic check` → `downgrade 0001` → `upgrade`.
- The dashboard against a database seeded by a real batch + browser run over the fixtures, checked in headless Chrome. The offline benchmark: [backend/benchmark/README.md](backend/benchmark/README.md).

**Not tested:**
- **Real employer websites and real ATS endpoints.** The Personio XML, Greenhouse and Lever formats follow their public documentation; the fixtures imitate them. Field names may differ in practice. Start with a few companies and look at *Crawler → company → Career crawl*.
- **Extraction accuracy on real sites.** The fixture scores are a regression check, not a real-world measurement.
- **The Docker images** (no Docker Hub access here). In particular the browser stage (`playwright install --with-deps --only-shell chromium`) has not been built. `docker compose config` validates.
- Throughput and memory on the target VPS under live load.

### Crawler identity, bot page and exports

- 32 more automated tests (205 in total, all passing): the User-Agent is read from the environment and no second one exists; docker-compose forwards `.env` and never overrides it; the robots.txt token for plain and Mozilla-style User-Agents (a `Mozilla` group in someone's robots.txt does not apply to us); website verification and the career fetcher send the configured User-Agent for both robots.txt and pages; disallowed URLs are never requested (checked against what the server received); browser XHR/fetch is checked against robots.txt, static assets are not; `/api/public/bot` works without login and leaks no secrets or invented contact; every other endpoint in the OpenAPI schema answers 401 without a session; CSV/YAML exports equal the table for the same filters, neutralise spreadsheet formulas, keep umlauts, contain every column and the full description, declare truncation, require login.
- In headless Chrome: `/bot` renders without a session; the export buttons download the files for the filtered selection.
- **Not verified:** `https://crawdler.duckdns.org/bot` on the real server (Caddy serving the SPA fallback is configured but was not exercised here) and the exports on a real database of scraped postings.

### Company discovery

**Tested** (Python 3.13, PostgreSQL 16, Redis 7, Chrome):
- 106 automated tests: URL normalization (31 cases), the Overpass query builder and parser, the client (cache, budget, minimum interval, 429 via `/api/status`, 504 back-off, 400 error text), the region registry, CSV import (cp1252/`;`, BOM/tab, row limit, WFS categories, NACE), end-to-end OSM and CSV jobs on PostgreSQL (new / duplicate-in-job / duplicate-existing / invalid, re-runs without duplicates, provenance, OSM + CSV merge), website verification against local HTTP servers (robots.txt disallow without a homepage request, 403, Cloudflare challenge, parked domain, 429, robots 5xx, redirects, redirect to the cloud metadata IP, redirect to Facebook, DNS errors, https→http fallback, 14 address classes for SSRF), and every API endpoint including auth/CSRF, filters, pagination, CSV export, scan queue and the crawler-side `claim`/`finish`.
- Alembic `upgrade` → `check` (models and migration match) → `downgrade` → `upgrade`.
- Full stack with real processes: uvicorn, two Celery workers on Redis, the built dashboard. A Sachsen-wide OSM job (13 Kreise), a WFS CSV import, and verification through the worker: robots.txt-disallowed site not fetched, 403 site requested exactly once, verified site auto-queued. The dashboard was clicked through in headless Chrome (light and dark theme) with no JavaScript errors.

**Not tested**, because this sandbox can't reach them:
- The real **overpass-api.de**. Tests use a stand-in that returns Overpass-formatted JSON with fictional companies. The query syntax follows the Overpass QL docs. Whether every Kreis boundary in OSM carries `de:regionalschluessel` / `de:amtlicher_gemeindeschluessel` was not checked live. If a Kreis is not found, the job names it ("region boundary not found"). Run the first job on Dresden only and look at the results before scanning all of Sachsen.
- **Docker images** (Docker Hub is blocked here). `docker compose config` validates, but the images were not built. Expect the first `docker compose up --build` to be the first real build.
- **Caddy** with a real certificate, and verification of real websites over HTTPS on the internet. The Caddyfile was checked with `caddy validate` / `caddy adapt` (Caddy 2.10.2), with and without an e-mail.
- **install.sh** on a real server. It passed shellcheck and ran on Ubuntu 24.04 up to the build (`--no-start`), including the re-run that keeps `.env`. The DuckDNS update, public-IP/DNS check and the Docker installation itself could not run here (no outside network, Docker already present).

## Repository layout

```
backend/
  gcc_core/            shared: settings, DB, auth, URL normalization, SSRF-safe fetch,
                       companies API, scan queue (outbox for the crawler), Celery app
  company_discovery/   regions/ (Sachsen), industries, overpass, csv_import,
                       service (upsert/dedup), verify, jobs, tasks, api
  career_crawler/      engine, fetch, urls, discovery, extract/ (jsonld, html, pdf, normalize,
                       skills), ats/ (Personio, Greenhouse, Lever), store, runner, browser,
                       resources, schedule, maintenance, llm, tasks, api, testing
  benchmark/           offline fixture websites + ground truth, benchmark runner
  migrations/          Alembic (0001 discovery, 0002 career crawler)
  tests/               pytest (PostgreSQL), fixtures with fictional companies
frontend/              React + TypeScript dashboard (Vite)
deploy/                Caddyfile, web image
docs/                  module design (company-discovery.md, career-crawler.md), API, OpenAPI
examples/              example CSV files
install.sh             server installer / updater (Ubuntu)
docker-compose.yml, .env.example
```
