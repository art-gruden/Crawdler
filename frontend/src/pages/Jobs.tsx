import { useEffect, useState } from "react";
import { api, qs } from "../api";
import CrawlDrawer from "../components/CrawlDrawer";
import { Badge, ErrorNote, SafeLink, fmtDate } from "../components/common";
import type { Vacancy, VacancyDetail } from "../types";

const EMPLOYMENT = ["full_time", "part_time", "permanent", "contract", "working_student", "internship", "apprenticeship", "thesis"];
const METHODS = ["ats", "jsonld", "html", "content", "pdf", "browser", "llm"];
const PAGE = 50;

function salary(j: Vacancy) {
  if (j.salary_min === null) return null;
  const f = (v: number) => v.toLocaleString("de-DE", { maximumFractionDigits: 0 });
  const range = j.salary_max && j.salary_max !== j.salary_min ? `${f(j.salary_min)}–${f(j.salary_max)}` : f(j.salary_min);
  return `${range} ${j.salary_currency ?? ""}${j.salary_period ? ` / ${j.salary_period}` : ""}`;
}

export default function Jobs({ params }: { params: URLSearchParams }) {
  const [f, setF] = useState(() => ({
    q: params.get("q") ?? "",
    city: params.get("city") ?? "",
    remote_type: params.get("remote_type") ?? "",
    employment_type: params.get("employment_type") ?? "",
    skill: params.get("skill") ?? "",
    method: params.get("method") ?? "",
    new_days: params.get("new_days") ?? "",
    status: params.get("status") ?? "open",
    company_id: params.get("company_id") ?? "",
    sort: params.get("sort") ?? "discovered",
  }));
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<{ total: number; items: Vacancy[] } | null>(null);
  const [skills, setSkills] = useState<{ skill: string; jobs: number }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<number | null>(null);

  useEffect(() => {
    api<{ skill: string; jobs: number }[]>("/api/jobs/skills?limit=30").then(setSkills).catch(() => {});
  }, []);

  useEffect(() => {
    const t = setTimeout(() => {
      api<{ total: number; items: Vacancy[] }>(`/api/jobs${qs({ ...f, sort: f.q && f.sort === "discovered" ? "relevance" : f.sort, limit: PAGE, offset })}`)
        .then((d) => {
          setData(d);
          setError(null);
        })
        .catch((e) => setError(String(e.message ?? e)));
    }, 250);
    return () => clearTimeout(t);
  }, [f, offset]);

  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => {
    setOffset(0);
    setF({ ...f, [k]: e.target.value });
  };

  return (
    <div>
      <h1>Vacancies</h1>
      <div className="filters">
        <input className="search" placeholder="Search title, location, description…  e.g. kubernetes -werkstudent" value={f.q} onChange={set("q")} aria-label="Search" />
        <input placeholder="City" value={f.city} onChange={set("city")} aria-label="City" />
        <select value={f.remote_type} onChange={set("remote_type")} aria-label="Remote">
          <option value="">Any work model</option>
          <option value="remote">Remote</option>
          <option value="hybrid">Hybrid</option>
          <option value="onsite">On-site</option>
        </select>
        <select value={f.employment_type} onChange={set("employment_type")} aria-label="Employment type">
          <option value="">Any employment</option>
          {EMPLOYMENT.map((x) => (
            <option key={x} value={x}>
              {x.replace(/_/g, " ")}
            </option>
          ))}
        </select>
        <select value={f.skill} onChange={set("skill")} aria-label="Skill">
          <option value="">Any skill</option>
          {skills.map((s) => (
            <option key={s.skill} value={s.skill}>
              {s.skill} ({s.jobs})
            </option>
          ))}
        </select>
        <select value={f.new_days} onChange={set("new_days")} aria-label="New">
          <option value="">Any time</option>
          <option value="1">New today</option>
          <option value="7">New this week</option>
          <option value="30">New this month</option>
        </select>
        <select value={f.method} onChange={set("method")} aria-label="Extraction method">
          <option value="">Any method</option>
          {METHODS.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        <select value={f.status} onChange={set("status")} aria-label="Status">
          <option value="open">Open</option>
          <option value="closed">Closed</option>
          <option value="expired">Expired</option>
          <option value="all">All</option>
        </select>
        <select value={f.sort} onChange={set("sort")} aria-label="Sort">
          <option value="discovered">Newest discovered</option>
          <option value="posted">Newest published</option>
          <option value="company">Company</option>
          <option value="title">Title</option>
        </select>
        {f.company_id && (
          <span className="chip">
            company #{f.company_id}
            <button className="link" onClick={() => setF({ ...f, company_id: "" })} aria-label="Remove company filter">
              ×
            </button>
          </span>
        )}
      </div>
      <ErrorNote error={error} />
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Title</th>
              <th>Company</th>
              <th>Location</th>
              <th>Type</th>
              <th>Published</th>
              <th>Discovered</th>
              <th>Method</th>
            </tr>
          </thead>
          <tbody>
            {data && data.items.length === 0 && (
              <tr>
                <td colSpan={7} className="empty">
                  No vacancies match these filters
                </td>
              </tr>
            )}
            {data?.items.map((j) => (
              <tr key={j.id}>
                <td>
                  <button className="link name" onClick={() => setDetail(j.id)}>
                    {j.title}
                  </button>
                  <div>
                    {j.skills.slice(0, 6).map((s) => (
                      <span key={s} className="tag">
                        {s}
                      </span>
                    ))}
                  </div>
                </td>
                <td>
                  <button className="link" onClick={() => setF({ ...f, company_id: String(j.company_id) })}>
                    {j.company_name}
                  </button>
                  <div className="hint">{j.company_domain}</div>
                </td>
                <td>
                  {j.city ?? j.location_raw ?? "–"} {j.remote_type !== "unknown" && <Badge value={j.remote_type} />}
                </td>
                <td className="hint">{j.employment_types.map((e) => e.replace(/_/g, " ")).join(", ") || "–"}</td>
                <td>{j.date_posted ? fmtDate(j.date_posted) : <span className="hint">not stated</span>}</td>
                <td>{fmtDate(j.discovered_at)}</td>
                <td>
                  <Badge value={j.extraction_method} />
                  {j.status !== "open" && <Badge value={j.status} />}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data && (
        <div className="pager">
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
            ← Previous
          </button>
          <span className="hint">
            {data.total === 0 ? 0 : offset + 1}–{Math.min(offset + PAGE, data.total)} of {data.total}
          </span>
          <button disabled={offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)}>
            Next →
          </button>
        </div>
      )}
      {detail !== null && <JobDrawer id={detail} onClose={() => setDetail(null)} />}
    </div>
  );
}

function JobDrawer({ id, onClose }: { id: number; onClose: () => void }) {
  const [j, setJ] = useState<VacancyDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [crawl, setCrawl] = useState(false);

  useEffect(() => {
    setJ(null);
    api<VacancyDetail>(`/api/jobs/${id}`)
      .then(setJ)
      .catch((e) => setError(String(e.message ?? e)));
  }, [id]);

  if (crawl && j) return <CrawlDrawer companyId={j.company_id} title={j.company_name} onClose={() => setCrawl(false)} />;

  return (
    <aside className="drawer" role="dialog" aria-label="Vacancy details">
      <div className="drawer-head">
        <h2>{j?.title ?? "…"}</h2>
        <button className="link" onClick={onClose} aria-label="Close">
          ×
        </button>
      </div>
      <ErrorNote error={error} />
      {j && (
        <div className="stack">
          <dl className="kv">
            <dt>Company</dt>
            <dd>
              {j.company_name}{" "}
              <a href={`#/jobs?company_id=${j.company_id}&status=all`}>all postings</a> ·{" "}
              <button className="link" onClick={() => setCrawl(true)}>
                crawl details
              </button>
            </dd>
            <dt>Location</dt>
            <dd>
              {[j.postcode, j.city].filter(Boolean).join(" ") || "–"} {j.remote_type !== "unknown" && <Badge value={j.remote_type} />}
              {j.location_raw && <div className="hint">as published: {j.location_raw}</div>}
            </dd>
            <dt>Employment</dt>
            <dd>{j.employment_types.map((e) => e.replace(/_/g, " ")).join(", ") || "–"}</dd>
            {salary(j) && (
              <>
                <dt>Salary</dt>
                <dd>{salary(j)}</dd>
              </>
            )}
            <dt>Published</dt>
            <dd>{j.date_posted ? fmtDate(j.date_posted) : <span className="hint">not stated by the source</span>}</dd>
            <dt>Discovered</dt>
            <dd>
              {fmtDate(j.discovered_at, true)} · last seen {fmtDate(j.last_seen_at, true)}
              {j.miss_count > 0 && <span className="hint"> · missing in {j.miss_count} scan(s)</span>}
            </dd>
            <dt>Status</dt>
            <dd>
              <Badge value={j.status} /> {j.closed_at && <span className="hint">since {fmtDate(j.closed_at)}</span>}
            </dd>
            <dt>Languages</dt>
            <dd>
              {j.languages.length === 0 && "–"}
              {j.languages.map((l) => (
                <span key={l.language} className="tag" title={l.raw}>
                  {l.language.toUpperCase()} {l.level ?? ""} {l.required ? "" : "(optional)"}
                </span>
              ))}
            </dd>
            <dt>Skills</dt>
            <dd>
              {j.skills.map((s) => (
                <span key={s} className="tag">
                  {s}
                </span>
              ))}
              {j.skills.length === 0 && "–"}
            </dd>
            <dt>Links</dt>
            <dd className="stack">
              <SafeLink href={j.source_url}>Posting</SafeLink>
              {j.application_url && <SafeLink href={j.application_url}>Apply</SafeLink>}
              {j.career_page_url && <SafeLink href={j.career_page_url}>Career page</SafeLink>}
            </dd>
            <dt>Extraction</dt>
            <dd>
              <Badge value={j.extraction_method} /> confidence {Math.round(j.confidence * 100)} %
              {j.ats_type && (
                <span className="hint">
                  {" "}
                  · {j.ats_type} #{j.ats_job_id}
                </span>
              )}
            </dd>
          </dl>
          {j.responsibilities.length > 0 && (
            <>
              <h3>Responsibilities</h3>
              <ul>
                {j.responsibilities.map((x, i) => (
                  <li key={i}>{x}</li>
                ))}
              </ul>
            </>
          )}
          {j.requirements.length > 0 && (
            <>
              <h3>Requirements</h3>
              <ul>
                {j.requirements.map((x, i) => (
                  <li key={i}>{x}</li>
                ))}
              </ul>
            </>
          )}
          {j.description && (
            <>
              <h3>Description</h3>
              {/* plain text from the API, rendered as text: no HTML from third-party pages reaches the DOM */}
              <div className="description">{j.description}</div>
            </>
          )}
        </div>
      )}
    </aside>
  );
}
