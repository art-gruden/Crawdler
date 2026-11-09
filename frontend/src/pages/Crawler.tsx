import { useEffect, useState } from "react";
import { api, qs } from "../api";
import CrawlDrawer from "../components/CrawlDrawer";
import { Badge, Card, ErrorNote, fmtDate } from "../components/common";
import type { CrawlRunRow, CrawlerStats, ProblemDomain, TimePoint } from "../types";

const METHOD_LABEL: Record<string, string> = {
  ats: "ATS feed",
  jsonld: "JSON-LD",
  html: "Static HTML",
  content: "Content extraction",
  pdf: "PDF",
  browser: "Browser",
  llm: "LLM",
};

function Tile({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <div className="tile">
      <div className="tile-value">{value}</div>
      <div className="tile-label">{label}</div>
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

/** Bars for one series; no chart library needed. */
function Bars({ points, field, label }: { points: TimePoint[]; field: keyof TimePoint; label: string }) {
  const values = points.map((p) => Number(p[field] ?? 0));
  const max = Math.max(1, ...values);
  const w = 600;
  const h = 70;
  const bw = Math.min(24, values.length ? w / values.length : w); // few buckets: thin bars, right-aligned
  const x0 = w - bw * values.length;
  return (
    <figure className="spark">
      <figcaption>
        {label} <span className="hint">max {Math.round(max)}</span>
      </figcaption>
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label={label}>
        {values.map((v, i) => (
          <rect key={i} x={x0 + i * bw + 1} width={Math.max(1, bw - 2)} y={h - (v / max) * h} height={(v / max) * h}>
            <title>
              {fmtDate(points[i].t, true)}: {Math.round(v)}
            </title>
          </rect>
        ))}
      </svg>
    </figure>
  );
}

export default function Crawler() {
  const [hours, setHours] = useState(24);
  const [stats, setStats] = useState<CrawlerStats | null>(null);
  const [series, setSeries] = useState<TimePoint[]>([]);
  const [active, setActive] = useState<CrawlRunRow[]>([]);
  const [recent, setRecent] = useState<CrawlRunRow[]>([]);
  const [problems, setProblems] = useState<ProblemDomain[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<{ id: number; name: string } | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const t = setInterval(() => setTick((x) => x + 1), 15000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    const bucket = hours <= 6 ? 5 : hours <= 48 ? 15 : 60;
    Promise.all([
      api<CrawlerStats>(`/api/crawler/stats${qs({ hours })}`),
      api<TimePoint[]>(`/api/crawler/timeseries${qs({ hours, bucket_minutes: bucket })}`),
      api<CrawlRunRow[]>(`/api/crawler/runs${qs({ status: "running", limit: 50 })}`),
      api<CrawlRunRow[]>(`/api/crawler/runs${qs({ limit: 30 })}`),
      api<ProblemDomain[]>("/api/crawler/problem-domains"),
    ])
      .then(([s, ts, a, r, p]) => {
        setStats(s);
        setSeries(ts);
        setActive(a);
        setRecent(r.filter((x) => x.status !== "running"));
        setProblems(p);
        setError(null);
      })
      .catch((e) => setError(String(e.message ?? e)));
  }, [hours, tick]);

  const methods = stats ? Object.entries(stats.extraction_by_method) : [];
  const totalJobsByMethod = stats ? Object.values(stats.open_jobs_by_method).reduce((a, b) => a + b, 0) : 0;

  return (
    <div>
      <div className="row between">
        <h1>Career crawler</h1>
        <div className="row">
          <label className="hint" htmlFor="window">
            Window
          </label>
          <select id="window" value={hours} onChange={(e) => setHours(Number(e.target.value))}>
            <option value={1}>1 h</option>
            <option value={6}>6 h</option>
            <option value={24}>24 h</option>
            <option value={168}>7 days</option>
          </select>
        </div>
      </div>
      <ErrorNote error={error} />
      {stats && (
        <>
          <div className="tiles">
            <Tile label="Active scans" value={stats.active_scans} />
            <Tile label={`Completed scans (${hours} h)`} value={stats.completed_scans} />
            <Tile label="Domains / hour" value={stats.companies_per_hour} hint="last 60 min" />
            <Tile label="Pages / second" value={stats.pages_per_second} hint="last 5 min" />
            <Tile label="Open postings" value={stats.jobs_open} hint={`${stats.jobs_total} total`} />
            <Tile label={`New postings (${hours} h)`} value={stats.jobs_new} />
            <Tile label="429 / 503" value={`${(stats.rate_429_503 * 100).toFixed(1)} %`} hint={`${stats.requests} requests`} />
            <Tile label="Avg latency" value={stats.avg_latency_ms ? `${Math.round(stats.avg_latency_ms)} ms` : "–"} hint={`${stats.mb_downloaded} MB`} />
          </div>

          <div className="grid two">
            <Card title="Extraction by method">
              <table className="table">
                <thead>
                  <tr>
                    <th>Method</th>
                    <th className="num">Success</th>
                    <th className="num">Failure</th>
                    <th className="num">Rate</th>
                    <th className="num">Postings found</th>
                    <th className="num">Open postings</th>
                  </tr>
                </thead>
                <tbody>
                  {methods.length === 0 && (
                    <tr>
                      <td colSpan={6} className="empty">
                        No extractions in this window
                      </td>
                    </tr>
                  )}
                  {methods.map(([m, c]) => (
                    <tr key={m}>
                      <td>
                        <Badge value={m} label={METHOD_LABEL[m] ?? m} />
                      </td>
                      <td className="num">{c.ok}</td>
                      <td className="num">{c.fail}</td>
                      <td className="num">{Math.round((c.ok / Math.max(1, c.ok + c.fail)) * 100)} %</td>
                      <td className="num">{c.jobs}</td>
                      <td className="num">{stats.open_jobs_by_method[m] ?? 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {totalJobsByMethod > 0 && (
                <p className="hint">
                  Open postings: ATS {Math.round(((stats.open_jobs_by_method.ats ?? 0) / totalJobsByMethod) * 100)} % · HTML{" "}
                  {Math.round((((stats.open_jobs_by_method.html ?? 0) + (stats.open_jobs_by_method.jsonld ?? 0) + (stats.open_jobs_by_method.content ?? 0)) / totalJobsByMethod) * 100)} % · browser{" "}
                  {Math.round(((stats.open_jobs_by_method.browser ?? 0) / totalJobsByMethod) * 100)} %
                </p>
              )}
            </Card>

            <Card title="Queues and resources">
              <dl className="kv">
                <dt>Scan queue</dt>
                <dd>
                  {stats.queues.scan_pending} pending · {stats.queues.scan_claimed} in progress
                </dd>
                <dt>Browser queue</dt>
                <dd>
                  {stats.queues.browser_pending} companies · {stats.browser_pages} pages rendered ({hours} h)
                </dd>
                <dt>LLM queue</dt>
                <dd>{stats.queues.llm_pending}</dd>
                <dt>Fetch errors</dt>
                <dd>{stats.fetch_errors}</dd>
              </dl>
              <table className="table">
                <thead>
                  <tr>
                    <th>Worker</th>
                    <th className="num">CPU</th>
                    <th className="num">Host RAM free</th>
                    <th className="num">Container</th>
                    <th className="num">HTTP limit</th>
                    <th>Browser</th>
                  </tr>
                </thead>
                <tbody>
                  {stats.workers.length === 0 && (
                    <tr>
                      <td colSpan={6} className="empty">
                        No worker reported in the last 10 minutes
                      </td>
                    </tr>
                  )}
                  {stats.workers.map((w) => (
                    <tr key={w.worker}>
                      <td className="mono clip" title={w.worker}>
                        {w.worker}
                      </td>
                      <td className="num">{w.cpu_pct !== null ? `${Math.round(w.cpu_pct)} %` : "–"}</td>
                      <td className="num">{w.mem_available_mb !== null ? `${w.mem_available_mb} MB` : "–"}</td>
                      <td className="num">{w.container_mem_mb !== null ? `${w.container_mem_mb} MB` : "–"}</td>
                      <td className="num">
                        {w.http_active ?? 0}/{w.http_limit ?? "–"}
                      </td>
                      <td>{w.browser_paused ? <Badge value="rate_limited" label="paused (memory)" /> : <Badge value="done" label="ok" />}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>

            <Card title="Activity">
              <Bars points={series} field="pages" label="Pages fetched" />
              <Bars points={series} field="jobs_new" label="New postings" />
              <Bars points={series} field="rate_limited" label="429 / 503 answers" />
              <Bars points={series} field="cpu_pct" label="CPU %" />
            </Card>

            <Card title={`Slow or failing domains (${problems.length})`}>
              <table className="table">
                <thead>
                  <tr>
                    <th>Company</th>
                    <th className="num">Errors in a row</th>
                    <th className="num">Latency</th>
                    <th>Last error</th>
                    <th>Next try</th>
                  </tr>
                </thead>
                <tbody>
                  {problems.length === 0 && (
                    <tr>
                      <td colSpan={5} className="empty">
                        None
                      </td>
                    </tr>
                  )}
                  {problems.map((p) => (
                    <tr key={p.company_id}>
                      <td>
                        <button className="link" onClick={() => setDrawer({ id: p.company_id, name: p.company })}>
                          {p.company}
                        </button>
                        <div className="hint">{p.domain}</div>
                      </td>
                      <td className="num">{p.error_streak}</td>
                      <td className="num">{p.avg_latency_ms ? `${Math.round(p.avg_latency_ms)} ms` : "–"}</td>
                      <td className="clip" title={p.last_error?.message ?? ""}>
                        {p.last_error ? <span className="mono">{p.last_error.kind}</span> : "–"}
                      </td>
                      <td>{fmtDate(p.rate_limited_until ?? p.next_scan_at, true)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>

            <Card title={`Scans (${active.length} active)`}>
              <RunsTable rows={[...active, ...recent].slice(0, 40)} onOpen={(r) => setDrawer({ id: r.company_id, name: r.company })} />
            </Card>

            <Card title="Outcomes">
              <ul className="sources">
                {Object.entries(stats.outcomes).map(([k, v]) => (
                  <li key={k}>
                    <Badge value={k} /> {v}
                  </li>
                ))}
                {Object.keys(stats.outcomes).length === 0 && <li className="hint">No finished scans in this window</li>}
              </ul>
            </Card>
          </div>
        </>
      )}
      {drawer && <CrawlDrawer companyId={drawer.id} title={drawer.name} onClose={() => setDrawer(null)} />}
    </div>
  );
}

function RunsTable({ rows, onOpen }: { rows: CrawlRunRow[]; onOpen: (r: CrawlRunRow) => void }) {
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Company</th>
          <th>Status</th>
          <th>Outcome</th>
          <th>Strategy</th>
          <th className="num">Pages</th>
          <th className="num">Postings</th>
          <th>Started</th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 && (
          <tr>
            <td colSpan={7} className="empty">
              No scans yet
            </td>
          </tr>
        )}
        {rows.map((r) => (
          <tr key={r.id}>
            <td>
              <button className="link" onClick={() => onOpen(r)}>
                {r.company}
              </button>
            </td>
            <td>
              <Badge value={r.status} />
            </td>
            <td>{r.outcome ? <Badge value={r.outcome} /> : "–"}</td>
            <td>{r.strategy ? <Badge value={r.strategy} label={METHOD_LABEL[r.strategy] ?? r.strategy} /> : "–"}</td>
            <td className="num">{r.pages_fetched}</td>
            <td className="num">
              {r.jobs_found}
              {r.jobs_new > 0 && <span className="hint"> (+{r.jobs_new})</span>}
            </td>
            <td>{fmtDate(r.started_at, true)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
