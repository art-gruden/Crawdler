# Germany Career Crawler: company discovery (Sachsen)

Self-hosted platform for finding German employers and, later, their vacancies. This repository currently contains the **`company_discovery` module**, plus the shared core it needs: the `companies` table, the dashboard and the scan queue the career crawler will consume.

What the module does:

1. Finds companies with a website in **Sachsen (DE-SN)** via the **OpenStreetMap Overpass API** (`website`, `contact:website`), filtered by **region** (all of Sachsen, Dresden, Leipzig, Chemnitz or any of the 10 Landkreise) and **industry** (IT, engineering, industry and manufacturing, research and technology; life sciences and "other companies" are opt-in).
2. Imports **CSV files** from other company directories, including a profile for exports of the WFS **Firmendatenbank Sachsen** (technology fields `MICRO!`, `MASCHINE!`, `LIFE SCIENCES!` and WZ/NACE codes).
3. Normalizes URLs (IDN, tracking parameters, social-media links rejected) and **deduplicates by registrable domain**.
4. **Verifies websites** without bypassing anything: it checks robots.txt first, does no CAPTCHA or bot-challenge workarounds, uses an SSRF-safe client, and backs off exponentially on transient errors.
5. Keeps **provenance** for every record: source, record ID (`node/123`, `id:SN-1001`), licence, attribution, data timestamp, and first/last seen.
6. **Inserts or updates** `companies` in PostgreSQL. It shows new vs. duplicate domains per job and queues verified websites for career-page crawling.

The career crawler (Phase 1 of the main spec) is **not part of this repository yet**. Discovery writes `scan_requests`, and the crawler will claim them with `gcc_core.scan_queue.claim()`. Neither module imports the other (see [docs/company-discovery.md](docs/company-discovery.md)).

```
            ┌──────────── Caddy (HTTPS, static dashboard, /api proxy) ────────────┐
Browser ───▶│  React dashboard                       FastAPI  /api/...            │
            └─────────────────────────────────────────────┬────────────────────────┘
                                                          │
          ┌───────────────── Redis (Celery broker) ◀──────┤
          ▼                                               ▼
  worker-discovery (-c 1)        worker-verify (-c 2)   PostgreSQL
  Overpass jobs, CSV imports     robots.txt + homepage   companies, company_sources,
  ──▶ overpass-api.de            ──▶ company websites    discovery_jobs, scan_requests …
                                                          ▲
  beat: re-verifies due websites every 10 min             │ claim()  (future career crawler)
```

## Quick start (Linux VPS, Docker Compose)

```bash
cp .env.example .env
# set POSTGRES_PASSWORD, SECRET_KEY (openssl rand -hex 32), ADMIN_PASSWORD,
# SITE_ADDRESS (your domain; DNS A/AAAA record must point to the server), ACME_EMAIL,
# CRAWLER_USER_AGENT (with a URL or e-mail where site owners can reach you)
docker compose up -d --build
docker compose ps            # all services "healthy"
```

