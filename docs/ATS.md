# ATS adapters and search-aware crawling (v3)

This extends the **existing** crawler (one engine, one safe HTTP client, one job store and lifecycle). Nothing here
starts a second crawler, and Telegram/bot users never trigger external requests: the bot API reads PostgreSQL only.

## 1. Evidence baseline (ATS diagnostics report, 2026-11-10)

Only 3 of the 10 attempted requests produced an API response.

| Source | Result | Decision | What the code does |
|---|---|---|---|
| Greenhouse N26 | HTTP 200, 323,515 B, 34 postings, `meta.total` 34, detail probe valid | integrate | Live-validated. Enabled. |
| Greenhouse Bitpanda | HTTP 200, 619,708 B, 45 postings, `meta.total` 45, duplicate title "AML Operations Associate", detail probe valid | integrate | Live-validated. Duplicate titles stay separate postings (key = job id). |
| Lever official example account | HTTP 200, `[]` (2 B) | verify, empty | Reachable, but **no non-empty live Lever board has been seen**. Status `live_empty_only`. |
| SmartRecruiters BoschGroup, DeliveryHero | robots.txt disallows the API URL | exclude | Adapter written from documented structures + fixtures. **Disabled**; robots.txt still refuses if you enable it. |
| Ashby deepl, smallpdf | robots.txt answered HTTP 401 | exclude (blocked) | Adapter written from documented structures + fixtures. **Disabled**; a 401/403 robots.txt on this host is treated as disallow-all. |
| Eightfold Qualcomm | robots.txt disallows | exclude | No adapter. |
| Workday, Phenom, SuccessFactors, Tesla | not configured | - | No adapter (no verified tenant/endpoint). |

The fixtures in `backend/tests/fixtures/ats/` follow the **documented** public structures of each provider; the report
only contained counts and verdicts, not payloads. A fixture proves parsing, never access: that is why a status is only
upgraded by a real, non-empty live validation.

## 2. Provider status (`career_crawler/ats/capabilities.py`)

| Provider | Catalogue | Search API | `access_status` | Enabled by default |
|---|---|---|---|---|
| personio | XML, all positions | no | `existing_unreviewed` (used before v3, not part of the report) | yes |
| greenhouse | one JSON request (`content=true`) | no | `live_validated` | yes |
| lever | JSON, `skip`/`limit` pages | no (structured filters only) | `live_empty_only` | yes (existing behaviour) |
| smartrecruiters | JSON, `limit`/`offset` pages | yes: `q`, `country`, `region`, `city` (all unverified live) | `blocked` | **no** |
| ashby | one JSON response | no | `blocked` | **no** |

`live_ready` is true only for `live_validated`. Enable providers explicitly with
`CRAWLER_ATS_ENABLED_PROVIDERS`; a detected account of a disabled provider is recorded as a source with error
`provider_disabled` and **no request is sent**. Robots.txt is always enforced by the shared client.

### What changed in the adapters
* **Greenhouse**: completeness requires `meta.total == len(jobs)` (otherwise the scan is partial); offices become
  structured locations; metadata (employment type, remote policy, multi-select lists) is mapped; `pay_input_ranges`
  becomes the salary when the employer published it; `language`/`requisition_id`/`internal_job_id` are kept; repeated
  ids in one payload are collapsed; if the `content=true` body exceeds the byte limit the board list is read without
  bodies (still a complete list) instead of failing.
* **Lever**: ids are deduplicated across overlapping pages, the posting `country` qualifies a single location,
  `salaryDescription` text reaches the salary parser, page cap hit => not complete. Validate it live with the probe (§6).
* **SmartRecruiters**: catalogue paging with total check and cap; the list response has **no description**, so
  postings carry `detail_pending` and confidence 0.85 (one request per posting is intentionally not made).
* **Ashby**: unlisted jobs are skipped, `compensation.summaryComponents` becomes the salary, secondary locations and
  `workplaceType`/`isRemote` are mapped.

## 3. Search-aware crawling

```
account detected (evidence only) ─▶ provider enabled? ─no─▶ record source "provider_disabled", stop
                                        │yes
                         select_strategy(capabilities)
        catalogue available ─▶ read the catalogue ─▶ capped? ─yes, search API + search enabled─▶ + bounded search
        no catalogue, search API ─▶ bounded search (only if CRAWLER_ATS_SEARCH_ENABLED=true)
        neither ─▶ source "search_disabled"/no strategy, nothing fetched
```

* **Capabilities** declare catalogue / search parameters / which parameters are unverified.
* **Parameter validation** (`SearchSpec.validate`): only parameters the provider supports; text length limits,
  no control characters, country = two letters; a query that fails validation is recorded and never sent.
