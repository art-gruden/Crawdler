import { useEffect, useState } from "react";
import { api, post, setUnauthorizedHandler } from "./api";
import Companies from "./pages/Companies";
import Crawler from "./pages/Crawler";
import Discovery from "./pages/Discovery";
import Jobs from "./pages/Jobs";
import JobStats from "./pages/JobStats";
import Login from "./pages/Login";
import { Attribution } from "./components/common";

type Page = "discovery" | "companies" | "jobs" | "stats" | "crawler";
type Route = { page: Page; params: URLSearchParams };
const PAGES: Page[] = ["discovery", "companies", "jobs", "stats", "crawler"];

function parseHash(): Route {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?");
  const page = (PAGES as string[]).includes(path) ? (path as Page) : "discovery";
  return { page, params: new URLSearchParams(query) };
}

export function navigate(page: Route["page"], params: Record<string, string> = {}) {
  const q = new URLSearchParams(params).toString();
  window.location.hash = `/${page}${q ? `?${q}` : ""}`;
}

export default function App() {
  const [user, setUser] = useState<string | null | undefined>(undefined);
  const [route, setRoute] = useState<Route>(parseHash);

  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null));
    api<{ username: string }>("/api/auth/me")
      .then((r) => setUser(r.username))
      .catch(() => setUser(null));
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  if (user === undefined) return <div className="loading">Loading…</div>;
  if (user === null) return <Login onLogin={setUser} />;

  return (
    <div className="app">
      <nav className="top">
        <span className="brand">Career Crawler</span>
        <a className={route.page === "discovery" ? "active" : ""} href="#/discovery">
          Discovery
        </a>
        <a className={route.page === "companies" ? "active" : ""} href="#/companies">
          Companies
        </a>
        <a className={route.page === "jobs" ? "active" : ""} href="#/jobs">
          Vacancies
        </a>
        <a className={route.page === "stats" ? "active" : ""} href="#/stats">
          📊 Job Statistics
        </a>
        <a className={route.page === "crawler" ? "active" : ""} href="#/crawler">
          Crawler
        </a>
        <span className="spacer" />
        <a href="/api/docs" target="_blank" rel="noopener noreferrer">
          API docs
        </a>
        <span className="user">{user}</span>
        <button className="link" onClick={() => post("/api/auth/logout").finally(() => setUser(null))}>
          Sign out
        </button>
      </nav>
      <main>
        {route.page === "companies" && <Companies params={route.params} />}
        {route.page === "jobs" && <Jobs key={route.params.toString()} params={route.params} />}
        {route.page === "stats" && <JobStats />}
        {route.page === "crawler" && <Crawler />}
        {route.page === "discovery" && <Discovery />}
      </main>
      <Attribution />
    </div>
  );
}
