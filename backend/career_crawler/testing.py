"""Safe test mode: serve every request from local fixture directories.

Layout::

    <fixtures>/<host>/index.html          -> https://<host>/
    <fixtures>/<host>/karriere.html       -> https://<host>/karriere   (or karriere/index.html)
    <fixtures>/<host>/_routes.json        -> overrides: {"/path?query": {"status": 429, "headers": {...},
                                              "file": "x.html", "location": "https://other/", "delay_s": 20,
                                              "content_type": "..."}}

The SSRF-safe client still runs end to end (DNS check, IP pinning, redirects, robots,
size limits); only the transport and resolver are replaced, so nothing leaves the host.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from gcc_core.netsafe import DnsError

FIXTURE_IP = "93.184.215.14"  # public-looking address; never connected to


class FixtureTransport(httpx.AsyncBaseTransport):
    def __init__(self, root: str | Path, *, sleep=asyncio.sleep):
        self.root = Path(root)
        self._routes: dict[str, dict] = {}
        self.requests: list[str] = []
        self._sleep = sleep

    def hosts(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith("_"))

    def _routes_for(self, host: str) -> dict:
        if host not in self._routes:
            f = self.root / host / "_routes.json"
            self._routes[host] = json.loads(f.read_text("utf-8")) if f.exists() else {}
        return self._routes[host]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "").split(":")[0].lower()
        parts = urlsplit(str(request.url))
        path = unquote(parts.path) or "/"
        key = path + (f"?{parts.query}" if parts.query else "")
        self.requests.append(f"{host}{key}")
        site = self.root / host
        if not site.is_dir():
            raise httpx.ConnectError(f"no fixture site {host}")
        route = self._routes_for(host).get(key) or self._routes_for(host).get(path)
        headers: dict[str, str] = {}
        status = 200
        file = None
        if route:
            if route.get("delay_s"):
                await self._sleep(float(route["delay_s"]))
                raise httpx.ReadTimeout("fixture timeout", request=request)
            status = int(route.get("status", 200))
            headers.update(route.get("headers", {}))
            if route.get("location"):
                headers["location"] = route["location"]
            if route.get("file"):
                file = site / route["file"]
            if route.get("body") is not None:
                body = route["body"].encode("utf-8")
                headers.setdefault("content-type", route.get("content_type", "text/html; charset=utf-8"))
                return httpx.Response(status, headers=headers, content=body, request=request)
            if file is None and (status >= 300):
                return httpx.Response(status, headers=headers, content=b"", request=request)
        if file is None:
            file = self._resolve(site, path, parts.query)
        if file is None or not file.is_file():
            return httpx.Response(
                404, headers={"content-type": "text/html"}, content=b"<h1>Not found</h1>", request=request
            )
        if (inm := request.headers.get("if-none-match")) and (et := headers.get("etag")) and inm == et:
            return httpx.Response(304, headers=headers, content=b"", request=request)
        ctype = (route or {}).get("content_type") or _ctype(file)
        headers.setdefault("content-type", ctype)
        return httpx.Response(status, headers=headers, content=file.read_bytes(), request=request)

    @staticmethod
    def _resolve(site: Path, path: str, query: str) -> Path | None:
        rel = path.strip("/")
        if ".." in rel.split("/"):
            return None
        candidates = []
        if query:
            candidates.append(site / f"{rel or 'index'}__{query.replace('=', '-').replace('&', '_')}.html")
        if not rel:
            candidates.append(site / "index.html")
        else:
            candidates += [site / rel, site / f"{rel}.html", site / rel / "index.html"]
        return next((c for c in candidates if c.is_file()), None)


def _ctype(file: Path) -> str:
    if file.name == "robots.txt":
        return "text/plain"
    if file.suffix in (".html", ".htm") or not file.suffix:
        return "text/html; charset=utf-8"
    return mimetypes.guess_type(file.name)[0] or "application/octet-stream"


def fixture_resolver(transport: FixtureTransport, overrides: dict[str, str] | None = None):
    """Fixture hosts resolve to a public-looking IP; ``overrides`` can map a host to any address
    (e.g. a private one, to test the SSRF guard). IP literals resolve to themselves."""
    import ipaddress

    hosts = set(transport.hosts())
    overrides = {k.lower(): v for k, v in (overrides or {}).items()}

    async def resolve(host: str, port: int) -> list[str]:
        h = host.lower().strip("[]")
        if h in overrides:
            return [overrides[h]]
        try:
            ipaddress.ip_address(h)
            return [h]
        except ValueError:
            pass
        if h in hosts:
            return [FIXTURE_IP]
        raise DnsError(f"cannot resolve {host} (test mode)")

    return resolve
