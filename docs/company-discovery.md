# company_discovery: design notes

## Module boundaries

| Package | Owns | May import |
|---|---|---|
| `gcc_core` | `companies`, `company_sources`, `scan_requests`, `external_api_usage`; auth, settings, URL rules, SSRF-safe HTTP, companies API | nothing module-specific |
| `company_discovery` | `discovery_jobs`, `discovery_job_items`; regions, industries, Overpass, CSV, verification | `gcc_core` |
| `career_crawler` | `career_sources`, `jobs`, `job_skills`, `crawl_runs`, `crawl_pages`, `crawl_metrics`, `site_fingerprints`, `extraction_errors` ([career-crawler.md](career-crawler.md)) | `gcc_core` |

The only coupling between discovery and the crawler is data:

- discovery writes companies and inserts `scan_requests` (status `pending`) for verified websites;
- the crawler calls `gcc_core.scan_queue.claim(session, worker=…, limit=…)` (`FOR UPDATE SKIP LOCKED`, highest priority first) and `finish(session, request_id, ok=…, error=…)`;
- a partial unique index guarantees at most one open request per company. Manual requests have priority 10, discovery 0.

The core dispatches website verification by Celery task **name** (`company_discovery.tasks.verify_companies`), so it never imports module code.

## Data model (migration `0001`)

```
companies            one row per employer; domain UNIQUE (registrable domain) = dedup key
  ├─ company_sources one row per (source, source_record_id): provenance, licence, attribution,
  │                  whitelisted raw data, data timestamp, first/last seen, discovery job
  └─ scan_requests   outbox for the career crawler
discovery_jobs       osm | csv; params, stats, errors, provenance of the whole job, file SHA-256
  └─ discovery_job_items  per input record: new | updated | duplicate_existing | duplicate_in_job |
                          invalid_url | no_website | error
external_api_usage   daily request/byte counters per external service (Overpass budget)
```

## Matching (insert or update)

For each record, in this order:

1. same `(source, source_record_id)` seen before → **updated** (re-runs never create duplicates);
2. same registrable domain → **duplicate_existing**, or **duplicate_in_job** if this job already had it;
3. no usable website → same normalized name (legal forms removed, umlauts folded) **and** same postcode (or city) → existing company; otherwise a new company without domain;
4. otherwise → **new**.

Later sources never overwrite existing values: they only fill empty fields, merge industries and add alternative names. Companies marked `is_manual` keep all their values. A company without a website gets one if a later source supplies a domain nobody else uses yet.

Domain key: eTLD+1 from the Public Suffix List snapshot bundled with `tldextract`, including private suffixes (so `firma.wixsite.com` ≠ `other.wixsite.com`). A short list of shared-hosting domains not in the PSL (`jimdosite.com`, `wordpress.com`, …) is keyed by full host. Links to social networks and directories (Facebook, XING, Gelbe Seiten, …) are rejected as `not_company_website`.

## Verification states

`verified` → may be queued. `redirected` (to another company's domain or a non-company site), `robots_disallowed`, `access_restricted`, `parked`, `blocked_address`, `no_website` → never crawled. `unreachable`, `dns_error`, `rate_limited`, `http_error`, `robots_unavailable` → retried with back-off 1 h, 4 h, 16 h, 64 h, then weekly. Verified sites are re-checked after `VERIFY_RECHECK_DAYS`.

When a verified homepage redirects to a different domain that no other company uses, the company adopts the new domain and keeps the old one in `verification_detail.previous_domain`.

## Overpass queries

One query per Kreis (leaf region):

```
[out:json][timeout:180][maxsize:268435456];
(
  area["boundary"="administrative"]["admin_level"="6"]["de:regionalschluessel"~"^14612"];
  area["boundary"="administrative"]["admin_level"="6"]["de:amtlicher_gemeindeschluessel"~"^14612"];
)->.a;
.a out ids;                       // lets the client detect "boundary not found"
(
  nwr["office"~"^(it|software|telecommunication)$"]["name"][~"^(website|contact:website)$"~"."](area.a);
  nwr["man_made"="works"]["name"][~"^(website|contact:website)$"~"."](area.a);
  …
);
out tags center;
```

The same selector definitions (`industries.py`) render the query and classify each result in Python. Adjusting a selector therefore changes both consistently.

## Adding another federal state

1. Create `company_discovery/regions/<state>.py` with a `Region(kind="state", code="DE-XX", children=(…Kreise with 5-digit Kreisschlüssel…))`.
2. Add it to `STATES` in `regions/__init__.py`.

Nothing else changes: the API, the dashboard (region selector), the query builder and CSV region matching all read the registry. Stadtstaaten (Berlin, Hamburg, Bremen) have no admin_level=6 Kreise in OSM. Give them a single leaf with `kind="state"`, which uses the `ISO3166-2` area selector.

## ODbL obligations (summary, not legal advice)

- Attribution "© OpenStreetMap contributors" with a link to https://www.openstreetmap.org/copyright wherever OSM-derived data is shown or exported. The dashboard footer, the CSV export and `company_sources.attribution` do this.
- The `companies` table built from OSM is a derivative database. Private, internal use is fine. If you make it (or a substantial extract) available to the public, it must be offered under the ODbL (share-alike). Because OSM-derived records are tagged per source, that part can be extracted on its own.
- Mixing in directory data: keep each directory's licence in its job and source records, and check it before any publication.
