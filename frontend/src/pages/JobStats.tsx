import { useEffect, useMemo, useRef, useState } from "react";
import { api, post, qsMulti } from "../api";
import { Card, ErrorNote, fmtDate } from "../components/common";
import { ColumnChart, Delta, HBars, NoData } from "../components/StatCharts";
import type {
  StatCities,
  StatCityRow,
  StatConfig,
  StatCountries,
  StatMatrix,
  StatPublication,
  StatSpecRow,
  StatSpecs,
  StatSummary,
  StatTrends,
  TelegramPreview,
} from "../types";

const BASE = "/api/crawler/stats/jobs";
const PERIODS: [string, string][] = [
  ["today", "Today"],
  ["yesterday", "Yesterday"],
  ["last_24h", "Last 24 hours"],
  ["7d", "Last 7 days"],
  ["30d", "Last 30 days"],
  ["custom", "Custom range"],
];
const nf = (n: number) => n.toLocaleString("de-DE");

interface Flt {
  period: string;
  from: string;
  to: string;
  tz: string;
  countries: string[];
  regions: string[];
  city: string;
  specs: string[];
  excludeInitial: boolean;
}

interface View {
  specLevel: "specialization" | "group";
  specTop: number;
  specSort: string;
  specOrder: "asc" | "desc";
  cityMode: "unique" | "coverage";
  cityLevel: "city" | "region" | "country";
  citySort: string;
  cityOrder: "asc" | "desc";
  cityTop: number;
  matrixLevel: "specialization" | "group";
}

const INITIAL_VIEW: View = {
  specLevel: "specialization",
  specTop: 10,
  specSort: "sel",
  specOrder: "desc",
  cityMode: "unique",
  cityLevel: "city",
  citySort: "sel",
  cityOrder: "desc",
  cityTop: 20,
  matrixLevel: "group",
};

function baseParams(f: Flt) {
  return {
    period: f.period,
    from: f.period === "custom" ? f.from : "",
    to: f.period === "custom" ? f.to : "",
    timezone: f.tz,
    country: f.countries,
    region: f.regions,
    city: f.city.trim() ? [f.city.trim()] : [],
    specialization: f.specs,
    exclude_initial_load: f.excludeInitial,
  };
}

/** Link into the Vacancies tab with exactly the filters of the statistic (same period, scope and filters). */
function vacanciesLink(s: StatSummary | null, f: Flt, over: { country?: string; region?: string; city?: string; specialization?: string }) {
  const p = new URLSearchParams({ status: "all", dach_scope: "true" });
  if (s) {
    p.set("discovered_from", s.period.start);
    p.set("discovered_to", s.period.counted_until);
  }
  const countries = over.country ? [over.country] : f.countries;
  const regions = over.region ? [over.region] : f.regions;
  const specs = over.specialization ? [over.specialization] : f.specs;
  const city = over.city ?? (f.city.trim() || "");
  if (countries.length) p.set("country_code", countries.join(","));
  if (regions.length) p.set("region", regions.join(","));
  if (specs.length) p.set("specialization", specs.join(","));
  if (city) p.set("place_city", city);
  return `#/jobs?${p.toString()}`;
}

function SortTh({ label, k, sort, order, onSort, className }: { label: string; k: string; sort: string; order: string; onSort: (k: string) => void; className?: string }) {
  const active = sort === k;
  return (
    <th className={`sortable ${className ?? ""}`} onClick={() => onSort(k)} aria-sort={active ? (order === "asc" ? "ascending" : "descending") : "none"}>
      {label} {active ? (order === "asc" ? "▲" : "▼") : ""}
    </th>
  );
}

