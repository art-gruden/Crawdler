"""Career-crawler settings (environment variables prefixed ``CRAWLER_``).

The defaults are the initial tuning for a 2 vCPU / 4 GB VPS. They are starting values
for the resource controller, not performance guarantees.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class CrawlerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CRAWLER_", env_file=".env", extra="ignore", case_sensitive=False)

    enabled: bool = True

    # --- concurrency (upper bounds; the resource controller moves below them) ---------
    http_concurrency: int = 16  # global, per HTML worker process
    http_concurrency_min: int = 2
    per_domain_concurrency: int = 1
    per_domain_delay_s: float = 2.0
    http_timeout_s: float = 15.0
    companies_in_parallel: int = 16  # company scans running at once in one HTML worker
    browser_pages: int = 1
    browser_contexts: int = 1

    # --- crawl budgets ------------------------------------------------------------
    max_pages_per_company: int = 50
    max_depth: int = 4
    max_html_bytes: int = 2 * 1024 * 1024
    max_feed_bytes: int = 8 * 1024 * 1024  # ATS XML/JSON feeds
    max_pdf_bytes: int = 5 * 1024 * 1024
    max_pdfs_per_company: int = 5
    max_redirects: int = 5
    max_external_ats_hops: int = 2  # redirects/links to an external ATS host followed per company
    max_sitemap_files: int = 3
    max_sitemap_urls: int = 20_000
    frontier_max: int = 400  # bounded in-memory frontier per company
    early_stop_after: int = 12  # pages fetched before the relevance check kicks in
    early_stop_min_ratio: float = 0.15  # stop when < 15 % of fetched pages were career-relevant
    scan_time_budget_s: int = 600  # one company
    batch_time_budget_s: int = 900  # one career_scan task (then the beat starts the next)

    # --- browser fallback -----------------------------------------------------------
    browser_enabled: bool = True
    browser_executable: str | None = None  # None: Playwright's bundled Chromium
    browser_page_timeout_s: float = 25.0
    browser_max_urls_per_company: int = 4
    browser_max_requests_per_page: int = 120
    browser_mem_pause_mb: int = 600  # pause new browser work below this host MemAvailable …
    browser_mem_resume_mb: int = 900  # … and resume above this (hysteresis)

    # --- optional LLM fallback (OpenAI-compatible endpoint, e.g. a local Ollama) -------
    llm_enabled: bool = False
    llm_url: str | None = None  # e.g. http://ollama:11434/v1/chat/completions
    llm_model: str = "qwen2.5:3b-instruct"
    llm_api_key: str | None = None
    llm_timeout_s: float = 60.0
    llm_max_chars: int = 12_000
    llm_max_calls_per_hour: int = 30

    # --- ATS providers and search-aware crawling ------------------------------------------
    # Only providers listed here are fetched. smartrecruiters/ashby are implemented from documented
    # structures and offline fixtures only; their robots.txt blocked the v3 diagnostics (docs/ATS.md).
    ats_enabled_providers: str = "personio,greenhouse,lever"
    ats_catalog_max_jobs: int = 2000  # catalogue larger than this is "capped" (never complete)
    ats_search_enabled: bool = False  # bounded keyword/location search for search-capable providers
    ats_search_keywords: str = "engineer,developer,data,product,devops,security,design,analyst,manager,sales,marketing"
    ats_search_countries: str = "DE,AT,CH"
    ats_search_max_queries: int = 40  # per account and scan
    ats_search_max_pages: int = 5  # per query
    ats_search_page_size: int = 100
    ats_search_max_jobs: int = 5000  # per account and scan

    # --- job lifecycle --------------------------------------------------------------
    close_after_misses: int = 3  # complete scans in a row without the posting …
    close_min_days: int = 3  # … and not seen for at least this many days

    # --- recrawl intervals (hours) ------------------------------------------------------
    interval_active_h: float = 18
    interval_normal_h: float = 60
    interval_no_jobs_h: float = 168
    interval_no_career_h: float = 21 * 24
    interval_max_h: float = 30 * 24
    schedule_batch: int = 200

    # --- recovery ----------------------------------------------------------------------
    claim_stale_minutes: int = 45  # a claimed request without heartbeat is re-queued
    max_scan_attempts: int = 3
    checkpoint_every_pages: int = 5
    resume_max_age_h: int = 6

    # --- storage / retention -------------------------------------------------------------
    keep_raw_html: bool = False  # debugging only
    raw_html_retention_days: int = 7
    raw_html_max_mb: int = 500
    page_retention_days: int = 120
    error_retention_days: int = 30
    metrics_retention_days: int = 30
    closed_job_retention_days: int = 365
    description_max_chars: int = 40_000

    # --- vacancy statistics and Telegram reports ----------------------------------------
    stats_timezone: str = "Europe/Berlin"  # default calendar for "today", day buckets and report periods
    stats_telegram_bot_link: str | None = None  # https://t.me/<bot>, shown in generated posts
    # Phase 4 (publishing to a channel). Off by default; the token is read from the environment only
    # (CRAWLER_STATS_TELEGRAM_BOT_TOKEN) and never returned by any endpoint or written to a log.
    stats_telegram_enabled: bool = False
    stats_telegram_bot_token: SecretStr | None = None
    stats_telegram_chat_id: str | None = None  # @channelname or numeric id
    stats_telegram_post_empty: bool = False  # allow publishing a report with zero new vacancies
    stats_telegram_min_interval_s: int = 5  # minimum gap between two publish attempts to one channel
    stats_telegram_timeout_s: float = 10.0

    # --- test mode ---------------------------------------------------------------------
    # Serve every request from local fixtures; never touches live employer websites.
    test_mode: bool = False
    fixtures_dir: str | None = None


@lru_cache
def get_crawler_settings() -> CrawlerSettings:
    return CrawlerSettings()
