# API

Interactive documentation: `https://<host>/api/docs`. Machine-readable: [openapi.json](openapi.json).

All endpoints except `/api/health` and `/api/auth/login` need the session cookie. Every `POST` needs the header `X-Requested-With: gcc-ui`.

```bash
H='X-Requested-With: gcc-ui'
curl -c jar -H "$H" -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"…"}' https://jobs.example.org/api/auth/login
```

## Required endpoints

### `POST /api/discovery/osm`: start an OpenStreetMap discovery job (202)

```bash
curl -b jar -H "$H" -H 'Content-Type: application/json' https://jobs.example.org/api/discovery/osm \
     -d '{"regions": ["14612", "14713", "14511"], "industries": ["it", "engineering", "industrial", "technology"], "verify": true}'
```

`regions`: Kreisschlüssel or `DE-SN` (all 13 Kreise). See `GET /api/discovery/regions`. `industries`: ids from `GET /api/discovery/industries`. Only one OSM job may run at a time (409 otherwise). The response is the job. Poll `GET /api/discovery/jobs/{id}`.

### `POST /api/companies/import`: import a CSV file (202)

`multipart/form-data`: `file` (required), `profile` (`generic` | `standort_sachsen`), `source_name`, `license`, `attribution`, `source_url`, `verify` (true/false).

```bash
curl -b jar -H "$H" -F file=@wfs-export.csv -F profile=standort_sachsen https://jobs.example.org/api/companies/import
curl -b jar -H "$H" -F file=@ihk.csv -F profile=generic -F "source_name=IHK Dresden" -F license=CC-BY-4.0 \
     https://jobs.example.org/api/companies/import
```

422 if no company-name column is found (the message lists the accepted header names). 413 if the file is larger than `IMPORT_MAX_MB`.

### `GET /api/companies`: list and filter

| Parameter | Example | |
|---|---|---|
| `q` | `elbtal` | name, domain or city contains |
| `region` | `14612`, `DE-SN` | Kreis or state |
| `industry` | `it` | |
| `verification` | `verified` or `unreachable,dns_error` | comma-separated |
| `scan_status` | `queued` | `not_queued`, `queued`, `scanning`, `done`, `failed` |
| `source` | `osm`, `csv:` | prefix match on the source |
| `job_id`, `new_in_job` | `7`, `true` | companies touched / created by a discovery job |
| `sort`, `order` | `name`, `asc` | `name`, `domain`, `first_seen`, `last_seen`, `city`, `verification` |
| `page`, `page_size` | `1`, `50` | max 500 |

Response: `{"items": [...], "total": n, "page": 1, "page_size": 50}`. Each item contains `domain` (ASCII/punycode, the dedup key), `domain_display` (Unicode), `verification_status`, `scan_status`, `industries`, `sources` and the address fields.

`GET /api/companies/export.csv` takes the same filters and returns a UTF-8 CSV with BOM. Each row has `sources`, `licenses` and `attribution` columns.

### `POST /api/companies/{id}/scan`: queue one company for vacancy scanning (202)

| Situation | Response |
|---|---|
| website verified | `{"status": "queued"}` or `"already_queued"` (a manual request raises the priority of a pending one) |
| not yet verified / retryable error | `{"status": "verifying"}`: verified now, queued automatically if it succeeds |
| robots.txt disallow, access restricted, parked, no website … | 409 with the reason |

Bulk: `POST /api/companies/scan` with `{"ids": [1, 2, 3]}` returns counts `queued`, `already_queued`, `verifying`, `not_scannable`, `not_found`.

## Further endpoints

| Method | Path | |
|---|---|---|
| GET | `/api/discovery/jobs?limit=50` | latest jobs with params, stats, errors and provenance |
| GET | `/api/discovery/jobs/{id}` | one job |
| POST | `/api/discovery/jobs/{id}/cancel` | stops between Kreise / batches; a pending job is cancelled immediately |
| GET | `/api/discovery/jobs/{id}/items?outcome=new\|duplicate\|invalid\|updated` | per-record results, with `counts` per outcome |
| GET | `/api/discovery/regions`, `/industries`, `/import-profiles` | metadata for forms |
| GET | `/api/discovery/overpass/usage` | today's Overpass requests/bytes vs. budget |
| GET | `/api/companies/{id}` | detail incl. all source records and the verification result |
| POST | `/api/companies/{id}/verify` | re-check the website now |
| GET | `/api/health` | database and broker status (used by the container health check) |
| POST | `/api/auth/login`, `/api/auth/logout`; GET `/api/auth/me` | session |

## Career crawler

All endpoints need the session cookie; `POST` needs `X-Requested-With: gcc-ui`. The queue
endpoint is unchanged: `POST /api/companies/{id}/scan` (and `/api/companies/scan` for bulk).