export default function JobStats() {
  const [cfg, setCfg] = useState<StatConfig | null>(null);
  const [flt, setFlt] = useState<Flt>({ period: "7d", from: "", to: "", tz: "", countries: [], regions: [], city: "", specs: [], excludeInitial: false });
  const [view, setView] = useState<View>(INITIAL_VIEW);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<StatSummary | null>(null);
  const [trends, setTrends] = useState<StatTrends | null>(null);
  const [specs, setSpecs] = useState<StatSpecs | null>(null);
  const [cities, setCities] = useState<StatCities | null>(null);
  const [countries, setCountries] = useState<StatCountries | null>(null);
  const [matrix, setMatrix] = useState<StatMatrix | null>(null);
  const snap = useRef<string | null>(null);
  const seq = useRef(0);
  const viewRef = useRef(view);
  viewRef.current = view;

  useEffect(() => {
    api<StatConfig>(`${BASE}/config`)
      .then(setCfg)
      .catch((e) => setError(String(e.message ?? e)));
  }, []);

  const customIncomplete = flt.period === "custom" && (!flt.from || !flt.to);

  const tableQueries = (f: Flt, v: View, asOf: string) => {
    const b = { ...baseParams(f), as_of: asOf };
    return {
      specs: api<StatSpecs>(`${BASE}/specializations${qsMulti({ ...b, level: v.specLevel, sort: v.specSort, order: v.specOrder, limit: v.specTop })}`),
      cities: api<StatCities>(`${BASE}/cities${qsMulti({ ...b, mode: v.cityMode, level: v.cityLevel, sort: v.citySort, order: v.cityOrder, limit: v.cityTop })}`),
      matrix: api<StatMatrix>(`${BASE}/specialization-city${qsMulti({ ...b, level: v.matrixLevel, rows: 8, cols: 8 })}`),
    };
  };

  // Filters / refresh: one snapshot (taken by the summary request) is pinned for every other request, so the
  // cards, charts and tables describe exactly the same moment.
  useEffect(() => {
    if (customIncomplete) return;
    const my = ++seq.current;
    setLoading(true);
    const t = setTimeout(async () => {
      try {
        const s = await api<StatSummary>(`${BASE}/summary${qsMulti(baseParams(flt))}`);
        if (my !== seq.current) return;
        snap.current = s.snapshot_at;
        const b = { ...baseParams(flt), as_of: s.snapshot_at };
        const q = tableQueries(flt, viewRef.current, s.snapshot_at);
        const [tr, co, sp, ci, mx] = await Promise.all([
          api<StatTrends>(`${BASE}/trends${qsMulti(b)}`),
          api<StatCountries>(`${BASE}/countries${qsMulti(b)}`),
          q.specs,
          q.cities,
          q.matrix,
        ]);
        if (my !== seq.current) return;
        setSummary(s);
        setTrends(tr);
        setCountries(co);
        setSpecs(sp);
        setCities(ci);
        setMatrix(mx);
        setError(null);
      } catch (e) {
        if (my === seq.current) setError(String((e as Error).message ?? e));
      } finally {
        if (my === seq.current) setLoading(false);
      }
    }, 250);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flt, refresh]);

  // Table options only refetch the tables, on the same snapshot.
  const firstView = useRef(true);
  useEffect(() => {
    if (firstView.current) {
      firstView.current = false;
      return;
    }
    if (!snap.current || customIncomplete) return;
    const my = ++seq.current;
    const q = tableQueries(flt, view, snap.current);
    Promise.all([q.specs, q.cities, q.matrix])
      .then(([sp, ci, mx]) => {
        if (my !== seq.current) return;
        setSpecs(sp);
        setCities(ci);
        setMatrix(mx);
      })
      .catch((e) => my === seq.current && setError(String(e.message ?? e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view]);

  const sortSpec = (k: string) =>
    setView((v) => ({ ...v, specSort: k, specOrder: v.specSort === k && v.specOrder === "desc" ? "asc" : "desc" }));
  const sortCity = (k: string) =>
    setView((v) => ({ ...v, citySort: k, cityOrder: v.citySort === k && v.cityOrder === "desc" ? "asc" : "desc" }));

  const regionsForCountries = useMemo(
    () => (cfg?.regions ?? []).filter((r) => !flt.countries.length || flt.countries.includes(r.country_code)),
    [cfg, flt.countries],
  );
  const toggle = (key: "countries" | "regions" | "specs", v: string) =>
    setFlt((f) => ({ ...f, [key]: f[key].includes(v) ? f[key].filter((x) => x !== v) : [...f[key], v] }));

  const p = summary?.period;
  const specBars = (specs?.rows ?? []).filter((r) => r.id !== null).map((r) => ({ label: r.name, value: r.sel, href: vacanciesLink(summary, flt, { specialization: r.slug }) }));
  const cityBars = (cities?.rows ?? [])
    .filter((r) => r.kind === "city")
    .map((r) => ({ label: `${r.label} (${r.country_code ?? "?"})`, value: r.sel, href: vacanciesLink(summary, flt, { city: r.city ?? "", country: r.country_code ?? undefined, region: r.region_code ?? undefined }) }));
  const countryBars = (countries?.rows ?? []).map((r) => ({ label: r.name, value: r.sel }));
  const kpi = (k: string) => summary?.kpis.find((x) => x.key === k);

  return (
    <div>
      <h1>📊 Job Statistics</h1>
      <p className="hint block">
        <b>Newly discovered</b> = vacancies our crawler saw for the first time in the period, each counted once (rescans, updates and reopened vacancies
        are not new). It is not the date the employer published the posting.
      </p>

      <Filters cfg={cfg} flt={flt} setFlt={setFlt} toggle={toggle} regions={regionsForCountries} onRefresh={() => setRefresh((n) => n + 1)} loading={loading} />
      <ErrorNote error={error} />
      {customIncomplete && <div className="note warn">Pick both dates for the custom range.</div>}

      {p && summary && (
        <div className="interval">
          <b>Interval:</b> {p.label} · counted until {fmtDate(p.counted_until, true)} {p.in_progress && <span className="badge info">in progress</span>} · snapshot{" "}
          {fmtDate(summary.snapshot_at, true)}
          {p.calendar_days ? "" : " · rolling 24 h"}
        </div>
      )}

      {summary && (
        <>
          <div className="tiles kpis">
            {(["today", "last_24h", "7d", "30d"] as const).map((k) => {
              const x = kpi(k);
              const label = { today: "New today", last_24h: "New, last 24 h", "7d": "New, 7 days", "30d": "New, 30 days" }[k];
              const hint = { today: "local calendar day", last_24h: "rolling", "7d": "7 calendar days incl. today", "30d": "30 calendar days incl. today" }[k];
              return (
                <div className="tile" key={k}>
                  <div className="tile-value">{x ? nf(x.new) : "–"}</div>
                  <div className="tile-label">{label}</div>
                  <div className="hint">{hint}</div>
                  {x && <Delta pct={x.change_pct} now={x.new} prev={x.previous} />}
                </div>
              );
            })}
            <div className="tile">
              <div className="tile-value">{nf(summary.active)}</div>
              <div className="tile-label">Currently active</div>
              <div className="hint">open now</div>
            </div>
            <div className="tile">
              <div className="tile-value">{nf(summary.new.companies)}</div>
              <div className="tile-label">Companies with new vacancies</div>
              <div className="hint">selected period</div>
            </div>
          </div>

          <div className="tiles kpis secondary">
            <div className="tile accent">
              <div className="tile-value">{nf(summary.new.total)}</div>
              <div className="tile-label">New in selected period</div>
              <Delta pct={summary.new.change_pct} now={summary.new.total} prev={summary.new.previous} />
            </div>
            <div className="tile">
              <div className="tile-value">{nf(summary.updated)}</div>
              <div className="tile-label">Updated</div>
              <div className="hint">vacancies with a change</div>
            </div>
            <div className="tile">
              <div className="tile-value">{nf(summary.closed)}</div>
              <div className="tile-label">Closed</div>
            </div>
            <div className="tile">
              <div className="tile-value">{nf(summary.reopened)}</div>
              <div className="tile-label">Reopened</div>
              <div className="hint">not counted as new</div>
            </div>
          </div>

          <DataQuality s={summary} />
        </>
      )}

      <div className="grid two">
        <Card title={`A · New vacancies per ${trends?.bucket ?? "day"}`}>
          {trends ? <ColumnChart points={trends.points} label="New vacancies over time" /> : <NoData>Loading…</NoData>}
          {trends && !trends.empty && <div className="hint">Total {nf(trends.total)} · bars are local {trends.bucket}s ({summary?.period.timezone})</div>}
        </Card>
        <Card title="D · By country">
          {countries ? <HBars rows={countryBars} label="New vacancies by country" empty="No new vacancies in this period" /> : <NoData>Loading…</NoData>}
          <div className="hint">Several-country and unknown-country vacancies are shown as their own rows; the rows add up to the total.</div>
        </Card>
        <Card title="B · Top specializations">
          {specs ? <HBars rows={specBars.slice(0, 10)} label="Top specializations" empty="No classified vacancies in this period" /> : <NoData>Loading…</NoData>}
        </Card>
        <Card title="C · Top cities">
          {cities ? <HBars rows={cityBars.slice(0, 10)} label="Top cities" empty="No vacancies with a single city in this period" /> : <NoData>Loading…</NoData>}
          <div className="hint">Multi-location, remote and unknown vacancies are in the table below, not here.</div>
        </Card>
      </div>

      <SpecTable specs={specs} view={view} setView={setView} sort={sortSpec} link={(r) => vacanciesLink(summary, flt, { specialization: r.slug })} />
      <CityTable
        cities={cities}
        view={view}
        setView={setView}
        sort={sortCity}
        link={(r) => vacanciesLink(summary, flt, rowOver(r))}
        drill={(r) => {
          if (r.kind === "country" && r.country_code) setFlt((f) => ({ ...f, countries: [r.country_code as string], regions: [] }));
          else if (r.kind === "region" && r.region_code) setFlt((f) => ({ ...f, regions: [r.region_code as string] }));
          setView((v) => ({ ...v, cityLevel: r.kind === "country" ? "region" : "city" }));
        }}
      />
      <MatrixCard matrix={matrix} view={view} setView={setView} link={(c, r) => vacanciesLink(summary, flt, { ...(c ? rowOver(c) : {}), specialization: r })} />
      <TelegramCard cfg={cfg} flt={flt} snap={summary?.snapshot_at ?? null} disabled={customIncomplete || !summary} />
    </div>
  );
}

function rowOver(r: StatCityRow) {
  if (r.kind === "country") return { country: r.country_code ?? undefined };
  if (r.kind === "region") return { country: r.country_code ?? undefined, region: r.region_code ?? undefined };
  if (r.kind === "city") return { city: r.city ?? "", country: r.country_code ?? undefined, region: r.region_code ?? undefined };
  return { country: r.country_code ?? undefined, region: r.region_code ?? undefined };
}

// --- filters ------------------------------------------------------------------------------------------------

function Filters({ cfg, flt, setFlt, toggle, regions, onRefresh, loading }: {
  cfg: StatConfig | null;
  flt: Flt;
  setFlt: (f: Flt | ((f: Flt) => Flt)) => void;
  toggle: (k: "countries" | "regions" | "specs", v: string) => void;
  regions: StatConfig["regions"];
  onRefresh: () => void;
  loading: boolean;
}) {
  const groups = (cfg?.specializations ?? []).filter((s) => s.parent_id === null);
  return (
    <div className="card stats-filters">
      <div className="row wrap">
        <label>
          Period
          <select value={flt.period} onChange={(e) => setFlt({ ...flt, period: e.target.value })}>
            {PERIODS.map(([k, l]) => (
              <option key={k} value={k}>
                {l}
              </option>
            ))}
          </select>
        </label>
        {flt.period === "custom" && (
          <>
            <label>
              From
              <input type="date" value={flt.from} max={flt.to || undefined} onChange={(e) => setFlt({ ...flt, from: e.target.value })} />
            </label>
            <label>
              To (inclusive)
              <input type="date" value={flt.to} min={flt.from || undefined} onChange={(e) => setFlt({ ...flt, to: e.target.value })} />
            </label>
          </>
        )}
        <label>
          Time zone
          <input list="tzs" placeholder={cfg?.timezone ?? "Europe/Berlin"} value={flt.tz} onChange={(e) => setFlt({ ...flt, tz: e.target.value })} />
          <datalist id="tzs">
            {["Europe/Berlin", "Europe/Vienna", "Europe/Zurich", "Europe/Kyiv", "UTC"].map((z) => (
              <option key={z} value={z} />
            ))}
          </datalist>
        </label>
        <fieldset className="chips">
          <legend>Country</legend>
          {[...(cfg?.countries ?? [])].sort((a, b) => ["DE", "AT", "CH"].indexOf(a.code) - ["DE", "AT", "CH"].indexOf(b.code)).map((c) => (
            <label key={c.code} className="check">
              <input type="checkbox" checked={flt.countries.includes(c.code)} onChange={() => toggle("countries", c.code)} /> {c.name}
            </label>
          ))}
          {!flt.countries.length && <span className="hint">All DACH</span>}
        </fieldset>
        <details className="multi">
          <summary>Region / canton {flt.regions.length ? `(${flt.regions.length})` : ""}</summary>
          <div className="multi-list">
            {regions.map((r) => (
              <label key={r.code} className="check">
                <input type="checkbox" checked={flt.regions.includes(r.code)} onChange={() => toggle("regions", r.code)} /> {r.name} <span className="hint">{r.code}</span>
              </label>
            ))}
          </div>
        </details>
        <label>
          City
          <input placeholder="exact name, e.g. Berlin" value={flt.city} onChange={(e) => setFlt({ ...flt, city: e.target.value })} />
        </label>
        <details className="multi">
          <summary>Specializations {flt.specs.length ? `(${flt.specs.length})` : "(all)"}</summary>
          <div className="multi-list">
            <label className="check strong">
              <input type="checkbox" checked={flt.specs.includes("unclassified")} onChange={() => toggle("specs", "unclassified")} /> Unclassified
            </label>
            {groups.map((g) => (
              <div key={g.id}>
                <label className="check strong">
                  <input type="checkbox" checked={flt.specs.includes(g.slug)} onChange={() => toggle("specs", g.slug)} /> {g.name_en}
                </label>
                {(cfg?.specializations ?? [])
                  .filter((s) => s.parent_id === g.id)
                  .map((s) => (
                    <label key={s.id} className="check indent">
                      <input type="checkbox" checked={flt.specs.includes(s.slug)} onChange={() => toggle("specs", s.slug)} /> {s.name_en}
                    </label>
                  ))}
              </div>
            ))}
          </div>
        </details>
        <label className="check" title="Leave out vacancies of companies whose first vacancy was discovered in the period (initial catalogue load)">
          <input type="checkbox" checked={flt.excludeInitial} onChange={(e) => setFlt({ ...flt, excludeInitial: e.target.checked })} /> Exclude initial loads
        </label>
        <button onClick={onRefresh} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </button>
        {(flt.countries.length > 0 || flt.regions.length > 0 || flt.city || flt.specs.length > 0 || flt.excludeInitial) && (
          <button className="link" onClick={() => setFlt({ ...flt, countries: [], regions: [], city: "", specs: [], excludeInitial: false })}>
            Clear filters
          </button>
        )}
      </div>
    </div>
  );
}

function DataQuality({ s }: { s: StatSummary }) {
  const d = s.data_quality;
  const pct = (x: number | null) => (x === null ? "–" : `${x.toLocaleString("de-DE")} %`);
  return (
    <details className="dq">
      <summary>
        Data quality of the {nf(d.new_total)} new vacancies{d.warnings.length > 0 && <span className="badge warn"> {d.warnings.length} note{d.warnings.length > 1 ? "s" : ""}</span>}
      </summary>
      {d.warnings.map((w) => (
        <div className="note warn" key={w.code}>
          {w.message}
        </div>
      ))}
      <ul className="dq-list">
        <li>With a specialization: {pct(d.classified_pct)}</li>
        <li>With a location: {pct(d.with_location_pct)}</li>
        <li>With a publication date from the employer: {pct(d.with_source_date_pct)} (not used for the "new" count)</li>
        <li>With a job.created event: {pct(d.created_event_pct)}; event log since {d.events_since ? fmtDate(d.events_since, true) : "–"}</li>
        <li>From companies first crawled in this period (initial load): {nf(d.initial_load)} ({pct(d.initial_load_pct)})</li>
      </ul>
    </details>
  );
}

// --- specialization table -------------------------------------------------------------------------------------

function SpecTable({ specs, view, setView, sort, link }: { specs: StatSpecs | null; view: View; setView: (f: (v: View) => View) => void; sort: (k: string) => void; link: (r: StatSpecRow) => string }) {
  return (
    <Card
      title="Specializations"
      actions={
        <div className="row wrap">
          <select value={view.specLevel} onChange={(e) => setView((v) => ({ ...v, specLevel: e.target.value as View["specLevel"] }))} aria-label="Level">
            <option value="specialization">Specializations</option>
            <option value="group">Groups</option>
          </select>
          <select value={view.specTop} onChange={(e) => setView((v) => ({ ...v, specTop: Number(e.target.value) }))} aria-label="Rows">
            <option value={5}>Top 5</option>
            <option value={10}>Top 10</option>
            <option value={0}>All</option>
          </select>
        </div>
      }
    >
      {!specs ? (
        <NoData>Loading…</NoData>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <SortTh label="Specialization" k="name" sort={view.specSort} order={view.specOrder} onSort={sort} />
                <SortTh label="New (period)" k="sel" sort={view.specSort} order={view.specOrder} onSort={sort} className="num" />
                <SortTh label="Previous" k="prev" sort={view.specSort} order={view.specOrder} onSort={sort} className="num" />
                <SortTh label="Change" k="change" sort={view.specSort} order={view.specOrder} onSort={sort} className="num" />
                <SortTh label="Today" k="today" sort={view.specSort} order={view.specOrder} onSort={sort} className="num" />
                <SortTh label="7 d" k="7d" sort={view.specSort} order={view.specOrder} onSort={sort} className="num" />
                <SortTh label="30 d" k="30d" sort={view.specSort} order={view.specOrder} onSort={sort} className="num" />
                <SortTh label="Active" k="active" sort={view.specSort} order={view.specOrder} onSort={sort} className="num" />
                <th className="num" title="Companies contributing new vacancies in the period">Companies</th>
                <th className="num" title="Career sources (pages / ATS feeds) that produced them">Sources</th>
                <th className="num" title="Vacancies that list it as secondary specialization too. Not additive.">Overlap*</th>
              </tr>
            </thead>
            <tbody>
              {specs.rows.length === 0 && (
                <tr>
                  <td colSpan={11} className="empty">
                    No vacancies
                  </td>
                </tr>
              )}
              {specs.rows.map((r) => (
                <tr key={r.key}>
                  <td>
                    <a href={link(r)} title="Open these vacancies">
                      {r.name}
                    </a>
                    {r.id === null && <span className="badge muted"> not classified</span>}
                    {r.is_group && r.id !== null && view.specLevel === "specialization" && <span className="badge muted"> group-level</span>}
                  </td>
                  <td className="num">
                    <b>{nf(r.sel)}</b>
                  </td>
                  <td className="num">{nf(r.prev)}</td>
                  <td className="num">
                    <Delta pct={r.change_pct} now={r.sel} prev={r.prev} />
                  </td>
                  <td className="num">{nf(r.today)}</td>
                  <td className="num">{nf(r["7d"])}</td>
                  <td className="num">{nf(r["30d"])}</td>
                  <td className="num">{nf(r.active)}</td>
                  <td className="num">{nf(r.companies)}</td>
                  <td className="num">{nf(r.sources)}</td>
                  <td className="num hint">{r.overlap === null ? "–" : nf(r.overlap)}</td>
                </tr>
              ))}
              {specs.rest && (
                <tr className="rest">
                  <td>… {specs.rest.rows} more</td>
                  <td className="num">{nf(specs.rest.sel)}</td>
                  <td className="num">{nf(specs.rest.prev)}</td>
                  <td />
                  <td className="num">{nf(specs.rest.today)}</td>
                  <td className="num">{nf(specs.rest["7d"])}</td>
                  <td className="num">{nf(specs.rest["30d"])}</td>
                  <td className="num">{nf(specs.rest.active)}</td>
                  <td colSpan={3} />
                </tr>
              )}
              <tr className="total">
                <td>Total (primary specialization)</td>
                <td className="num">{nf(specs.totals.sel)}</td>
                <td className="num">{nf(specs.totals.prev)}</td>
                <td className="num">
                  <Delta pct={specs.totals.change_pct ?? null} now={specs.totals.sel} prev={specs.totals.prev} />
                </td>
                <td className="num">{nf(specs.totals.today)}</td>
                <td className="num">{nf(specs.totals["7d"])}</td>
                <td className="num">{nf(specs.totals["30d"])}</td>
                <td className="num">{nf(specs.totals.active)}</td>
                <td colSpan={3} />
              </tr>
            </tbody>
          </table>
        </div>
      )}
      {specs && (
        <p className="hint">
          Completeness: {specs.completeness.classified_pct === null ? "–" : `${specs.completeness.classified_pct} %`} of the new vacancies have a specialization ({nf(specs.completeness.classified)} of{" "}
          {nf(specs.completeness.new_total)}). Each vacancy is counted once, under its primary specialization. *Overlap counts vacancies that also list it as secondary; it is not additive. "Today / 7 d /
          30 d / Active" do not depend on the selected period.
        </p>
      )}
    </Card>
  );
}

// --- city table ------------------------------------------------------------------------------------------------

function CityTable({ cities, view, setView, sort, link, drill }: { cities: StatCities | null; view: View; setView: (f: (v: View) => View) => void; sort: (k: string) => void; link: (r: StatCityRow) => string; drill: (r: StatCityRow) => void }) {
  return (
    <Card
      title="Cities"
      actions={
        <div className="row wrap">
          <select value={view.cityMode} onChange={(e) => setView((v) => ({ ...v, cityMode: e.target.value as View["cityMode"] }))} aria-label="Mode">
            <option value="unique">Unique job breakdown (adds up)</option>
            <option value="coverage">City coverage (overlapping)</option>
          </select>
          <select value={view.cityLevel} onChange={(e) => setView((v) => ({ ...v, cityLevel: e.target.value as View["cityLevel"] }))} aria-label="Level">
            <option value="city">Cities</option>
            <option value="region">Regions / cantons</option>
            <option value="country">Countries</option>
          </select>
          <select value={view.cityTop} onChange={(e) => setView((v) => ({ ...v, cityTop: Number(e.target.value) }))} aria-label="Rows">
            <option value={10}>Top 10</option>
            <option value={20}>Top 20</option>
            <option value={50}>Top 50</option>
            <option value={0}>All</option>
          </select>
        </div>
      }
    >
      {!cities ? (
        <NoData>Loading…</NoData>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Country</th>
                <th>Region / canton</th>
                <SortTh label={view.cityLevel === "city" ? "City" : view.cityLevel === "region" ? "Region" : "Country"} k="name" sort={view.citySort} order={view.cityOrder} onSort={sort} />
                <SortTh label="New (period)" k="sel" sort={view.citySort} order={view.cityOrder} onSort={sort} className="num" />
                <SortTh label="Change" k="change" sort={view.citySort} order={view.cityOrder} onSort={sort} className="num" />
                <SortTh label="Today" k="today" sort={view.citySort} order={view.cityOrder} onSort={sort} className="num" />
                <SortTh label="7 d" k="7d" sort={view.citySort} order={view.cityOrder} onSort={sort} className="num" />
                <SortTh label="30 d" k="30d" sort={view.citySort} order={view.cityOrder} onSort={sort} className="num" />
                <SortTh label="Active" k="active" sort={view.citySort} order={view.cityOrder} onSort={sort} className="num" />
                <th />
              </tr>
            </thead>
            <tbody>
              {cities.rows.length === 0 && (
                <tr>
                  <td colSpan={10} className="empty">
                    No vacancies
                  </td>
                </tr>
              )}
              {cities.rows.map((r) => (
                <tr key={r.key} className={r.kind === "multi" || r.kind === "remote" || r.kind === "unknown" ? "bucket" : ""}>
                  <td>{r.country ?? <span className="hint">–</span>}</td>
                  <td>{r.region ?? <span className="hint">–</span>}</td>
                  <td>
                    {r.clickable ? (
                      <a href={link(r)} title="Open these vacancies">
                        {r.label}
                      </a>
                    ) : (
                      <i title="Not a single place: shown separately so that the rows add up">{r.label}</i>
                    )}
                  </td>
                  <td className="num">
                    <b>{nf(r.sel)}</b>
                  </td>
                  <td className="num">
                    <Delta pct={r.change_pct} now={r.sel} prev={r.prev} />
                  </td>
                  <td className="num">{nf(r.today)}</td>
                  <td className="num">{nf(r["7d"])}</td>
                  <td className="num">{nf(r["30d"])}</td>
                  <td className="num">{nf(r.active)}</td>
                  <td>
                    {(r.kind === "country" || r.kind === "region") && (
                      <button className="small" onClick={() => drill(r)} title="Filter the whole dashboard to this place and go one level down">
                        Drill down
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {cities.rest && (
                <tr className="rest">
                  <td colSpan={3}>… {cities.rest.rows} more</td>
                  <td className="num">{nf(cities.rest.sel)}</td>
                  <td />
                  <td className="num">{nf(cities.rest.today)}</td>
                  <td className="num">{nf(cities.rest["7d"])}</td>
                  <td className="num">{nf(cities.rest["30d"])}</td>
                  <td className="num">{nf(cities.rest.active)}</td>
                  <td />
                </tr>
              )}
              {cities.totals && (
                <tr className="total">
                  <td colSpan={3}>Total (each vacancy once)</td>
                  <td className="num">{nf(cities.totals.sel)}</td>
                  <td />
                  <td className="num">{nf(cities.totals.today)}</td>
                  <td className="num">{nf(cities.totals["7d"])}</td>
                  <td className="num">{nf(cities.totals["30d"])}</td>
                  <td className="num">{nf(cities.totals.active)}</td>
                  <td />
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
      {cities && (
        <ul className="hint notes">
          {cities.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
          {!cities.additive && <li>This mode has no total: a vacancy listing three cities appears in all three rows.</li>}
        </ul>
      )}
    </Card>
  );
}

// --- specialization x city -----------------------------------------------------------------------------------------

function MatrixCard({ matrix, view, setView, link }: { matrix: StatMatrix | null; view: View; setView: (f: (v: View) => View) => void; link: (city: StatCityRow | null, spec: string) => string }) {
  const max = matrix ? Math.max(1, ...matrix.cells.flat()) : 1;
  return (
    <Card
      title="Specialization × city"
      actions={
        <select value={view.matrixLevel} onChange={(e) => setView((v) => ({ ...v, matrixLevel: e.target.value as View["matrixLevel"] }))} aria-label="Level">
          <option value="group">Groups</option>
          <option value="specialization">Specializations</option>
        </select>
      }
    >
      {!matrix ? (
        <NoData>Loading…</NoData>
      ) : matrix.empty ? (
        <NoData>No new vacancies in this period</NoData>
      ) : (
        <>
          <div className="table-wrap">
            <table className="table matrix">
              <thead>
                <tr>
                  <th>Specialization ↓ / city →</th>
                  {matrix.columns.map((c) => (
                    <th key={c.key} className="num" title={`${c.country ?? ""} ${c.region ?? ""}`}>
                      {c.label} <span className="hint">{c.country_code}</span>
                    </th>
                  ))}
                  <th className="num">Row total</th>
                </tr>
              </thead>
              <tbody>
                {matrix.rows.map((r, i) => (
                  <tr key={r.slug}>
                    <td>
                      <a href={link(null, r.slug)}>{r.name}</a>
                    </td>
                    {matrix.cells[i].map((n, j) => (
                      <td key={j} className="num cell" style={{ background: n ? `color-mix(in srgb, var(--accent) ${Math.round(12 + (n / max) * 58)}%, transparent)` : undefined }}>
                        {n ? <a href={link(matrix.columns[j], r.slug)}>{nf(n)}</a> : <span className="hint">·</span>}
                      </td>
                    ))}
                    <td className="num">
                      <b>{nf(r.total)}</b>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="hint">
            Each vacancy sits in exactly one cell (primary specialization × its single place). {nf(matrix.other.outside_shown)} are in rows/columns not shown, {nf(matrix.other.in_buckets)} are
            multi-location, remote or unknown. Total {nf(matrix.total)}.
          </p>
          <h3>Top combinations</h3>
          <ol className="combos">
            {matrix.top_combinations.slice(0, 10).map((c, i) => (
              <li key={i}>
                <b>{nf(c.count)}</b> · {c.specialization.name} · {c.place.label}
                {c.place.country_code && c.place.kind === "city" ? ` (${c.place.country_code})` : ""}
              </li>
            ))}
          </ol>
        </>
      )}
    </Card>
  );
}

// --- Telegram ------------------------------------------------------------------------------------------------------------

function TelegramCard({ cfg, flt, snap, disabled }: { cfg: StatConfig | null; flt: Flt; snap: string | null; disabled: boolean }) {
  const [opt, setOpt] = useState({ language: "uk", top_specializations: 5, top_cities: 5, include_bot_link: true, include_countries: true, format: "plain", bot_link: "" });
  const [prev, setPrev] = useState<TelegramPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [published, setPublished] = useState<StatPublication | null>(null);
  const [history, setHistory] = useState<StatPublication[]>([]);

  const body = () => {
    const b = baseParams(flt);
    return {
      period: b.period,
      from: b.from || null,
      to: b.to || null,
      timezone: b.timezone || null,
      country: b.country,
      region: b.region,
      city: b.city,
      specialization: b.specialization,
      exclude_initial_load: b.exclude_initial_load,
      as_of: snap,
      language: opt.language,
      top_specializations: opt.top_specializations,
      top_cities: opt.top_cities,
      include_bot_link: opt.include_bot_link,
      include_countries: opt.include_countries,
      format: opt.format,
      bot_link: opt.bot_link.trim() || null,
    };
  };

  const loadHistory = () =>
    api<{ items: StatPublication[] }>(`${BASE}/telegram-publications?limit=10`)
      .then((r) => setHistory(r.items))
      .catch(() => {});
  useEffect(() => {
    if (cfg?.telegram.enabled) loadHistory();
  }, [cfg]);

  const generate = async () => {
    setBusy(true);
    setError(null);
    setPublished(null);
    try {
      setPrev(await post<TelegramPreview>(`${BASE}/telegram-preview`, body()));
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    if (!prev) return;
    try {
      await navigator.clipboard.writeText(prev.text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setError("Copy failed: select the text and copy it manually.");
    }
  };

  const publish = async () => {
    if (!prev) return;
    if (!window.confirm("Publish this exact text to the Telegram channel now?")) return;
    setBusy(true);
    setError(null);
    try {
      setPublished(await post<StatPublication>(`${BASE}/telegram-publish`, { ...body(), approved_sha256: prev.sha256, confirm: true }));
      loadHistory();
    } catch (e) {
      setError(String((e as Error).message ?? e));
      loadHistory();
    } finally {
      setBusy(false);
    }
  };

  const set = <K extends keyof typeof opt>(k: K, v: (typeof opt)[K]) => {
    setOpt((o) => ({ ...o, [k]: v }));
    setPrev(null);
  };
  const tg = cfg?.telegram;

  return (
    <Card title="📢 Telegram post">
      <p className="hint block">Built from the same numbers, period, snapshot and filters as this dashboard. Nothing is estimated and no text is written by an AI.</p>
      <div className="row wrap">
        <label>
          Language
          <select value={opt.language} onChange={(e) => set("language", e.target.value)}>
            <option value="uk">Українська</option>
            <option value="ru">Русский</option>
            <option value="en">English</option>
            <option value="de">Deutsch</option>
          </select>
        </label>
        <label>
          Top specializations
          <select value={opt.top_specializations} onChange={(e) => set("top_specializations", Number(e.target.value))}>
            {[3, 5, 10].map((n) => (
              <option key={n}>{n}</option>
            ))}
          </select>
        </label>
        <label>
          Top cities
          <select value={opt.top_cities} onChange={(e) => set("top_cities", Number(e.target.value))}>
            {[3, 5, 10].map((n) => (
              <option key={n}>{n}</option>
            ))}
          </select>
        </label>
        <label>
          Format
          <select value={opt.format} onChange={(e) => set("format", e.target.value)}>
            <option value="plain">Plain text (paste into Telegram)</option>
            <option value="html">HTML (bot / API)</option>
            <option value="markdownv2">MarkdownV2 (bot / API)</option>
          </select>
        </label>
        <label className="grow">
          Bot link
          <input placeholder={tg?.bot_link ?? "https://t.me/your_bot"} value={opt.bot_link} onChange={(e) => set("bot_link", e.target.value)} />
        </label>
        <label className="check">
          <input type="checkbox" checked={opt.include_bot_link} onChange={(e) => set("include_bot_link", e.target.checked)} /> Bot link
        </label>
        <label className="check">
          <input type="checkbox" checked={opt.include_countries} onChange={(e) => set("include_countries", e.target.checked)} /> Country breakdown
        </label>
      </div>
      <div className="row wrap tg-actions">
        <button className="primary" onClick={generate} disabled={busy || disabled}>
          {prev ? "Regenerate" : "Generate Telegram Post"}
        </button>
        <button onClick={copy} disabled={!prev}>
          {copied ? "Copied ✓" : "Copy"}
        </button>
        {prev && <span className="hint">{prev.length} characters · {prev.parse_mode ?? "plain text"} · snapshot {fmtDate(prev.snapshot_at, true)}</span>}
      </div>
      <ErrorNote error={error} />
      {prev && (
        <>
          {prev.warnings.map((w) => (
            <div className="note warn" key={w}>
              {w}
            </div>
          ))}
          {prev.empty && <div className="note warn">No new vacancies in this period: nothing worth posting.</div>}
          <pre className="tg-preview" aria-label="Telegram post preview">
            {prev.text}
          </pre>
          {prev.parse_mode && <p className="hint">Formatted for the {prev.parse_mode} parse mode: it is meant for a bot or the API. Pasting it into the Telegram app shows the markup characters.</p>}
        </>
      )}

      {tg?.enabled ? (
        <div className="publish">
          <h3>Publish to the channel</h3>
          {!tg.configured && <div className="note warn">Publishing is enabled, but the bot token or chat id is missing on the server.</div>}
          <button onClick={publish} disabled={busy || !prev || !tg.configured || (prev?.empty && !tg.post_empty)} title="Posts exactly the previewed text. The same period and configuration can be published only once.">
            Publish this text…
          </button>
          {published && <div className="note ok">Published (message {published.message_id}).</div>}
          {history.length > 0 && (
            <table className="table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Status</th>
                  <th>Period</th>
                  <th>Info</th>
                </tr>
              </thead>
              <tbody>
                {history.map((h) => (
                  <tr key={h.id}>
                    <td>{fmtDate(h.sent_at ?? h.created_at, true)}</td>
                    <td>
                      <span className={`badge ${h.status === "sent" ? "ok" : h.status === "failed" ? "bad" : "info"}`}>{h.status}</span>
                    </td>
                    <td className="hint">
                      {fmtDate(h.period_start)} – {fmtDate(h.period_end)}
                    </td>
                    <td className="hint">{h.error ?? (h.message_id ? `message ${h.message_id}` : "")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : (
        <p className="hint">Publishing to a channel is switched off (default). Copy the text above, or enable it on the server (see docs/STATS.md).</p>
      )}
    </Card>
  );
}
