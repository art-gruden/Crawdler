import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { api, post, qs } from "../api";
import { navigate } from "../App";
import { Badge, Card, ErrorNote, fmtDate } from "../components/common";
import type { ImportProfile, Industry, Job, JobItem, Page, Region, Usage } from "../types";

export default function Discovery() {
  const [regions, setRegions] = useState<Region[]>([]);
  const [industries, setIndustries] = useState<Industry[]>([]);
  const [profiles, setProfiles] = useState<ImportProfile[]>([]);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selectedJob, setSelectedJob] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refreshJobs = useCallback(async () => {
    try {
      const [j, u] = await Promise.all([api<Job[]>("/api/discovery/jobs?limit=30"), api<Usage>("/api/discovery/overpass/usage")]);
      setJobs(j);
      setUsage(u);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  }, []);

  useEffect(() => {
    Promise.all([
      api<Region[]>("/api/discovery/regions"),
      api<Industry[]>("/api/discovery/industries"),
      api<ImportProfile[]>("/api/discovery/import-profiles"),
    ])
      .then(([r, i, p]) => {
        setRegions(r);
        setIndustries(i);
        setProfiles(p);
      })
      .catch((e) => setError(String(e.message ?? e)));
    refreshJobs();
  }, [refreshJobs]);

  const active = jobs.some((j) => j.status === "pending" || j.status === "running");
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(refreshJobs, 3000);
    return () => window.clearInterval(t);
  }, [active, refreshJobs]);

  const regionName = useMemo(() => {
    const m = new Map<string, string>();
    regions.forEach((s) => {
      m.set(s.code, s.name);
      s.children.forEach((c) => m.set(c.code, c.name));
    });
    return m;
  }, [regions]);

  return (
    <div className="grid two">
      <OsmForm regions={regions} industries={industries} usage={usage} busy={jobs.some((j) => j.kind === "osm" && (j.status === "pending" || j.status === "running"))} onStarted={(id) => { setSelectedJob(id); refreshJobs(); }} />
      <CsvForm profiles={profiles} onStarted={(id) => { setSelectedJob(id); refreshJobs(); }} />
      <div className="span2">
        <ErrorNote error={error} />
        <JobsTable jobs={jobs} selected={selectedJob} onSelect={setSelectedJob} onChanged={refreshJobs} />
      </div>
      {selectedJob !== null && (
        <div className="span2">
          <JobResults job={jobs.find((j) => j.id === selectedJob) ?? null} regionName={regionName} />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- OSM form

function OsmForm(props: { regions: Region[]; industries: Industry[]; usage: Usage | null; busy: boolean; onStarted: (id: number) => void }) {
  const { regions, industries, usage, busy, onStarted } = props;
  const [selected, setSelected] = useState<Set<string>>(new Set(["14612"]));
  const [inds, setInds] = useState<Set<string> | null>(null);
  const [verify, setVerify] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  const activeInds = inds ?? new Set(industries.filter((i) => i.default).map((i) => i.id));
  const state = regions[0];
  const wholeState = state ? selected.has(state.code) : false;
  const requests = state ? (wholeState ? state.children.length : selected.size) : 0;
  const left = usage ? usage.max_requests - usage.requests : null;

  function toggle(set: Set<string>, value: string) {
    const next = new Set(set);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    return next;
  }

  async function start(e: FormEvent) {
    e.preventDefault();
    setSending(true);
    setError(null);
    try {
      const codes = wholeState && state ? [state.code] : [...selected];
      const job = await post<Job>("/api/discovery/osm", { regions: codes, industries: [...activeInds], verify });
      onStarted(job.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSending(false);
    }
  }

  if (!state) return <Card title="OpenStreetMap discovery">Loading…</Card>;
  const cities = state.children.filter((c) => c.kind === "kreisfreie_stadt");
  const kreise = state.children.filter((c) => c.kind === "landkreis");

  return (
    <Card title="OpenStreetMap discovery">
      <form onSubmit={start} className="stack">
        <fieldset>
          <legend>Region</legend>
          <label className="check strong">
            <input type="checkbox" checked={wholeState} onChange={() => setSelected(toggle(selected, state.code))} />
            All of {state.name} ({state.code}, {state.children.length} Kreise)
          </label>
          <div className={`columns ${wholeState ? "disabled" : ""}`}>
            <div>
              <div className="sub">Kreisfreie Städte</div>
              {cities.map((r) => (
                <label key={r.code} className="check">
                  <input type="checkbox" disabled={wholeState} checked={wholeState || selected.has(r.code)} onChange={() => setSelected(toggle(selected, r.code))} />
                  {r.name}
                </label>
              ))}
            </div>
            <div>
              <div className="sub">Landkreise</div>
              {kreise.map((r) => (
                <label key={r.code} className="check">
                  <input type="checkbox" disabled={wholeState} checked={wholeState || selected.has(r.code)} onChange={() => setSelected(toggle(selected, r.code))} />
                  {r.name.replace(/^Landkreis /, "")}
                </label>
              ))}
            </div>
          </div>
        </fieldset>
        <fieldset>
          <legend>Industry</legend>
          {industries.map((i) => (
            <label key={i.id} className="check" title={i.description}>
              <input type="checkbox" checked={activeInds.has(i.id)} onChange={() => setInds(toggle(activeInds, i.id))} />
              <span>
                {i.label}
                <span className="hint block">{i.description}</span>
              </span>
            </label>
          ))}
        </fieldset>
        <label className="check">
          <input type="checkbox" checked={verify} onChange={(e) => setVerify(e.target.checked)} />
          Verify websites (robots.txt + homepage) and queue verified ones for vacancy scanning
        </label>
        <div className="row">
          <button className="primary" disabled={sending || busy || requests === 0 || activeInds.size === 0}>
            {busy ? "A discovery job is running" : "Start discovery"}
          </button>
          <span className="hint">
            {requests} Overpass request{requests === 1 ? "" : "s"}
            {usage && ` · used today ${usage.requests}/${usage.max_requests}, ${Math.round(usage.bytes / 1048576)} MB`}
            {usage && ` · ≥${usage.min_interval_s}s apart`}
          </span>
        </div>
        {left !== null && requests > left && <div className="note warn">Today's Overpass budget is not enough for all regions; cached regions do not count.</div>}
        <ErrorNote error={error} />
      </form>
    </Card>
  );
}

// ---------------------------------------------------------------- CSV form

function CsvForm({ profiles, onStarted }: { profiles: ImportProfile[]; onStarted: (id: number) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [profile, setProfile] = useState("standort_sachsen");
  const [sourceName, setSourceName] = useState("");
  const [license, setLicense] = useState("");
  const [attribution, setAttribution] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [verify, setVerify] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const prof = profiles.find((p) => p.id === profile);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!file) return;
    const fd = new FormData();
    fd.set("file", file);
    fd.set("profile", profile);
    if (sourceName) fd.set("source_name", sourceName);
    if (license) fd.set("license", license);
    if (attribution) fd.set("attribution", attribution);
    if (sourceUrl) fd.set("source_url", sourceUrl);
    fd.set("verify", String(verify));
    setSending(true);
    setError(null);
    try {
      const job = await post<Job>("/api/companies/import", fd);
      onStarted(job.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSending(false);
    }
  }

  return (
    <Card title="Import CSV from a company directory">
      <form onSubmit={submit} className="stack">
        <label>
          File
          <input type="file" accept=".csv,text/csv,text/plain" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
        <label>
          Format
          <select value={profile} onChange={(e) => setProfile(e.target.value)}>
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </label>
        {profile === "standort_sachsen" && (
          <p className="hint">
            Maps the technology fields <b>MICRO!</b> (IT, electronics, software), <b>MASCHINE!</b> (mechanical engineering,
            sensors) and <b>LIFE SCIENCES!</b> plus WZ/NACE codes (62/63 → IT). The directory offers no public export or
            API; use an export WFS gave you, not scraped data.
          </p>
        )}
        <label>
          Source name <span className="hint">(e.g. “IHK Dresden list”; keeps IDs of different files apart)</span>
          <input value={sourceName} onChange={(e) => setSourceName(e.target.value)} placeholder={prof?.label} />
        </label>
        <div className="row wrap">
          <label className="grow">
            Licence
            <input value={license} onChange={(e) => setLicense(e.target.value)} placeholder={prof?.license ?? "e.g. CC-BY-4.0"} />
          </label>
          <label className="grow">
            Source URL
            <input value={sourceUrl} onChange={(e) => setSourceUrl(e.target.value)} placeholder={prof?.source_url ?? "https://…"} />
          </label>
        </div>
        <label>
          Attribution
          <input value={attribution} onChange={(e) => setAttribution(e.target.value)} placeholder={prof?.attribution ?? "Quelle: …"} />
        </label>
        <label className="check">
          <input type="checkbox" checked={verify} onChange={(e) => setVerify(e.target.checked)} />
          Verify websites and queue verified ones for vacancy scanning
        </label>
        <div className="row">
          <button className="primary" disabled={!file || sending}>
            {sending ? "Uploading…" : "Import"}
          </button>
          <span className="hint">Columns are detected by name: Firma/Name, Webseite/URL, PLZ, Ort, Landkreis, Branche, WZ…</span>
        </div>
        <ErrorNote error={error} />
      </form>
    </Card>
  );
}

// ---------------------------------------------------------------- jobs

function jobScope(j: Job) {
  if (j.kind === "osm") return (j.params.region_names ?? []).join(", ") + ` · ${(j.params.industries ?? []).join(", ")}`;
  return `${j.file_name ?? "CSV"} · ${j.params.source_name ?? j.source}`;
}

function JobsTable({ jobs, selected, onSelect, onChanged }: { jobs: Job[]; selected: number | null; onSelect: (id: number) => void; onChanged: () => void }) {
  async function cancel(id: number) {
    await post(`/api/discovery/jobs/${id}/cancel`);
    onChanged();
  }
  return (
    <Card title="Discovery jobs" actions={<button onClick={onChanged}>Refresh</button>}>
      <table className="table">
        <thead>
          <tr>
            <th>#</th>
            <th>Source</th>
            <th>Scope</th>
            <th>Status</th>
            <th>Progress</th>
            <th className="num">New</th>
            <th className="num">Duplicates</th>
            <th className="num">Invalid / no site</th>
            <th>Started</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {jobs.length === 0 && (
            <tr>
              <td colSpan={10} className="empty">
                No jobs yet. Start an OpenStreetMap discovery or import a CSV file.
              </td>
            </tr>
          )}
          {jobs.map((j) => {
            const s = j.stats;
            const progress =
              j.kind === "osm"
                ? `${s.steps_done ?? 0}/${s.steps_total ?? j.params.leaf_count ?? "?"} Kreise`
                : `${s.records ?? 0} rows${s.row_errors ? `, ${s.row_errors} errors` : ""}`;
            return (
              <tr key={j.id} className={selected === j.id ? "selected" : ""} onClick={() => onSelect(j.id)}>
                <td>{j.id}</td>
                <td>{j.kind === "osm" ? "OpenStreetMap" : "CSV"}</td>
                <td className="clip" title={jobScope(j)}>
                  {jobScope(j)}
                </td>
                <td>
                  <Badge value={j.status} title={j.error ?? undefined} />
                  {j.status === "running" && j.params.current_region && <span className="hint"> {String(j.params.current_region)}</span>}
                </td>
                <td>{progress}</td>
                <td className="num">{s.new ?? 0}</td>
                <td className="num">{(s.duplicate_existing ?? 0) + (s.duplicate_in_job ?? 0)}</td>
                <td className="num">{(s.invalid_url ?? 0) + (s.no_website ?? 0)}</td>
                <td>{fmtDate(j.started_at ?? j.created_at, true)}</td>
                <td>
                  {(j.status === "pending" || j.status === "running") && !j.cancel_requested && (
                    <button
                      className="small"
                      onClick={(e) => {
                        e.stopPropagation();
                        cancel(j.id);
                      }}
                    >
                      Cancel
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Card>
  );
}

const TABS: { id: string; label: string; keys: string[] }[] = [
  { id: "new", label: "New domains", keys: ["new"] },
  { id: "duplicate", label: "Duplicate domains", keys: ["duplicate_existing", "duplicate_in_job"] },
  { id: "invalid", label: "Invalid / no website", keys: ["invalid_url", "no_website"] },
  { id: "updated", label: "Seen before", keys: ["updated"] },
  { id: "", label: "All", keys: [] },
];

function JobResults({ job, regionName }: { job: Job | null; regionName: Map<string, string> }) {
  const [tab, setTab] = useState("new");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<(Page<JobItem> & { counts: Record<string, number> }) | null>(null);
  const [error, setError] = useState<string | null>(null);
  const status = job?.status;
  const records = job?.stats.records;

  useEffect(() => setPage(1), [tab, job?.id]);
  useEffect(() => {
    if (!job) return;
    api<Page<JobItem> & { counts: Record<string, number> }>(`/api/discovery/jobs/${job.id}/items${qs({ outcome: tab, page, page_size: 50 })}`)
      .then(setData)
      .catch((e) => setError(String(e.message ?? e)));
  }, [job?.id, tab, page, status, records]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!job) return null;
  const counts = data?.counts ?? {};
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  return (
    <Card
      title={`Job #${job.id} results`}
      actions={
        <div className="row">
          <button onClick={() => navigate("companies", { job: String(job.id), new: "1" })}>New companies in list →</button>
          <button onClick={() => navigate("companies", { job: String(job.id) })}>All touched companies →</button>
        </div>
      }
    >
      <div className="meta">
        <span>
          Provenance: <b>{job.source}</b>
          {job.license && <> · licence {job.license}</>}
          {job.data_timestamp && <> · data as of {fmtDate(job.data_timestamp, true)}</>}
        </span>
        {job.attribution && <span className="hint">{job.attribution}</span>}
        {job.error && <div className="note bad">{job.error}</div>}
        {job.errors.length > 0 && (
          <details>
            <summary>{job.errors.length} problem(s)</summary>
            <ul className="errors">
              {job.errors.map((e, i) => (
                <li key={i}>
                  {e.region && <b>{regionName.get(e.region) ?? e.region}: </b>}
                  {e.line && <b>line {e.line}: </b>}
                  {e.name && <b>{e.name}: </b>}
                  {e.error}
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
      <div className="tabs">
        {TABS.map((t) => (
          <button key={t.id} className={tab === t.id ? "active" : ""} onClick={() => setTab(t.id)}>
            {t.label} <span className="count">{t.keys.length ? t.keys.reduce((a, k) => a + (counts[k] ?? 0), 0) : Object.values(counts).reduce((a, b) => a + b, 0)}</span>
          </button>
        ))}
      </div>
      <ErrorNote error={error} />
      <table className="table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Domain</th>
            <th>Website as given</th>
            <th>Outcome</th>
            <th>Region</th>
            <th>Record</th>
          </tr>
        </thead>
        <tbody>
          {data?.items.map((it) => (
            <tr key={it.id}>
              <td>{it.name}</td>
              <td>{it.domain_display ?? it.domain ?? "–"}</td>
              <td className="clip" title={it.website_raw ?? ""}>
                {it.website_raw ?? "–"}
              </td>
              <td>
                <Badge value={it.outcome} title={it.detail ?? undefined} />
                {it.detail && <span className="hint"> {it.detail}</span>}
              </td>
              <td>{it.region_code ? regionName.get(it.region_code) ?? it.region_code : "–"}</td>
              <td className="mono">
                {it.source_record_id?.match(/^(node|way|relation)\/\d+$/) ? (
                  <a href={`https://www.openstreetmap.org/${it.source_record_id}`} target="_blank" rel="noopener noreferrer">
                    {it.source_record_id}
                  </a>
                ) : (
                  it.source_record_id?.slice(0, 20)
                )}
              </td>
            </tr>
          ))}
          {data && data.items.length === 0 && (
            <tr>
              <td colSpan={6} className="empty">
                Nothing here.
              </td>
            </tr>
          )}
        </tbody>
      </table>
      <Pager page={page} pages={pages} total={data?.total ?? 0} onPage={setPage} />
    </Card>
  );
}

export function Pager({ page, pages, total, onPage }: { page: number; pages: number; total: number; onPage: (p: number) => void }) {
  return (
    <div className="pager">
      <button disabled={page <= 1} onClick={() => onPage(page - 1)}>
        ‹ Prev
      </button>
      <span>
        Page {page} / {pages} · {total} total
      </span>
      <button disabled={page >= pages} onClick={() => onPage(page + 1)}>
        Next ›
      </button>
    </div>
  );
}
