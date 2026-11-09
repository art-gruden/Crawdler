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