| Method | Path | |
|---|---|---|
| GET | `/api/jobs?q=&status=open\|closed\|expired\|all&company_id=&city=&remote_type=&employment_type=&skill=&method=&new_days=&posted_days=&min_confidence=&sort=discovered\|posted\|title\|company\|relevance&limit=&offset=` | vacancy search. `q` uses PostgreSQL `websearch_to_tsquery` (quotes, `-exclude`, `or`) plus a title substring match. `skill` can repeat (AND). |
| GET | `/api/jobs/export.csv?<same filters>&delimiter=,\|;&limit=` | download of **all** matching vacancies (max. 10 000) as CSV: the table columns, UTF-8 BOM, formula-like text prefixed with `'` |
| GET | `/api/jobs/export.yaml?<same filters>&limit=` | the same selection as YAML with every stored field incl. full description, company and skills; header with filters, `total_matching`, `exported`, `truncated` |
| GET | `/api/jobs/{id}/export.yaml` | one vacancy as YAML, all fields |
| GET | `/api/public/bot` | **no login**: crawler name, User-Agent, robots.txt token, contact address, crawl delay (for the `/bot` page) |
| GET | `/api/jobs/{id}` | full posting: plain-text description, requirements, responsibilities, languages, salary, source values, extraction method/confidence |
| GET | `/api/jobs/skills?limit=40` | most frequent skills in open postings |
| GET | `/api/crawler/stats?hours=24` | dashboard figures: active/completed scans, domains per hour, pages per second, open/new postings, 429/503 share, latency, extraction success/failure per method, queues, latest CPU/RAM sample per worker |
| GET | `/api/crawler/timeseries?hours=24&bucket_minutes=15` | pages, new postings, errors, 429/503, companies, CPU, MemAvailable, browser pages, HTTP limit per bucket |
| GET | `/api/crawler/runs?status=running\|done\|partial\|failed\|abandoned&limit=50` | company scans |
| GET | `/api/crawler/problem-domains` | slow or failing sites (error streak, latency, rate limit, last error) |
| GET | `/api/crawler/companies/{id}` | diagnosis in plain words, site fingerprint (strategy, success rates, ATS, career URLs, schedule), career sources with evidence, last runs, errors, career pages |
| GET | `/api/crawler/ats/providers` | per ATS provider: capabilities (catalogue, search parameters, unverified parameters), `access_status` and evidence, `live_ready`, whether it is enabled, observed access (`ok`, `failing`, `robots_blocked`, `not_fetched`, `no_data`), source/open-job counts, strategies, scan completeness, last (complete) scan, top errors; plus the active ATS settings |
| GET | `/api/crawler/ats/sources?provider=&only=failing\|incomplete\|disabled\|capped\|search&limit=50` | ATS sources with strategy, `scan_complete`, provider-reported total, job count, search statistics (queries run/failed/saturated, budget skips), last error |
| POST | `/api/crawler/companies/{id}/rescan` body `{"full": false}` | queue with manual priority; `full: true` also forgets the cached strategy/ATS (next scan runs full discovery). 409 if the website is not verified. |

### Vacancy statistics (admin session only, see [STATS.md](STATS.md))

All GET endpoints share the parameters `period` (`today|yesterday|last_24h|7d|30d|custom`), `from`/`to` (custom, local dates, inclusive), `timezone`, `country`, `region`, `city`, `specialization` (repeatable; `unclassified` is allowed), `exclude_initial_load` and `as_of` (snapshot time). Invalid input answers 422.

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/api/crawler/stats/jobs/config` | taxonomy, countries, regions, default time zone, Telegram flags (never the token) |
| GET | `/api/crawler/stats/jobs/summary` | KPI cards (today, 24 h, 7 d, 30 d, selected period), active, updated/closed/reopened, data quality |
| GET | `/api/crawler/stats/jobs/trends` | new vacancies per local day (per hour up to 48 h) |
| GET | `/api/crawler/stats/jobs/specializations?level=&sort=&order=&limit=` | primary-specialization table with previous period, change, today/7 d/30 d/active, overlap |
| GET | `/api/crawler/stats/jobs/cities?mode=unique\|coverage&level=city\|region\|country&sort=&limit=` | place ranking; `unique` adds up to the total |
| GET | `/api/crawler/stats/jobs/countries` | DE / AT / CH / several / unknown |
| GET | `/api/crawler/stats/jobs/specialization-city?level=&rows=&cols=` | matrix and top combinations |
| POST | `/api/crawler/stats/jobs/telegram-preview` | post text from the same numbers (`language`, `top_*`, `format`, ...) |
| POST | `/api/crawler/stats/jobs/telegram-publish` | Phase 4, off by default; needs `confirm` and the previewed `approved_sha256` |
| GET | `/api/crawler/stats/jobs/telegram-publications` | publication history |

`GET /api/jobs` also accepts `discovered_from`, `discovered_to`, `place_city`, `dach_scope` and `specialization=unclassified`, so a statistic can be opened as the exact list behind it.

Example:

```bash
curl -s -b cookies 'https://crawdler.duckdns.org/api/jobs?q=kubernetes&city=Dresden&new_days=7' | jq '.items[] | {title, company_name, date_posted, skills}'
```


## Bot API (`/api/bot/v1`)

Read-only, Bearer-token authenticated, independent of the administrator session. See [DACH.md](DACH.md) for semantics
and guarantees; `BOT_API_KEYS` in `.env` (never put a token in a repository or a log).

Typical client flow (token in the `GCC_BOT_TOKEN` environment variable):

```bash
H="Authorization: Bearer $GCC_BOT_TOKEN"; B=https://<SITE_ADDRESS>/api/bot/v1

