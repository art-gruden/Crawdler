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
| GET | `/api/jobs/{id}` | full posting: plain-text description, requirements, responsibilities, languages, salary, source values, extraction method/confidence |
| GET | `/api/jobs/skills?limit=40` | most frequent skills in open postings |
| GET | `/api/crawler/stats?hours=24` | dashboard figures: active/completed scans, domains per hour, pages per second, open/new postings, 429/503 share, latency, extraction success/failure per method, queues, latest CPU/RAM sample per worker |
| GET | `/api/crawler/timeseries?hours=24&bucket_minutes=15` | pages, new postings, errors, 429/503, companies, CPU, MemAvailable, browser pages, HTTP limit per bucket |
| GET | `/api/crawler/runs?status=running\|done\|partial\|failed\|abandoned&limit=50` | company scans |
| GET | `/api/crawler/problem-domains` | slow or failing sites (error streak, latency, rate limit, last error) |
| GET | `/api/crawler/companies/{id}` | diagnosis in plain words, site fingerprint (strategy, success rates, ATS, career URLs, schedule), career sources with evidence, last runs, errors, career pages |
| POST | `/api/crawler/companies/{id}/rescan` body `{"full": false}` | queue with manual priority; `full: true` also forgets the cached strategy/ATS (next scan runs full discovery). 409 if the website is not verified. |

Example:

```bash
curl -s -b cookies 'https://crawdler.duckdns.org/api/jobs?q=kubernetes&city=Dresden&new_days=7' | jq '.items[] | {title, company_name, date_posted, skills}'
```
