import { useEffect, useState } from "react";
import { api, post } from "../api";
import type { CompanyCrawl } from "../types";
import { Badge, ErrorNote, SafeLink, fmtDate } from "./common";

/** Why does a company yield (no) postings? Diagnosis, strategy cache, sources, errors, rescan. */
export function CrawlDetails({ companyId, onChanged }: { companyId: number; onChanged?: () => void }) {
  const [d, setD] = useState<CompanyCrawl | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = () =>
    api<CompanyCrawl>(`/api/crawler/companies/${companyId}`)
      .then(setD)
      .catch((e) => setError(String(e.message ?? e)));

  useEffect(() => {
    setD(null);
    setError(null);
    setNote(null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [companyId]);

  async function rescan(full: boolean) {
    setError(null);
    try {
      const r = await post<{ status: string }>(`/api/crawler/companies/${companyId}/rescan`, { full });
      setNote(r.status === "queued" ? (full ? "Queued for a full rediscovery." : "Queued for rescan.") : "Already queued.");
      onChanged?.();
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (!d) return <ErrorNote error={error} />;
  const fp = d.fingerprint;
  return (
    <div className="stack">
      <div className={`note ${d.diagnosis.code === "jobs_found" ? "ok" : "warn"}`}>{d.diagnosis.text}</div>
      <ErrorNote error={error} />
      {note && <div className="note ok inline">{note}</div>}
      <div className="row wrap">
        <button className="primary" onClick={() => rescan(false)} disabled={d.company.verification_status !== "verified"}>
          Rescan
        </button>
        <button onClick={() => rescan(true)} disabled={d.company.verification_status !== "verified"} title="Forget the cached strategy and ATS, crawl the whole site again">
          Full rediscovery
        </button>
        <span className="hint">
          Open postings: {d.jobs.open ?? 0} · closed: {d.jobs.closed ?? 0}
        </span>
      </div>
      {fp && (
        <dl className="kv">
          <dt>Best strategy</dt>
          <dd>
            {fp.best_strategy ? <Badge value={fp.best_strategy} /> : "–"}
            {fp.ats_type && (
              <span className="hint">
                {" "}
                {fp.ats_type}: <span className="mono">{fp.ats_account}</span>
              </span>
            )}
            {fp.needs_browser && <Badge value="browser" label="needs browser" />}
          </dd>
          <dt>Success rates</dt>
          <dd>
            {Object.entries(fp.strategy_stats).map(([k, v]) => (
              <span key={k} className="tag" title={`${v.ok}/${v.attempts} successful, ${v.jobs} postings`}>
                {k} {Math.round((v.ok / Math.max(1, v.attempts)) * 100)}%
              </span>
            ))}
            {Object.keys(fp.strategy_stats).length === 0 && "–"}
          </dd>
          <dt>Career URLs</dt>
          <dd>
            {fp.career_urls.length === 0 && "–"}
            {fp.career_urls.map((u) => (
              <div key={u} className="clip">
                <SafeLink href={u}>{u}</SafeLink>
              </div>
            ))}
          </dd>
          <dt>Schedule</dt>
          <dd>
            every ~{fp.interval_h ? Math.round(fp.interval_h) : "?"} h · next {fmtDate(fp.next_scan_at, true)}
            {fp.avg_latency_ms && <span className="hint"> · avg {Math.round(fp.avg_latency_ms)} ms</span>}
          </dd>
          {fp.technologies.length > 0 && (
            <>
              <dt>Site</dt>
              <dd>{fp.technologies.join(", ")}</dd>
            </>
          )}
        </dl>
      )}
      {d.sources.length > 0 && (
        <>
          <h3>Career sources</h3>
          <ul className="sources">
            {d.sources.map((s) => (
              <li key={s.id}>
                <Badge value={s.kind === "ats_feed" ? "ats" : "html"} label={s.ats_type ?? s.kind.replace("_", " ")} />{" "}
                <SafeLink href={s.url}>{s.url}</SafeLink>
                <div className="hint">
                  {s.last_job_count ?? 0} postings · last ok {fmtDate(s.last_success_at, true)}
                  {s.last_error && <> · error: {s.last_error}</>}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
      <h3>Recent scans</h3>
      <table className="table">
        <thead>
          <tr>
            <th>Started</th>
            <th>Status</th>
            <th>Outcome</th>
            <th className="num">Pages</th>
            <th className="num">Postings</th>
            <th className="num">New</th>
          </tr>
        </thead>
        <tbody>
          {d.runs.length === 0 && (
            <tr>
              <td colSpan={6} className="empty">
                No scans yet
              </td>
            </tr>
          )}
          {d.runs.map((r) => (
            <tr key={r.id}>
              <td>{fmtDate(r.started_at, true)}</td>
              <td>
                <Badge value={r.status} />
                {r.browser_pending && <Badge value="browser" label="browser pending" />}
              </td>
              <td>{r.outcome ? <Badge value={r.outcome} /> : "–"}</td>
              <td className="num">{r.pages_fetched}</td>
              <td className="num">{r.jobs_found}</td>
              <td className="num">{r.jobs_new}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {d.errors.length > 0 && (
        <>
          <h3>Errors</h3>
          <ul className="errors">
            {d.errors.slice(0, 20).map((e, i) => (
              <li key={i}>
                <span className="mono">
                  {e.stage}/{e.kind}
                </span>{" "}
                {e.message && <span className="hint">{e.message}</span>}
                {e.url && (
                  <div className="hint clip">
                    {fmtDate(e.at, true)} · {e.url}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

export default function CrawlDrawer({ companyId, title, onClose }: { companyId: number; title?: string; onClose: () => void }) {
  return (
    <aside className="drawer" role="dialog" aria-label="Career crawl details">
      <div className="drawer-head">
        <h2>{title ?? "Career crawl"}</h2>
        <button className="link" onClick={onClose} aria-label="Close">
          ×
        </button>
      </div>
      <CrawlDetails companyId={companyId} />
    </aside>
  );
}
