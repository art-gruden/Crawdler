import { useEffect, useMemo, useState } from "react";
import { api, post, qs } from "../api";
import { navigate } from "../App";
import { Badge, Card, ErrorNote, SafeLink, VERIFICATION_STATUSES, fmtDate, verificationLabel } from "../components/common";
import type { Company, CompanyDetail, Industry, Page, Region } from "../types";
import { Pager } from "./Discovery";

const SCAN_STATUSES = ["not_queued", "queued", "scanning", "done", "failed"];
// Never crawled: robots.txt forbids it, access is restricted, or the domain is not the company's.
const EXCLUDED = ["robots_disallowed", "access_restricted", "parked", "redirected", "blocked_address", "no_website"];

export default function Companies({ params }: { params: URLSearchParams }) {
  const [regions, setRegions] = useState<Region[]>([]);
  const [industries, setIndustries] = useState<Industry[]>([]);
  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [region, setRegion] = useState("");
  const [industry, setIndustry] = useState("");
  const [verification, setVerification] = useState("");
  const [scanStatus, setScanStatus] = useState("");
  const [source, setSource] = useState("");
  const [sort, setSort] = useState("first_seen");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [data, setData] = useState<Page<Company> | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [detailId, setDetailId] = useState<number | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  const jobId = params.get("job");
  const newInJob = params.get("new") === "1";

  useEffect(() => {
    api<Region[]>("/api/discovery/regions").then(setRegions).catch(() => {});
    api<Industry[]>("/api/discovery/industries").then(setIndustries).catch(() => {});
  }, []);

  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedQ(q), 300);
    return () => window.clearTimeout(t);
  }, [q]);

  const filters = useMemo(
    () => ({
      q: debouncedQ,
      region,
      industry,
      verification,
      scan_status: scanStatus,
      source,
      job_id: jobId ?? undefined,
      new_in_job: newInJob,
    }),
    [debouncedQ, region, industry, verification, scanStatus, source, jobId, newInJob],
  );

  useEffect(() => setPage(1), [filters, pageSize]);

  useEffect(() => {
    api<Page<Company>>(`/api/companies${qs({ ...filters, sort, order, page, page_size: pageSize })}`)
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(String(e.message ?? e)));
  }, [filters, sort, order, page, pageSize, reload]);

  const regionName = useMemo(() => {
    const m = new Map<string, string>();
    regions.forEach((s) => s.children.forEach((c) => m.set(c.code, c.name)));
    return m;
  }, [regions]);

  function sortBy(col: string) {
    if (sort === col) setOrder(order === "asc" ? "desc" : "asc");
    else {
      setSort(col);
      setOrder(col === "name" || col === "city" || col === "domain" ? "asc" : "desc");
    }
  }

  async function queueSelected() {
    setMessage(null);
    try {
      const r = await post<Record<string, number>>("/api/companies/scan", { ids: [...selected] });
      setMessage(
        `Queued ${r.queued}, already queued ${r.already_queued}, verifying first ${r.verifying}, not scannable ${r.not_scannable}` +
          (r.not_scannable ? " (only verified websites are crawled)" : ""),
      );
      setSelected(new Set());
      setReload((x) => x + 1);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function scanOne(c: Company) {
    setMessage(null);
    try {
      const r = await post<{ status: string }>(`/api/companies/${c.id}/scan`);
      setMessage(`${c.name}: ${r.status.replace(/_/g, " ")}`);
      setReload((x) => x + 1);
    } catch (e) {
      setError(`${c.name}: ${e instanceof Error ? e.message : e}`);
    }
  }

  const items = data?.items ?? [];
  const allOnPage = items.length > 0 && items.every((c) => selected.has(c.id));
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;
  const exportHref = `/api/companies/export.csv${qs(filters)}`;
  const sortMark = (col: string) => (sort === col ? (order === "asc" ? " ▲" : " ▼") : "");

  return (
    <>
      <Card
        title="Companies"
        actions={
          <a className="button" href={exportHref}>
            Export CSV
          </a>
        }
      >
        <div className="filters">
          <input className="search" placeholder="Search name, domain, city…" value={q} onChange={(e) => setQ(e.target.value)} />
          <select value={region} onChange={(e) => setRegion(e.target.value)} aria-label="Region">
            <option value="">All regions</option>
            {regions.map((s) => (
              <optgroup key={s.code} label={s.name}>
                <option value={s.code}>All of {s.name}</option>
                {s.children.map((c) => (
                  <option key={c.code} value={c.code}>
                    {c.name}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
          <select value={industry} onChange={(e) => setIndustry(e.target.value)} aria-label="Industry">
            <option value="">All industries</option>
            {industries.map((i) => (
              <option key={i.id} value={i.id}>
                {i.label}
              </option>
            ))}
          </select>
          <select value={verification} onChange={(e) => setVerification(e.target.value)} aria-label="Website status">
            <option value="">Any website status</option>
            <option value="verified">Verified</option>
            <option value="unverified">Not checked</option>
            <option value="access_restricted,robots_disallowed,parked,redirected,blocked_address">Excluded from crawling</option>
            <option value="unreachable,dns_error,http_error,rate_limited,robots_unavailable">Errors (retried)</option>
            <option value="no_website">No website</option>
            {VERIFICATION_STATUSES.map((v) => (
              <option key={v} value={v}>
                · {verificationLabel(v)}
              </option>
            ))}
          </select>
          <select value={scanStatus} onChange={(e) => setScanStatus(e.target.value)} aria-label="Scan status">
            <option value="">Any scan status</option>
            {SCAN_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.replace("_", " ")}
              </option>
            ))}
          </select>
          <select value={source} onChange={(e) => setSource(e.target.value)} aria-label="Source">
            <option value="">Any source</option>
            <option value="osm">OpenStreetMap</option>
            <option value="csv:">Any CSV import</option>
            <option value="csv:standort_sachsen">Firmendatenbank Sachsen</option>
          </select>
          {jobId && (
            <span className="chip">
              {newInJob ? "New in" : "Touched by"} job #{jobId}
              <button className="link" onClick={() => navigate("companies")} aria-label="Clear job filter">
                ×
              </button>
            </span>
          )}
        </div>

        <div className="bulk">
          <span>{selected.size} selected</span>
          <button className="primary" disabled={selected.size === 0} onClick={queueSelected}>
            Queue selected for vacancy scanning
          </button>
          {selected.size > 0 && (
            <button className="link" onClick={() => setSelected(new Set())}>
              Clear selection
            </button>
          )}
          {message && <span className="note ok inline">{message}</span>}
        </div>
        <ErrorNote error={error} />

        <div className="table-wrap">
          <table className="table companies">
            <thead>
              <tr>
                <th>
                  <input
                    type="checkbox"
                    aria-label="Select page"
                    checked={allOnPage}
                    onChange={() => {
                      const next = new Set(selected);
                      items.forEach((c) => (allOnPage ? next.delete(c.id) : next.add(c.id)));
                      setSelected(next);
                    }}
                  />
                </th>
                <th className="sortable" onClick={() => sortBy("name")}>
                  Company{sortMark("name")}
                </th>
                <th className="sortable" onClick={() => sortBy("domain")}>
                  Website{sortMark("domain")}
                </th>
                <th className="sortable" onClick={() => sortBy("city")}>
                  Location{sortMark("city")}
                </th>
                <th>Industry</th>
                <th className="sortable" onClick={() => sortBy("verification")}>
                  Website status{sortMark("verification")}
                </th>
                <th>Sources</th>
                <th className="sortable" onClick={() => sortBy("first_seen")}>
                  First seen{sortMark("first_seen")}
                </th>
                <th>Scan</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((c) => (
                <tr key={c.id} className={selected.has(c.id) ? "selected" : ""}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Select ${c.name}`}
                      checked={selected.has(c.id)}
                      onChange={() => {
                        const next = new Set(selected);
                        if (next.has(c.id)) next.delete(c.id);
                        else next.add(c.id);
                        setSelected(next);
                      }}
                    />
                  </td>
                  <td>
                    <button className="link name" onClick={() => setDetailId(c.id)}>
                      {c.name}
                    </button>
                  </td>
                  <td className="clip">
                    {c.domain ? <SafeLink href={c.website_url}>{c.domain_display ?? c.domain}</SafeLink> : <span className="hint">{c.website_rejected_reason ?? "–"}</span>}
                  </td>
                  <td>
                    {c.city ?? "–"}
                    {c.district_key && c.district_name !== c.city && <div className="hint">{regionName.get(c.district_key) ?? c.district_name}</div>}
                  </td>
                  <td>
                    {c.industries.map((i) => (
                      <span key={i} className="tag">
                        {industries.find((x) => x.id === i)?.label ?? i}
                      </span>
                    ))}
                  </td>
                  <td>
                    <Badge value={c.verification_status} title={c.verification_confidence !== null ? `confidence ${c.verification_confidence}` : undefined} />
                  </td>
                  <td>
                    {c.sources.map((s) => (
                      <span key={s} className={`tag ${s === "osm" ? "osm" : ""}`}>
                        {s === "osm" ? "OSM" : s.replace(/^csv:/, "CSV ")}
                      </span>
                    ))}
                  </td>
                  <td>{fmtDate(c.first_seen_at)}</td>
                  <td>
                    <Badge value={c.scan_status} />
                  </td>
                  <td>
                    <button
                      className="small"
                      disabled={!c.domain || ["queued", "scanning"].includes(c.scan_status) || EXCLUDED.includes(c.verification_status)}
                      onClick={() => scanOne(c)}
                      title="Queue this company for vacancy scanning (verifies the website first if needed)"
                    >
                      Scan
                    </button>
                  </td>
                </tr>
              ))}
              {data && items.length === 0 && (
                <tr>
                  <td colSpan={10} className="empty">
                    No companies match these filters.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="row between">
          <Pager page={page} pages={pages} total={data?.total ?? 0} onPage={setPage} />
          <label className="inline">
            Per page{" "}
            <select value={pageSize} onChange={(e) => setPageSize(Number(e.target.value))}>
              {[25, 50, 100, 200].map((n) => (
                <option key={n}>{n}</option>
              ))}
            </select>
          </label>
        </div>
      </Card>
      {detailId !== null && <CompanyDrawer id={detailId} onClose={() => setDetailId(null)} onChanged={() => setReload((x) => x + 1)} />}
    </>
  );
}

function CompanyDrawer({ id, onClose, onChanged }: { id: number; onClose: () => void; onChanged: () => void }) {
  const [c, setC] = useState<CompanyDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setC(null);
    api<CompanyDetail>(`/api/companies/${id}`)
      .then(setC)
      .catch((e) => setError(String(e.message ?? e)));
  }, [id]);

  async function verify() {
    try {
      await post(`/api/companies/${id}/verify`);
      onChanged();
      setC(await api<CompanyDetail>(`/api/companies/${id}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <aside className="drawer" role="dialog" aria-label="Company details">
      <div className="drawer-head">
        <h2>{c?.name ?? "…"}</h2>
        <button className="link" onClick={onClose} aria-label="Close">
          ×
        </button>
      </div>
      <ErrorNote error={error} />
      {c && (
        <div className="stack">
          <dl className="kv">
            <dt>Website</dt>
            <dd>
              <SafeLink href={c.website_url}>{c.website_url ?? "–"}</SafeLink>
              {c.website_raw && c.website_raw !== c.website_url && <div className="hint">as given: {c.website_raw}</div>}
            </dd>
            <dt>Status</dt>
            <dd>
              <Badge value={c.verification_status} /> {c.verified_at && <span className="hint">checked {fmtDate(c.verified_at, true)}</span>}
              {typeof c.verification_detail.title === "string" && <div className="hint">“{c.verification_detail.title}”</div>}
              {typeof c.verification_detail.reason === "string" && <div className="hint">{c.verification_detail.reason}</div>}
              {typeof c.verification_detail.error === "string" && <div className="hint">{c.verification_detail.error}</div>}
            </dd>
            <dt>Address</dt>
            <dd>{[c.street, [c.postcode, c.city].filter(Boolean).join(" ")].filter(Boolean).join(", ") || "–"}</dd>
            <dt>Region</dt>
            <dd>
              {c.district_name ?? "–"} {c.state_code && <span className="hint">({c.state_code})</span>}
            </dd>
            {c.lat !== null && c.lon !== null && (
              <>
                <dt>Map</dt>
                <dd>
                  <a href={`https://www.openstreetmap.org/?mlat=${c.lat}&mlon=${c.lon}#map=17/${c.lat}/${c.lon}`} target="_blank" rel="noopener noreferrer">
                    {c.lat.toFixed(5)}, {c.lon.toFixed(5)}
                  </a>
                </dd>
              </>
            )}
            {c.alt_names.length > 0 && (
              <>
                <dt>Also listed as</dt>
                <dd>{c.alt_names.join(" · ")}</dd>
              </>
            )}
            <dt>Seen</dt>
            <dd>
              first {fmtDate(c.first_seen_at, true)} · last {fmtDate(c.last_seen_at, true)}
            </dd>
            <dt>Scan</dt>
            <dd>
              <Badge value={c.scan_status} /> {c.last_scan_at && <span className="hint">last {fmtDate(c.last_scan_at, true)}</span>}
            </dd>
          </dl>
          <div className="row">
            <button onClick={verify} disabled={!c.domain}>
              Re-check website
            </button>
          </div>
          <h3>Sources ({c.source_records.length})</h3>
          <ul className="sources">
            {c.source_records.map((s) => (
              <li key={`${s.source}-${s.source_record_id}`}>
                <div>
                  <b>{s.source}</b> · <span className="mono">{s.source_record_id}</span>
                  {s.source_url && (
                    <>
                      {" "}
                      · <SafeLink href={s.source_url}>open</SafeLink>
                    </>
                  )}
                </div>
                <div className="hint">
                  {s.name && <>“{s.name}” · </>}
                  {s.license ?? "licence unknown"} · first {fmtDate(s.first_seen_at)} · last {fmtDate(s.last_seen_at)}
                  {s.data_timestamp && <> · data as of {fmtDate(s.data_timestamp)}</>}
                </div>
                {s.attribution && <div className="hint">{s.attribution}</div>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </aside>
  );
}
