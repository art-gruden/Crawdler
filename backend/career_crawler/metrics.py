"""Per-minute counters, accumulated in memory and flushed to ``crawl_metrics`` with upserts."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .fetch import FetchStat
from .models import CrawlMetric

COUNTERS = (
    "pages",
    "bytes",
    "requests",
    "status_429",
    "status_503",
    "errors",
    "latency_ms_sum",
    "companies_done",
    "jobs_found",
    "jobs_new",
    "browser_pages",
)


class Metrics:
    def __init__(self, worker: str):
        self.worker = worker[:64]
        self._c: dict[str, int] = defaultdict(int)
        self._by_method: dict[str, dict[str, int]] = {}

    def on_fetch(self, st: FetchStat) -> None:
        self._c["requests"] += 1
        self._c["latency_ms_sum"] += st.elapsed_ms
        if st.error:
            self._c["errors"] += 1
            return
        self._c["bytes"] += st.bytes
        if st.status == 429:
            self._c["status_429"] += 1
        elif st.status == 503:
            self._c["status_503"] += 1
        elif st.status and st.status < 400:
            self._c["pages"] += 1

    def browser_page(self) -> None:
        self._c["browser_pages"] += 1

    def company_done(self, strategy_stats: dict, jobs_found: int, jobs_new: int) -> None:
        self._c["companies_done"] += 1
        self._c["jobs_found"] += jobs_found
        self._c["jobs_new"] += jobs_new
        for name, st in strategy_stats.items():
            m = self._by_method.setdefault(name, {"ok": 0, "fail": 0, "jobs": 0})
            m["ok"] += st.get("ok", 0)
            m["fail"] += st.get("attempts", 0) - st.get("ok", 0)
            m["jobs"] += st.get("jobs", 0)

    def flush(self, s: Session, resources: dict | None = None) -> None:
        if not self._c and not self._by_method and not resources:
            return
        bucket = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0)
        res = resources or {}
        values = {k: self._c.get(k, 0) for k in COUNTERS}
        snap = {
            "cpu_pct": res.get("cpu_pct"),
            "mem_available_mb": res.get("mem_available_mb"),
            "container_mem_mb": res.get("container_mem_mb"),
            "http_limit": res.get("http_limit"),
            "http_active": res.get("http_active"),
            "browser_active": res.get("browser_active"),
            "browser_paused": (not res["browser_allowed"]) if "browser_allowed" in res else None,
        }
        stmt = insert(CrawlMetric).values(bucket=bucket, worker=self.worker, by_method={}, **values, **snap)
        set_ = {k: getattr(CrawlMetric, k) + getattr(stmt.excluded, k) for k in COUNTERS}
        set_.update({k: getattr(stmt.excluded, k) for k, v in snap.items() if v is not None})
        s.execute(stmt.on_conflict_do_update(index_elements=["bucket", "worker"], set_=set_))
        if self._by_method:  # one row per worker and minute: read-modify-write is contention free
            row = s.get(CrawlMetric, (bucket, self.worker), with_for_update=True)
            merged = {k: dict(v) for k, v in (row.by_method or {}).items()}
            for name, add in self._by_method.items():
                cur = merged.setdefault(name, {"ok": 0, "fail": 0, "jobs": 0})
                for k, v in add.items():
                    cur[k] = cur.get(k, 0) + v
            row.by_method = merged
        self._c.clear()
        self._by_method = {}
