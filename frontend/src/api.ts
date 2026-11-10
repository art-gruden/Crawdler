// Thin fetch wrapper. The session lives in an HttpOnly cookie; unsafe methods send the
// X-Requested-With header the API requires (CSRF protection).

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

type Query = Record<string, string | number | boolean | undefined | null>;

export function qs(params: Query): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "" && v !== false) sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

/** Like qs(), but arrays become repeated parameters (country=DE&country=AT). */
export function qsMulti(params: Record<string, string | number | boolean | undefined | null | string[]>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (Array.isArray(v)) v.filter((x) => x !== "").forEach((x) => sp.append(k, x));
    else if (v !== undefined && v !== null && v !== "" && v !== false) sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const method = (init.method || "GET").toUpperCase();
  if (method !== "GET") headers.set("X-Requested-With", "gcc-ui");
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...init, headers, credentials: "same-origin" });
  if (res.status === 401 && !path.startsWith("/api/auth/login")) onUnauthorized();
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") msg = body.detail;
      else if (Array.isArray(body.detail)) msg = body.detail.map((d: { msg: string }) => d.msg).join("; ");
      else if (body.detail && typeof body.detail.message === "string") msg = body.detail.message;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, msg);
  }
  return (await res.json()) as T;
}

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body) });