Open `https://<SITE_ADDRESS>/` and sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`. Caddy gets the certificate automatically and redirects port 80 to HTTPS. To try it locally, use `SITE_ADDRESS=localhost` (self-signed certificate from Caddy's internal CA).

Database migrations run when the API container starts (`alembic upgrade head`). The API docs are at `/api/docs`, and the OpenAPI file is also in [docs/openapi.json](docs/openapi.json).

## Using it

**Discovery → OpenStreetMap discovery:** pick regions and industries, then press *Start discovery*. "All of Sachsen" runs one Overpass request per Kreis (13). The job table shows progress per Kreis. Results are split into **new domains**, **duplicate domains** (already known, or found twice in this job), **invalid / no website** (Facebook page instead of a website, …) and **seen before**.

**Discovery → Import CSV:** upload a file and choose a format. Columns are recognised by name (`Firma`/`Name`, `Webseite`/`URL`/`Internet`, `PLZ`, `Ort`, `Landkreis`, `Branche`, `WZ 2008`, `Technologiefelder`, `Firmen-ID`, …). Encodings UTF-8 and Windows-1252 are supported, as are the delimiters `,` `;` tab and `|`. Give a **source name** for each directory so record IDs from different files don't collide, and set licence and attribution. Examples: [examples/](examples/).

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
| Auth | Single admin login, HttpOnly + SameSite=Strict session cookie, `X-Requested-With` header required for state-changing requests, HTTPS via Caddy, CSP. |

This is not legal advice. Check the licence terms of every directory you import yourself.

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
| `CRAWLER_USER_AGENT` | GermanyCareerCrawler/0.1 (+…) | Sent to Overpass and websites. Put your contact URL here. |

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

# frontend
cd frontend && npm ci && npm run dev      # http://localhost:5173, proxies /api to :8000

# tests: real PostgreSQL, no network needed (Overpass and websites are faked)
TEST_DATABASE_URL=postgresql+psycopg://gcc:gcc@localhost:5432/gcc_test pytest
ruff check .
```

## What was tested, and what not

**Tested** (Python 3.13, PostgreSQL 16, Redis 7, Chrome):
- 106 automated tests: URL normalization (31 cases), the Overpass query builder and parser, the client (cache, budget, minimum interval, 429 via `/api/status`, 504 back-off, 400 error text), the region registry, CSV import (cp1252/`;`, BOM/tab, row limit, WFS categories, NACE), end-to-end OSM and CSV jobs on PostgreSQL (new / duplicate-in-job / duplicate-existing / invalid, re-runs without duplicates, provenance, OSM + CSV merge), website verification against local HTTP servers (robots.txt disallow without a homepage request, 403, Cloudflare challenge, parked domain, 429, robots 5xx, redirects, redirect to the cloud metadata IP, redirect to Facebook, DNS errors, https→http fallback, 14 address classes for SSRF), and every API endpoint including auth/CSRF, filters, pagination, CSV export, scan queue and the crawler-side `claim`/`finish`.
- Alembic `upgrade` → `check` (models and migration match) → `downgrade` → `upgrade`.
- Full stack with real processes: uvicorn, two Celery workers on Redis, the built dashboard. A Sachsen-wide OSM job (13 Kreise), a WFS CSV import, and verification through the worker: robots.txt-disallowed site not fetched, 403 site requested exactly once, verified site auto-queued. The dashboard was clicked through in headless Chrome (light and dark theme) with no JavaScript errors.

**Not tested**, because this sandbox can't reach them:
- The real **overpass-api.de**. Tests use a stand-in that returns Overpass-formatted JSON with fictional companies. The query syntax follows the Overpass QL docs. Whether every Kreis boundary in OSM carries `de:regionalschluessel` / `de:amtlicher_gemeindeschluessel` was not checked live. If a Kreis is not found, the job names it ("region boundary not found"). Run the first job on Dresden only and look at the results before scanning all of Sachsen.
- **Docker images** (Docker Hub is blocked here). `docker compose config` validates, but the images were not built. Expect the first `docker compose up --build` to be the first real build.
- **Caddy** with a real certificate, and verification of real websites over HTTPS on the internet.

## Repository layout

```
backend/
  gcc_core/            shared: settings, DB, auth, URL normalization, SSRF-safe fetch,
                       companies API, scan queue (outbox for the crawler), Celery app
  company_discovery/   this module: regions/ (Sachsen), industries, overpass, csv_import,
                       service (upsert/dedup), verify, jobs, tasks, api
  migrations/          Alembic
  tests/               pytest (PostgreSQL), fixtures with fictional companies
frontend/              React + TypeScript dashboard (Vite)
deploy/                Caddyfile, web image
docs/                  module design, API, OpenAPI
examples/              example CSV files
docker-compose.yml, .env.example
```