# 1. what can be chosen
curl -s -H "$H" $B/facets/countries | jq '.[] | {code, name, open_jobs}'
curl -s -H "$H" "$B/facets/regions?country=AT" | jq '.[] | {code, name, open_jobs}'
curl -s -H "$H" "$B/facets/cities?country=CH&region=CH-ZH" | jq

# 2. filtered search: Python/Kubernetes in Switzerland, German B2 + English C1, CHF 100k+, newest first
curl -s -H "$H" -G $B/jobs \
  --data-urlencode country=CH --data-urlencode "skills=Python,Kubernetes" --data-urlencode skills_mode=any \
  --data-urlencode "languages=de:B2,en:C1" --data-urlencode salary_min=100000 --data-urlencode currency=CHF \
  --data-urlencode limit=10 | jq '.items[] | {id, title, company: .company.name, language_fit, salary}'
#    next page: add  --data-urlencode "cursor=<next_cursor>"  with the same filters

# 3. start following changes from now (first run) ...
CUR=$(curl -s -H "$H" $B/changes/head | jq -r .cursor)

# 4. ... and poll; persist next_cursor only after the page was processed
curl -s -H "$H" -G $B/changes --data-urlencode "cursor=$CUR" --data-urlencode limit=100 \
  | jq '{n: (.items|length), next_cursor, has_more, first: .items[0]}'
```

Python sketch of a resumable consumer:

```python
import os, requests

S = requests.Session(); S.headers["Authorization"] = f"Bearer {os.environ['GCC_BOT_TOKEN']}"
BASE = "https://<SITE_ADDRESS>/api/bot/v1"

def poll(cursor: str | None, handle) -> str | None:
    while True:
        page = S.get(f"{BASE}/changes", params={"cursor": cursor, "limit": 200} if cursor else {"limit": 200}, timeout=30)
        page.raise_for_status()
        body = page.json()
        for ev in body["items"]:
            handle(ev)                      # idempotent: de-duplicate on ev["event_id"]
        cursor = body["next_cursor"]        # store durably here
        if not body["has_more"]:
            return cursor
```

Example `GET /jobs/{id}` response (abridged):

```json
{
  "id": 42, "title": "Senior Python Developer (m/w/d)",
  "company": {"id": 7, "name": "A GmbH", "domain": "a-firma.de", "country": "DE"},
  "status": "open", "published_at": "2026-11-01",
  "geo_status": "resolved",
  "locations": [{"id": 9, "country": "DE", "country_name": "Germany", "region": "DE-SN", "region_name": "Sachsen",
                 "city": "Dresden", "postcode": null, "is_primary": true, "confidence": 0.9, "source": "gazetteer"}],
  "work_model": "HYBRID", "remote_eligibility": {"scope": "unknown", "countries": []},
  "employment_types": ["FULL_TIME"], "seniority": "SENIOR",
  "specialization": {"id": 10, "slug": "backend-development", "name": "Backend development", "group": "information-technology", "is_it": true, "confidence": 0.8},
  "skills": [{"name": "Python", "category": "language"}],
  "languages": [{"language": "de", "level": "C1", "requirement": "required", "descriptor": null, "evidence": "Deutsch C1 erforderlich", "confidence": 0.9}],
  "salary": {"min": 60000, "max": 75000, "currency": "EUR", "period": "year", "basis": "unknown", "raw": "60.000 - 75.000 € pro Jahr", "confidence": 0.9},
  "quality": {"score": 0.82, "completeness": 0.85, "missing": ["date_posted"], "field_confidence": {}}
}
```

Example `GET /changes`:

```json
{"items": [{"event_id": 118, "event_type": "job.updated", "job_id": 42, "occurred_at": "2026-11-10T09:15:02Z", "fields": ["salary"]},
           {"event_id": 119, "event_type": "job.closed", "job_id": 17, "occurred_at": "2026-11-10T09:15:03Z", "reason": "missed"}],
 "next_cursor": "eyJ2IjoxLCJlIjoxMTl9", "has_more": false}
```

Errors: 401 (token), 422 (invalid filter: unknown skill/country/level, `salary_min` without `currency`, foreign
cursor), 429 (`Retry-After`), 503 (bot API disabled or misconfigured), `/changes`: 400 invalid cursor, 409 cursor ahead of the log.

The `/api/jobs` endpoints above also accept the additive filters `country_code`, `region`, `specialization`,
`work_model` and `seniority` (repeatable), and return `country_codes`, `region_codes`, `geo_status`, `work_model`,
`seniority`, `employment_kinds` per vacancy.
