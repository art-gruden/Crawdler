# DACH job data platform

The crawler stores vacancies for Germany, Austria and Switzerland in a normalized form and serves them to
machine clients (the planned Telegram bot, scripts) through a **read-only, versioned API** with a durable
change feed. The dashboard and its APIs keep working as before.

Contents: [Data model](#data-model) · [Normalization rules](#normalization-rules) ·
[Search semantics](#search-semantics) · [Bot API](#bot-api) · [Change feed](#change-feed) ·
[Authentication](#authentication) · [Upgrade and backfill](#upgrade-and-backfill) ·
[Indexes and performance](#indexes-and-performance) · [Limits and open work](#limits-and-open-work)

## Data model

| Table | Purpose |
|---|---|
| `geo_countries`, `geo_regions` | Seeded reference data: DE 16 Länder, AT 9 Bundesländer, CH 26 cantons (ISO 3166-2 codes) |
| `geo_locations` | One row per `country │ region │ folded city`; a gazetteer of ~180 DACH cities with multilingual aliases (München/Munich/Muenchen, Wien/Vienna, Zürich/Zurich, Genève/Geneva …) |
| `job_locations` | The **work locations of a vacancy** (several allowed), each with `confidence` and `source` (`structured`, `gazetteer`, `text`, `context`, `legacy`) |
| `company_locations` | Headquarters / offices of a company — kept apart from where a vacancy is worked |
| `jobs.country_codes`, `region_codes`, `location_ids` | Denormalized arrays (GIN-indexed) used by the filters |
| `jobs.geo_status` | `resolved`, `partial`, `ambiguous`, `unknown` — never silently "Germany" |
| `jobs.remote_scope`, `remote_countries` | Remote **eligibility** (`unknown`, `worldwide`, `europe`, `eu`, `dach`, `countries`), separate from `work_model` |
| `specializations`, `job_specializations` | Two-level taxonomy (9 groups, ~75 specializations), primary + secondary with confidence |
| `job_languages` | Language requirements: CEFR level only when written, `requirement` required / preferred / unknown, evidence, confidence |
| `job_events` | Transactional outbox for `job.created`, `job.updated`, `job.closed`, `job.reopened` |

Rules that matter:

* **A country is assigned only from evidence**: an explicit country/region name, a structured address field, or a
  gazetteer city that exists in one country only. A five-digit postcode is *not* evidence (01067 is not "Germany").
  When a name exists in several countries (Rheinfelden DE/CH, Neustadt, Buchs) and nothing disambiguates it, the
  status is `ambiguous`. The employer's country is used only to break such ties.
* Frankfurt and Freiburg default to the larger city (Main / im Breisgau) with confidence 0.6; "Freiburg, Schweiz"
  resolves to Fribourg.
* `work_model` (`REMOTE`/`HYBRID`/`ONSITE`/`UNKNOWN`) says *how* people work; `remote_eligibility` says *from where*.
  "Remote" alone is `unknown` eligibility; "Remote in Austria" is `countries: [AT]`.
* **Missing is not negative.** An unknown salary, level or seniority is `null`/`UNKNOWN` and is reported as such.

## Normalization rules

* **Specialization** is decided by the title; skills alone need at least three distinct skills and are capped at
  confidence 0.4. **Seniority** comes from the title only (`INTERN`, `JUNIOR`, `MID`, `SENIOR`, `LEAD`, `EXECUTIVE`, else `UNKNOWN`).
* **Employment types**: `FULL_TIME PART_TIME CONTRACT FREELANCE TEMPORARY INTERNSHIP APPRENTICESHIP WORKING_STUDENT OTHER UNKNOWN`
  (several may apply). The legacy `employment_types` column is still filled.
* **Languages**: a CEFR level is stored only when the posting writes one (`B2`, `C1`, a range gives its lower
  bound). "fließend", "good", "native" are kept as `descriptor`, never converted into a level. The requirement
  (`required`/`preferred`) is read from the same clause, so "Französisch fließend, Italienisch ist ein Plus" does not mix.
* **Salary**: a currency sign or ISO code is required — **no EUR is assumed** (Swiss postings use CHF). Period
  (`year`/`month`/`hour`) and `gross`/`net`/`unknown` are stored with the raw text and a confidence. Nothing is
  converted between currencies or periods.
* **Quality**: `quality.completeness` and `quality.missing` list which fields are unknown.

## Search semantics

`GET /api/bot/v1/jobs` — filters combine with **AND across categories** and **OR within one** (`country=DE,AT`).

| Filter | Notes |
|---|---|
| `country`, `region`, `city` | ISO 3166-1 / ISO 3166-2 / location ids from `/facets/cities`. Combining levels requires them to match **the same work location** |
| `specialization` | slug or id; a group (e.g. `information-technology`) includes its members; primary or secondary |
| `q`, `exclude` | PostgreSQL full text (`simple` configuration): `"exact phrase"`, `-word`, `or` |
| `work_model`, `employment`, `seniority` | enumerations above |
| `skills` + `skills_mode=any\|all` | canonical names from `/facets/skills` (case-insensitive, common aliases); unknown name → 422 |
| `languages=de:B2,en:C1` + `lang_mode` | the languages the candidate speaks (level optional). Per vacancy: `compatible`, `unknown` or `exceeds`. `inclusive` (default) keeps compatible + unknown, `strict` keeps compatible only |
| `salary_min` + `currency` (+ `salary_period`, default `year`) | currency is mandatory; salaries are never converted |
| `new_since`, `employer`, `status` (`open` default), `remote_in` | `remote_in=AT` = remote employee living in Austria |
| `include_unknown=seniority,salary,…` | vacancies whose value is unknown match that filter only if named here |
| `sort=newest\|relevance\|salary_desc` | `salary_desc` needs `currency` |

**Relevance** is deterministic: an integer `round(10 · Σ weight · component)` over specialization, text rank,
skills, location, work model, language fit and recency. Weights default to 25/20/20/15/8/7/5 and can be set with
`BOT_RANK_WEIGHTS`. Recency is frozen at the first page's timestamp (stored in the cursor), so paging is stable.

**Pagination** is keyset based: `limit` (≤ 100) and an opaque `cursor`. A cursor from a different query is
rejected with 422. Results do not skip or repeat rows when data changes between pages.

## Bot API

Base path `/api/bot/v1`, GET only, OpenAPI at `/api/openapi.json` (tag `bot-api`). Responses expose a public schema:
no dedup keys, extraction method, miss counters, content hashes or raw source values.

| Path | |
|---|---|
| `/jobs` | search (above); `items[]`, `next_cursor`, `has_more` |
| `/jobs/{id}` | one vacancy with full description, requirements, responsibilities |
| `/facets/countries` | DE/AT/CH (and others if present) with open-vacancy counts |
| `/facets/regions?country=DE` | all 16 / 9 / 26 regions with counts |
| `/facets/cities?country=&region=&q=&limit=` | cities with open vacancies; `q` is a prefix, umlauts and `ae/oe/ue` interchangeable |
| `/facets/specializations` | the taxonomy tree with counts |
| `/facets/skills?q=&limit=` | skills with counts |
| `/changes?cursor=&limit=` | change feed |
| `/changes/head` | a cursor positioned after the newest event |

Examples: [API.md](API.md#bot-api-apibotv1).

## Change feed

Events are written to `job_events` **in the same transaction** as the change (outbox). Guarantees:

* **Atomic** — a rollback discards the events; a commit publishes state and events together.
* **Ordered by commit** — `event_id` follows commit order. A sequence alone would not: two writers can take ids in
  one order and commit in the other, and a reader could then skip an event forever. The events are therefore
  inserted in a `before_commit` hook right after a transaction-scoped advisory lock, held until COMMIT.
* **Replayable and idempotent** — events are never changed or deleted. Re-reading with the same cursor returns the
  same page; consumers de-duplicate on `event_id` (delivery is at-least-once). No Redis pub/sub is involved.

| Event | Emitted when |
|---|---|
| `job.created` | a canonical vacancy is seen for the first time (rescans, duplicates in one scan: never again) |
| `job.updated` | meaningful change only: title, apply URL, work model, employment, salary, locations, remote eligibility, a newly added language, or a substantial description change (similarity < 90 % or length change > 15 %). `fields` lists what changed. Refreshing `last_seen`/`last_checked` emits nothing |
| `job.closed` | the existing conservative closure (complete scans, repeated misses, minimum age) or the source's own expiry date; `reason` = `missed` / `expired`. A failed or partial scan never closes anything |
| `job.reopened` | a closed vacancy that is reliably seen again (an expired posting whose `validThrough` is still in the past is not reopened) |

Response: `{items:[{event_id, event_type, job_id, occurred_at, fields?, reason?}], next_cursor, has_more}`.
`next_cursor` is always present; on an empty poll it equals the cursor you sent. Persist it **after** processing a
page. An invalid cursor is 400; a cursor beyond the log (database restored from an older backup) is 409 — start again
from `/changes/head` and re-synchronise with `/jobs`.

## Authentication

Bearer token, separate from the administrator login: an admin session does **not** open `/api/bot/v1`, and a bot
token does not open any admin route. The admin password is never reused.

```bash
docker compose exec api python -m gcc_core.botauth generate telegram   # prints the token once + the .env line
# paste the line (name:sha256:<hex>) into .env as BOT_API_KEYS, then:
docker compose up -d
```

* Only the SHA-256 digest needs to be stored; comparison is constant time over all keys.
* Rotation: several entries may be active; add the new one, switch the client, remove the old one.
* Rate limit per key (`BOT_RATE_LIMIT_PER_MIN`, headers `X-RateLimit-*`, `Retry-After` on 429) and a limit on failed
  attempts per client address. The token is never logged; the key *name* is.
* No key configured → 503 "disabled"; an unparsable setting → 503 (details only in the server log).
* `/api/public/bot` stays public.

## Upgrade and backfill

`alembic upgrade head` (runs automatically when the API container starts) applies revision `0003`: new tables and
columns, seed of the geography/taxonomy, then an **idempotent backfill** of existing vacancies and companies
(Sachsen data included) in plain SQL:

* no events are emitted, no rows are duplicated (`loc_key` and primary keys), re-running changes nothing;
* old `country = 'DE'` values become a weak location (confidence 0.5, source `legacy`) because the old code assumed
  Germany; a legacy `EUR` salary without evidence is cleared, a currency found in the raw text wins;
* `python -m career_crawler.backfill` re-runs it by hand (rows with `norm_version` below the current one).

After the upgrade the first rescans may emit `job.updated` for vacancies whose fresh extraction differs slightly
from the backfilled values. Bots should treat `job.updated` as "re-read the vacancy".

## Indexes and performance

GIN on `country_codes`, `region_codes`, `location_ids`, `employment_kinds`, `remote_countries`; partial B-tree indexes
on open vacancies (`discovered_at`, work model, specialization, seniority); `(skill, job_id)`, salary and
`last_checked_at` indexes. Reproduce the plan check on a scratch database:

```bash
cd backend && PYTHONPATH=. DATABASE_URL=postgresql+psycopg://…/scratch python scripts/dach_explain.py --jobs 100000
```

Measured on 100 000 synthetic vacancies (local PostgreSQL 16, not the production VPS): country+region 1 ms,
specialization group 0.6 ms, text + work model 0.3 ms, skills ALL 14 ms, language fit 0.2 ms, salary_min + salary sort
36 ms (sequential scan; fine at this size), relevance with text + skills + language 130 ms.

## Limits and open work

* No real-site testing was done for DACH; extraction was verified on fixtures only.
* Discovery is still Sachsen/Germany-centred (Phase 3: AT/CH/DE-wide discovery, DACH CSV imports, more FR/IT extraction).
* Full-text search uses the `simple` configuration: German compounds ("Softwareentwickler") are not split, so
  `q=entwickler` will not find them; use the specialization filter. Ambiguity is detected only among gazetteer names;
  a small unknown town in the wrong country cannot be flagged.
* Phase 4 (taxonomy expansion, ranking tuning, quality dashboards) is not done.
