import type { ReactNode } from "react";

const VERIFICATION_LABEL: Record<string, string> = {
  unverified: "Not checked",
  verified: "Verified",
  redirected: "Redirects elsewhere",
  robots_disallowed: "robots.txt: no",
  access_restricted: "Access restricted",
  parked: "Parked domain",
  http_error: "HTTP error",
  rate_limited: "Rate-limited",
  unreachable: "Unreachable",
  dns_error: "DNS error",
  blocked_address: "Blocked address",
  robots_unavailable: "robots.txt error",
  no_website: "No website",
};

const TONE: Record<string, string> = {
  verified: "ok",
  completed: "ok",
  queued: "info",
  scanning: "info",
  done: "ok",
  running: "info",
  pending: "muted",
  unverified: "muted",
  not_queued: "muted",
  no_website: "muted",
  cancelled: "muted",
  failed: "bad",
  dns_error: "bad",
  blocked_address: "bad",
  parked: "warn",
  robots_disallowed: "warn",
  access_restricted: "warn",
  redirected: "warn",
  http_error: "warn",
  rate_limited: "warn",
  unreachable: "warn",
  robots_unavailable: "warn",
  new: "ok",
  updated: "muted",
  duplicate_existing: "info",
  duplicate_in_job: "info",
  invalid_url: "warn",
};

export const VERIFICATION_STATUSES = Object.keys(VERIFICATION_LABEL);

export function Badge({ value, label, title }: { value: string; label?: string; title?: string }) {
  return (
    <span className={`badge ${TONE[value] ?? "muted"}`} title={title}>
      {label ?? VERIFICATION_LABEL[value] ?? value.replace(/_/g, " ")}
    </span>
  );
}

export function verificationLabel(v: string) {
  return VERIFICATION_LABEL[v] ?? v;
}

export function Attribution() {
  return (
    <footer className="attribution">
      Company locations and websites partly from{" "}
      <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">
        © OpenStreetMap contributors
      </a>
      , available under the Open Database License (ODbL 1.0). Imported directories keep their own source and licence per record.
    </footer>
  );
}

export function fmtDate(iso: string | null | undefined, withTime = false) {
  if (!iso) return "–";
  const d = new Date(iso);
  return withTime
    ? d.toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" })
    : d.toLocaleDateString("de-DE");
}

export function Card({ title, children, actions }: { title: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="card">
      <header>
        <h2>{title}</h2>
        {actions}
      </header>
      {children}
    </section>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="note bad">{error}</div>;
}

/** Only http(s) links are rendered as links. */
export function SafeLink({ href, children }: { href: string | null; children: ReactNode }) {
  if (!href || !/^https?:\/\//i.test(href)) return <>{children}</>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer nofollow">
      {children}
    </a>
  );
}
