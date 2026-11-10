// Hand-drawn SVG/CSS charts for the statistics tab (no chart library). Real data only:
// an empty series shows a message instead of an empty frame.

export function NoData({ children }: { children?: string }) {
  return <div className="nodata">{children ?? "No data for this period"}</div>;
}

const nf = (n: number) => n.toLocaleString("de-DE");

/** Chart A: one column per day (or hour). */
export function ColumnChart({ points, label }: { points: { t: string; label: string; count: number }[]; label: string }) {
  const max = Math.max(1, ...points.map((p) => p.count));
  if (!points.length || points.every((p) => p.count === 0)) return <NoData />;
  const w = 720;
  const h = 180;
  const pad = { l: 36, r: 8, t: 10, b: 26 };
  const iw = w - pad.l - pad.r;
  const ih = h - pad.t - pad.b;
  const bw = iw / points.length;
  const ticks = [0, Math.round(max / 2), max];
  const every = Math.ceil(points.length / 10); // at most ~10 x labels
  return (
    <svg className="chart" viewBox={`0 0 ${w} ${h}`} role="img" aria-label={label}>
      {ticks.map((v) => {
        const y = pad.t + ih - (v / max) * ih;
        return (
          <g key={v}>
            <line x1={pad.l} x2={w - pad.r} y1={y} y2={y} className="grid" />
            <text x={pad.l - 6} y={y + 4} textAnchor="end" className="axis">
              {nf(v)}
            </text>
          </g>
        );
      })}
      {points.map((p, i) => {
        const bh = (p.count / max) * ih;
        const x = pad.l + i * bw;
        return (
          <g key={p.t}>
            <rect x={x + bw * 0.12} width={Math.max(1, bw * 0.76)} y={pad.t + ih - bh} height={bh} className="bar">
              <title>
                {p.label}: {nf(p.count)}
              </title>
            </rect>
            {points.length <= 16 && p.count > 0 && (
              <text x={x + bw / 2} y={pad.t + ih - bh - 3} textAnchor="middle" className="val">
                {nf(p.count)}
              </text>
            )}
            {i % every === 0 && (
              <text x={x + bw / 2} y={h - 8} textAnchor="middle" className="axis">
                {p.label.length > 5 && p.label.includes("-") ? p.label.slice(5) : p.label}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

/** Charts B, C, D: horizontal bars with the value written next to each bar. */
export function HBars({ rows, label, empty }: { rows: { label: string; value: number; href?: string; note?: string }[]; label: string; empty?: string }) {
  const shown = rows.filter((r) => r.value > 0);
  if (!shown.length) return <NoData>{empty}</NoData>;
  const max = Math.max(...shown.map((r) => r.value));
  return (
    <ul className="hbars" aria-label={label}>
      {shown.map((r) => (
        <li key={r.label}>
          <span className="hb-label" title={r.label}>
            {r.href ? <a href={r.href}>{r.label}</a> : r.label}
          </span>
          <span className="hb-track">
            <span className="hb-bar" style={{ width: `${Math.max(2, (r.value / max) * 100)}%` }} />
          </span>
          <span className="hb-val">
            {nf(r.value)}
            {r.note && <span className="hint"> {r.note}</span>}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function Delta({ pct, now, prev }: { pct: number | null; now: number; prev: number }) {
  if (pct === null) {
    if (prev === 0 && now > 0) return <span className="delta flat" title="No vacancies in the previous period">new</span>;
    return <span className="delta flat">–</span>;
  }
  const dir = pct > 0 ? "up" : pct < 0 ? "down" : "flat";
  const arrow = pct > 0 ? "▲" : pct < 0 ? "▼" : "■";
  return (
    <span className={`delta ${dir}`} title={`Previous comparable period: ${nf(prev)}`}>
      {arrow} {pct > 0 ? "+" : ""}
      {pct.toLocaleString("de-DE", { maximumFractionDigits: 1 })} %
    </span>
  );
}
