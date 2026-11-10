export interface Region {
  code: string;
  name: string;
  kind: "state" | "kreisfreie_stadt" | "landkreis";
  state_code: string;
  seat: string | null;
  children: Region[];
}

export interface Industry {
  id: string;
  label: string;
  description: string;
  default: boolean;
}

export interface ImportProfile {
  id: string;
  label: string;
  source: string;
  license: string | null;
  attribution: string | null;
  source_url: string | null;
}

export interface Job {
  id: number;
  kind: "osm" | "csv";
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  params: Record<string, unknown> & { region_names?: string[]; industries?: string[]; current_region?: string; source_name?: string };
  stats: Record<string, number>;
  errors: { region?: string; name?: string; line?: number; record?: string; error: string }[];
  error: string | null;
  cancel_requested: boolean;
  source: string;
  license: string | null;
  attribution: string | null;
  source_url: string | null;
  data_timestamp: string | null;
  file_name: string | null;
  requested_by: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface JobItem {
  id: number;
  company_id: number | null;
  outcome: string;
  name: string | null;
  domain: string | null;
  domain_display: string | null;
  website_raw: string | null;
  source_record_id: string | null;
  region_code: string | null;
  detail: string | null;
}

export interface Company {
  id: number;
  name: string;
  website_url: string | null;
  domain: string | null;
  domain_display: string | null;
  website_raw: string | null;
  website_rejected_reason: string | null;
  verification_status: string;
  verification_confidence: number | null;
  verified_at: string | null;
  street: string | null;
  postcode: string | null;
  city: string | null;
  district_key: string | null;
  district_name: string | null;
  state_code: string | null;
  lat: number | null;
  lon: number | null;
  industries: string[];
  scan_status: string;
  last_scan_at: string | null;
  first_seen_at: string;
  last_seen_at: string;
  sources: string[];
}

export interface SourceRecord {
  source: string;
  source_record_id: string;
  source_url: string | null;
  name: string | null;
  license: string | null;
  attribution: string | null;
  first_seen_at: string;
  last_seen_at: string;
  data_timestamp: string | null;
  discovery_job_id: number | null;
  data: Record<string, unknown> | null;
}

export interface CompanyDetail extends Company {
  alt_names: string[];
  verification_detail: Record<string, unknown>;
  source_records: SourceRecord[];
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface Usage {
  requests: number;
  bytes: number;
  max_requests: number;
  max_bytes: number;
  last_request_at: string | null;
  min_interval_s: number;
}

// --- career crawler ---------------------------------------------------------------

export interface Vacancy {
  id: number;
  company_id: number;
  company_name: string;
  company_domain: string | null;
  title: string;
  city: string | null;
  postcode: string | null;
  country: string | null;
  location_raw: string | null;
  remote_type: string;
  employment_types: string[];
  date_posted: string | null;
  discovered_at: string;
  last_seen_at: string;
  status: string;
  source_url: string;
  application_url: string | null;
  extraction_method: string;
  confidence: number;
  salary_min: number | null;
  salary_max: number | null;
  salary_currency: string | null;
  salary_period: string | null;
  skills: string[];
}

export interface VacancyDetail extends Vacancy {
  career_page_url: string | null;
  canonical_url: string | null;
  description: string | null;
  requirements: string[];
  responsibilities: string[];
  languages: { language: string; level: string | null; required: boolean; raw?: string }[];
  salary_raw: string | null;
  valid_through: string | null;
  closed_at: string | null;
  miss_count: number;
  ats_type: string | null;
  ats_job_id: string | null;
  source_values: Record<string, unknown>;
}

export interface MethodCounts {
  ok: number;
  fail: number;
  jobs: number;
}

export interface WorkerSample {
  worker: string;
  at: string;
  cpu_pct: number | null;
  mem_available_mb: number | null;
  container_mem_mb: number | null;
  http_limit: number | null;
  http_active: number | null;
  browser_active: number | null;
  browser_paused: boolean | null;
}

export interface CrawlerStats {
  window_hours: number;
  active_scans: number;
  completed_scans: number;
  outcomes: Record<string, number>;
  companies_per_hour: number;
  pages_per_second: number;
  pages: number;
  requests: number;
  mb_downloaded: number;
  avg_latency_ms: number | null;
  rate_429_503: number;
  fetch_errors: number;
  jobs_open: number;
  jobs_total: number;
  jobs_new: number;
  browser_pages: number;
  extraction_by_method: Record<string, MethodCounts>;
  open_jobs_by_method: Record<string, number>;
  queues: { scan_pending: number; scan_claimed: number; browser_pending: number; llm_pending: number };
  workers: WorkerSample[];
}

export interface TimePoint {
  t: string;
  pages: number | null;
  jobs_new: number | null;
  errors: number | null;
  rate_limited: number | null;
  companies: number | null;
  cpu_pct: number | null;
  mem_available_mb: number | null;
  browser_pages: number | null;
  http_limit: number | null;
}

export interface CrawlRunRow {
  id: number;
  company_id: number;
  company: string;
  domain: string | null;
  status: string;
  outcome: string | null;
  strategy: string | null;
  started_at: string;
  finished_at: string | null;
  pages_fetched: number;
  jobs_found: number;
  jobs_new: number;
  jobs_closed: number;
  complete: boolean;
  browser_pending: boolean;
}

export interface ProblemDomain {
  company_id: number;
  company: string;
  domain: string;
  error_streak: number;
  avg_latency_ms: number | null;
  rate_limited_until: string | null;
  last_error: { kind: string; message: string | null } | null;
  next_scan_at: string | null;
}

export interface CompanyCrawl {
  company: { id: number; name: string; domain: string | null; website_url: string | null; verification_status: string; scan_status: string; last_scan_at: string | null };
  diagnosis: { code: string; text: string };
  fingerprint: null | {
    best_strategy: string | null;
    strategy_stats: Record<string, { attempts: number; ok: number; jobs: number; rate?: number }>;
    ats_type: string | null;
    ats_account: string | null;
    career_urls: string[];
    needs_browser: boolean;
    technologies: string[];
    avg_latency_ms: number | null;
    interval_h: number | null;
    next_scan_at: string | null;
    last_success_at: string | null;
    open_jobs: number;
  };
  sources: { id: number; url: string; kind: string; ats_type: string | null; last_success_at: string | null; last_error: string | null; last_job_count: number | null; evidence: Record<string, unknown> }[];
  runs: { id: number; status: string; outcome: string | null; strategy: string | null; started_at: string; pages_fetched: number; jobs_found: number; jobs_new: number; complete: boolean; browser_pending: boolean }[];
  errors: { at: string; stage: string; kind: string; url: string | null; message: string | null; http_status: number | null }[];
  career_pages: { url: string; page_type: string; http_status: number | null; jobs_found: number; strategy: string | null; fetched_at: string }[];
  jobs: Record<string, number>;
}

// --- vacancy statistics -------------------------------------------------------------------------

export interface StatPeriod {
  key: string;
  timezone: string;
  start: string;
  end: string;
  counted_until: string;
  local_start: string;
  local_end: string;
  first: string;
  last: string;
  label: string;
  calendar_days: boolean;
  in_progress: boolean;
  previous: { start: string; end: string };
}

export interface StatKpi {
  key: string;
  new: number;
  previous: number;
  change_pct: number | null;
  companies: number;
  period: StatPeriod;
}

export interface StatSummary {
  snapshot_at: string;
  period: StatPeriod;
  new: { total: number; previous: number; change_pct: number | null; companies: number };
  active: number;
  updated: number;
  closed: number;
  reopened: number;
  kpis: StatKpi[];
  data_quality: {
    new_total: number;
    with_created_event: number;
    created_event_pct: number | null;
    initial_load: number;
    initial_load_pct: number | null;
    with_source_date: number;
    with_source_date_pct: number | null;
    classified: number;
    classified_pct: number | null;
    with_location: number;
    with_location_pct: number | null;
    events_since: string | null;
    warnings: { code: string; message: string }[];
  };
}

export interface StatTrends {
  period: StatPeriod;
  bucket: "day" | "hour";
  points: { t: string; label: string; count: number }[];
  total: number;
  empty: boolean;
}

export interface StatSpecRow {
  key: string;
  id: number | null;
  slug: string;
  name: string;
  name_de: string;
  group: string | null;
  is_it: boolean;
  is_group: boolean;
  sel: number;
  prev: number;
  change_pct: number | null;
  today: number;
  "7d": number;
  "30d": number;
  active: number;
  companies: number;
  sources: number;
  overlap: number | null;
}

export type StatTotals = { sel: number; prev: number; today: number; "7d": number; "30d": number; active: number; change_pct?: number | null };

export interface StatSpecs {
  period: StatPeriod;
  level: string;
  rows: StatSpecRow[];
  rest: (StatTotals & { rows: number }) | null;
  totals: StatTotals;
  total_rows: number;
  completeness: { new_total: number; classified: number; classified_pct: number | null };
  notes: string[];
}

export interface StatCityRow {
  key: string;
  kind: string;
  country_code: string | null;
  country: string | null;
  region_code: string | null;
  region: string | null;
  city: string | null;
  label: string;
  clickable: boolean;
  sel: number;
  prev: number;
  change_pct: number | null;
  today: number;
  "7d": number;
  "30d": number;
  active: number;
  companies: number;
}

export interface StatCities {
  period: StatPeriod;
  mode: string;
  level: string;
  additive: boolean;
  rows: StatCityRow[];
  rest: (StatTotals & { rows: number }) | null;
  totals: StatTotals | null;
  total_rows: number;
  notes: string[];
}

export interface StatCountries {
  period: StatPeriod;
  rows: { code: string; name: string; sel: number; prev: number; change_pct: number | null; today: number; "7d": number; "30d": number; active: number }[];
  totals: StatTotals;
  empty: boolean;
}

export interface StatMatrix {
  period: StatPeriod;
  level: string;
  total: number;
  rows: { id: number | null; slug: string; name: string; total: number }[];
  columns: StatCityRow[];
  cells: number[][];
  other: { outside_shown: number; in_buckets: number };
  top_combinations: { specialization: { id: number | null; slug: string; name: string }; place: StatCityRow; count: number }[];
  empty: boolean;
  notes: string[];
}

export interface StatConfig {
  specializations: { id: number; slug: string; name_en: string; name_de: string; parent_id: number | null; is_it: boolean }[];
  countries: { code: string; name: string; in_dach: boolean }[];
  regions: { code: string; country_code: string; name: string }[];
  periods: string[];
  timezone: string;
  telegram: { enabled: boolean; configured: boolean; post_empty: boolean; bot_link: string | null };
  languages: string[];
  formats: string[];
}

export interface TelegramPreview {
  text: string;
  parse_mode: string | null;
  sha256: string;
  length: number;
  empty: boolean;
  period: StatPeriod;
  snapshot_at: string;
  warnings: string[];
  data: { new_jobs_total: number; classified_pct: number | null };
}

export interface StatPublication {
  id: number;
  status: string;
  channel: string;
  period_start: string;
  period_end: string;
  attempts: number;
  message_id: number | null;
  error: string | null;
  text: string;
  created_by: string;
  created_at: string | null;
  sent_at: string | null;
}
