# Vacancy statistics and Telegram reports

Admin tab **📊 Job Statistics** (`#/stats`), API under `/api/crawler/stats/jobs/*` (session login, same as the other
crawler dashboard routes; **not** part of the public Bot API). Code: `career_crawler/jobstats.py` (counting),
`statsreport.py` (Telegram text), `statspublish.py` (Phase 4), `stats_api.py` (routes).

## What is counted

| Term | Definition |
| --- | --- |
| **Newly discovered** | `jobs.discovered_at` lies in `[start, end)`. One `jobs` row = one canonical vacancy (`dedup_key` is unique, so the same posting seen through two URLs or two ATS feeds is one row). `discovered_at` is set on insert and never changed: rescans, updates, closures and reopenings never make a vacancy "new". |
| **Currently active** | `status = 'open'` as of the snapshot. |
| **Updated / closed / reopened** | distinct vacancies with a `job.updated` / `job.closed` / `job.reopened` event in the period (the event log exists since the DACH deployment; `events_since` in the summary shows the start). A vacancy can appear in several of these lines. |
| **Companies with new vacancies** | distinct companies among the newly discovered vacancies. |

"Discovered" means *found by our crawler*. It is **not** "published that day": the employer's own date
(`date_posted`) is reported only as a data-quality figure and the Telegram text never says "published".

### Periods and time zone

Default zone `Europe/Berlin` (`CRAWLER_STATS_TIMEZONE`; per request `timezone=`). Day boundaries are computed from local
dates, so DST days have 23 or 25 hours. Start is inclusive, end exclusive; the exact interval is shown on the page.

* **Today / yesterday**: one local calendar day. **Last 24 h**: rolling, ending at the snapshot (differs from "today").
* **7 d / 30 d**: the last 7 / 30 local calendar days *including today*.
* **Custom**: `from`..`to`, local dates, inclusive, at most 366 days.
* **Change %** compares with the previous period of the *same elapsed length* (a half-finished day against the same
  part of the previous day). `null` when the previous period is empty.
* **Snapshot**: the summary response carries `snapshot_at`; the dashboard sends it as `as_of` with every other request
  and in the Telegram preview, so cards, charts, tables and the post describe the same moment.

### Scope and geography

* "All DACH" = vacancies in DE/AT/CH **plus those without any country yet** (shown as "Unknown"). Vacancies located only
  outside DACH are left out. Filters: country, region/canton (ISO 3166-2), exact city name, specializations (a group
  includes its members, `unclassified` is selectable). Filters combine with AND; several values of one filter are OR.
* **Unique job breakdown** (default, used for Telegram): a vacancy with exactly one place counts there; with several
  places it is **Multi-location**; with none it is **Remote (no location)** (work model REMOTE) or **Unknown location**.
  Rows add up to the total. Places are keyed by country + region + city, so "Neustadt" in Saxony and in Austria are two
  rows. City aliases are resolved by the gazetteer when the vacancy is stored (München = Munich = Muenchen).
* **City coverage**: a vacancy counts in every place it lists; rows overlap and have no total.
* Specializations use each vacancy's **primary** specialization (rows add up to the total, NULL = *Unclassified*).
  The *Overlap* column also counts secondary specializations and is not additive. The taxonomy is the existing one
  (all 9 groups, not only IT). Specialization names in uk/ru posts stay in English (the taxonomy has en/de only).

### Data-quality notes (shown under the KPI cards)

* **Initial catalogue load**: when the crawler first visits a company, its whole existing catalogue becomes "new".
  The summary reports how many new vacancies belong to companies whose first vacancy was found in the period and warns
  at >= 50 %. The filter *Exclude initial loads* removes them from every statistic (note: the excluded set is
  evaluated per period start).
* **Events**: share of new vacancies with a `job.created` event; imported/backfilled rows have none and keep their
  original `discovered_at` (backfills never change it).
* Share with specialization, location and employer publication date.

## Telegram post

`POST /telegram-preview` builds the post from the same functions as the dashboard: headline total, optional country
line (only countries that have new vacancies, plus "+N in several or unspecified countries"), top specializations (not
"Unclassified"), top cities (single-city vacancies only), period, call to action, optional bot link, source line.
No numbers are estimated and no text is model-written. Languages uk/ru/en/de, top lists 3/5/10, formats `plain`
(paste into the Telegram app), `html` and `markdownv2` (bot/API; every text piece is escaped, bot links must match
`https://t.me/<name>`). An empty period yields a short "no new vacancies" text and is flagged `empty`.
The text never claims to cover the whole DACH market.

## Phase 4: publishing (implemented, **off by default**)

Enable with `CRAWLER_STATS_TELEGRAM_ENABLED=true`, `CRAWLER_STATS_TELEGRAM_BOT_TOKEN`, `CRAWLER_STATS_TELEGRAM_CHAT_ID`
(environment only; no endpoint returns them, errors are scrubbed). `POST /telegram-publish` requires `confirm: true`
and the `sha256` of the previewed text (a changed text is refused), publishes at most once per channel + period +
configuration (unique key; failed attempts can be retried), keeps a history (`/telegram-publications`: text, status,
Telegram's answer, message id), honours Telegram's `retry_after` without retrying automatically, enforces a minimum gap
between attempts, and refuses empty reports unless `CRAWLER_STATS_TELEGRAM_POST_EMPTY=true`.
**Not implemented**: scheduled/automatic posting (nothing is ever posted without a confirmed API call) and PNG export.
Publishing was tested against a mocked Telegram API only, not against the live service.
Note: for the rolling `last_24h` period every snapshot is a new period, so duplicate prevention does not apply; use a
calendar period for channel posts.

## Database

Migration `0005`: indexes `ix_job_events_type_time (event_type, occurred_at)` and `ix_jobs_company_discovered
(company_id, discovered_at)`, table `stat_publications`. Nothing is rewritten; `alembic downgrade 0004` removes them.
All counting is SQL aggregation (FILTER aggregates, one pass per table); no caching layer. Measured on 50,000
vacancies (2 vCPU sandbox, PostgreSQL 16): summary 0.1-0.4 s, specialization table 0.1 s, city table 0.25 s
(coverage mode 0.6 s), matrix 0.2 s, trends 0.04 s; with *Exclude initial loads* the summary takes about 1.7 s.

## Known limitations

* *Active* is "open now"; for an `as_of` in the past it is approximated by `discovered_at < as_of`.
* Updated/closed/reopened are only as complete as the event log (see `events_since`).
* The unique city view uses `jobs.location_ids`, which is kept in step with `job_locations` by the normaliser.
* Vacancies without a resolvable location are listed as Unknown, not guessed.