* **Planner** (`SearchPlanner`): breadth-first and budgeted. Base queries = countries × keywords
  (`CRAWLER_ATS_SEARCH_COUNTRIES`, `..._KEYWORDS`); a query whose provider total exceeds what can be paged
  (`min(window, pages × page size)`) is *saturated* and is split by city (DE/AT/CH city lists), once. Hard limits:
  `..._MAX_QUERIES` per account and scan, `..._MAX_PAGES` per query, `..._MAX_JOBS` per account and scan.
* **Executor** (`run_search`): shared SSRF-safe fetcher (robots, throttle, size limit); short page or reached total ends
  a query; postings are deduplicated by provider job id across queries and against the catalogue part; a robots
  refusal aborts, a 429/503 keeps what was found and stops the scan politely (`Retry-After` honoured), three failing
  queries in a row abort.
* **Completeness**: a search result is **always `complete=false`**. The lifecycle counts misses only for complete reads,
  so a failed, rate-limited, empty, saturated or narrowly filtered search cannot close a vacancy. Postings it does return are
  stored through the normal path (same dedup key as the catalogue, `job.created` events, `last_seen_at`).
* **Persistence** (migration `0004`, table `career_sources`): `scan_strategy`, `scan_complete`, `reported_total`,
  `scan_detail` (queries planned/run/failed/saturated, skipped by budget, per-query rows, caps), `last_scan_at`,
  `last_complete_scan_at`.

The mock provider (`career_crawler/ats/mocksearch.py`, registered only by tests) serves a search API through the real
fetcher, so keyword/location search, pagination, saturation/refinement, dedup, failures, 429 and robots are tested end to
end without contacting a restricted provider.

## 4. Admin diagnostics

* `GET /api/crawler/ats/providers`: per provider capabilities, `access_status` + evidence, `live_ready`, `enabled`,
  `observed_access` (`ok` | `failing` | `robots_blocked` | `not_fetched` | `no_data`), source counts (ok / complete / partial),
  strategies, open jobs, jobs in the last scans, last (complete) scan, top errors; plus the active ATS settings.
* `GET /api/crawler/ats/sources?provider=&only=failing|incomplete|disabled|capped|search`: the sources behind the numbers.

Both require the administrator login. The bot API (`/api/bot/v1`) is unchanged.

## 5. Settings

See `.env.example` (`CRAWLER_ATS_*`). Defaults: providers `personio,greenhouse,lever`, search **off**, catalogue cap 2000.

## 6. Live validation procedure (before trusting a provider)

```bash
docker compose exec worker-crawl python -m career_crawler.ats.probe https://jobs.lever.co/<account-with-open-postings>
docker compose exec worker-crawl python -m career_crawler.ats.probe https://boards.greenhouse.io/<account> --save /tmp/raw
```
The probe uses the crawler's own client (robots.txt, delay, User-Agent), makes the minimum requests for one board and
prints the verdict: `validated_nonempty`, `valid_but_empty` (does **not** validate parsing), `partial`, `blocked`, `error`.
Compare the sample titles with the public page; with `--save` the raw bodies can become offline fixtures. Only after a
`validated_nonempty` run on a permitted board should the provider's `access_status` in `capabilities.py` be raised.
SmartRecruiters/Ashby should only be enabled after the operator has obtained permission (or the providers' robots.txt
allows the API) and has validated them this way.

## 7. Not done / unverified

* No live Lever board with postings has been validated; no live SmartRecruiters/Ashby request was possible.
* Greenhouse `pay_input_ranges`, Lever `salaryRange`/`workplaceType` and SmartRecruiters `q`/`country`/`city`/`releasedAfter`
  are implemented from documentation and fixtures only.
* SmartRecruiters descriptions need a per-posting request: not implemented.
* Workday, Phenom, SuccessFactors, Eightfold: not implemented (no verified tenant/endpoint or robots.txt disallows).
* The search keyword list is a flat configuration; it is not yet derived from the DACH taxonomy groups.

## 8. Deploy and rollback

```bash
git pull && docker compose up -d --build        # migration 0004 runs on API start (6 nullable columns, no rewrite)
docker compose exec api alembic current          # expect 0004
# rollback (code and schema)
docker compose exec api alembic downgrade 0003   # drops the six career_sources columns
git checkout <previous-tag> && docker compose up -d --build
```
Behaviour switches without a rollback: leave `CRAWLER_ATS_SEARCH_ENABLED=false` (default) and keep
`CRAWLER_ATS_ENABLED_PROVIDERS` at `personio,greenhouse,lever`.
