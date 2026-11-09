"""Resource controller: measures the host and the crawl, and moves concurrency gradually.

Signals: host MemAvailable, CPU utilization, container memory (cgroup v2/v1), active
HTTP requests and browser pages, queue sizes, response latency (EWMA) and the share of
HTTP 429/503 answers in a sliding window.

Policy (AIMD with hysteresis):
* **decrease** (×0.7, at most once per cooldown) when CPU > 85 %, MemAvailable < low
  watermark, container memory > 85 % of its limit, latency > 3× baseline, or 429/503
  share > 5 %;
* **increase** (+1) only after ``healthy_samples`` consecutive healthy samples with clear
  headroom (CPU < 60 %, MemAvailable > high watermark, 429/503 < 1 %);
* between the two bands nothing changes — that gap is the hysteresis.
* Browser work pauses below ``browser_mem_pause_mb`` and resumes only above
  ``browser_mem_resume_mb``.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import CrawlerSettings


@dataclass
class Sample:
    ts: float
    cpu_pct: float | None
    mem_available_mb: int | None
    container_mem_mb: int | None
    container_limit_mb: int | None
    http_active: int
    browser_active: int
    latency_ms: float | None
    error_rate: float  # 429/503 share in the window
    queue_pending: int | None = None


def _read_int(path: str) -> int | None:
    try:
        raw = Path(path).read_text().strip()
    except OSError:
        return None
    if raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def container_memory_mb() -> tuple[int | None, int | None]:
    """(usage, limit) of this container from cgroup v2, falling back to v1."""
    usage = _read_int("/sys/fs/cgroup/memory.current")
    limit = _read_int("/sys/fs/cgroup/memory.max")
    if usage is None:
        usage = _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        limit = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    mb = 1024 * 1024
    if limit is not None and limit > 1 << 50:  # "unlimited" on v1
        limit = None
    return (usage // mb if usage is not None else None, limit // mb if limit is not None else None)


class Probe:
    """System measurements (psutil when available, /proc otherwise)."""

    def __init__(self):
        try:
            import psutil

            self._psutil = psutil
            psutil.cpu_percent(interval=None)  # prime
        except ImportError:  # pragma: no cover
            self._psutil = None

    def cpu_pct(self) -> float | None:
        return self._psutil.cpu_percent(interval=None) if self._psutil else None

    def mem_available_mb(self) -> int | None:
        if self._psutil:
            return int(self._psutil.virtual_memory().available / 1024 / 1024)
        for line in Path("/proc/meminfo").read_text().splitlines():  # pragma: no cover
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
        return None  # pragma: no cover

    def container(self) -> tuple[int | None, int | None]:
        return container_memory_mb()


@dataclass
class Decision:
    http_limit: int
    browser_allowed: bool
    reason: str


class ResourceController:
    def __init__(
        self,
        settings: CrawlerSettings,
        probe: Probe | None = None,
        clock=time.monotonic,
        *,
        mem_low_mb: int = 450,
        mem_high_mb: int = 900,
        cooldown_s: float = 10.0,
        healthy_samples: int = 3,
    ):
        self.s = settings
        self.probe = probe or Probe()
        self.clock = clock
        self.http_limit = max(settings.http_concurrency_min, min(settings.http_concurrency, 8))  # start in the middle
        self.browser_allowed = True
        self.mem_low_mb = mem_low_mb
        self.mem_high_mb = mem_high_mb
        self.cooldown_s = cooldown_s
        self.healthy_needed = healthy_samples
        self._healthy = 0
        self._last_change = -1e9
        self._latency_ewma: float | None = None
        self._latency_base: float | None = None
        self._window: deque[tuple[float, int]] = deque(maxlen=500)  # (ts, status)
        self.http_active = 0
        self.browser_active = 0
        self.last: Sample | None = None
        self.last_decision = Decision(self.http_limit, True, "start")

    # --- inputs ---------------------------------------------------------------------------------

    def observe(self, status: int | None, latency_ms: int) -> None:
        self._window.append((self.clock(), status or 0))
        if status and status < 500 and status != 429:
            a = 0.2
            self._latency_ewma = (
                latency_ms if self._latency_ewma is None else (1 - a) * self._latency_ewma + a * latency_ms
            )
            # the baseline adapts slowly so a gradual slowdown is still noticed
            self._latency_base = (
                latency_ms
                if self._latency_base is None
                else min(self._latency_base * 1.01, 0.98 * self._latency_base + 0.02 * latency_ms)
            )

    def error_rate(self, horizon_s: float = 120.0) -> float:
        cutoff = self.clock() - horizon_s
        recent = [st for ts, st in self._window if ts >= cutoff]
        if len(recent) < 10:
            return 0.0
        return sum(1 for st in recent if st in (429, 503)) / len(recent)

    def sample(self, queue_pending: int | None = None) -> Sample:
        used, limit = self.probe.container()
        self.last = Sample(
            ts=self.clock(),
            cpu_pct=self.probe.cpu_pct(),
            mem_available_mb=self.probe.mem_available_mb(),
            container_mem_mb=used,
            container_limit_mb=limit,
            http_active=self.http_active,
            browser_active=self.browser_active,
            latency_ms=round(self._latency_ewma, 1) if self._latency_ewma is not None else None,
            error_rate=round(self.error_rate(), 4),
            queue_pending=queue_pending,
        )
        return self.last

    # --- policy ---------------------------------------------------------------------------------

    def decide(self, sample: Sample | None = None) -> Decision:
        smp = sample or self.sample()
        now = self.clock()
        pressure = []
        if smp.cpu_pct is not None and smp.cpu_pct > 85:
            pressure.append(f"cpu {smp.cpu_pct:.0f}%")
        if smp.mem_available_mb is not None and smp.mem_available_mb < self.mem_low_mb:
            pressure.append(f"mem {smp.mem_available_mb}MB")
        if smp.container_mem_mb and smp.container_limit_mb and smp.container_mem_mb > 0.85 * smp.container_limit_mb:
            pressure.append(f"container {smp.container_mem_mb}/{smp.container_limit_mb}MB")
        if self._latency_base and smp.latency_ms and smp.latency_ms > 3 * self._latency_base and smp.latency_ms > 1500:
            pressure.append(f"latency {smp.latency_ms:.0f}ms")
        if smp.error_rate > 0.05:
            pressure.append(f"429/503 {smp.error_rate:.0%}")
        healthy = (
            not pressure
            and (smp.cpu_pct is None or smp.cpu_pct < 60)
            and (smp.mem_available_mb is None or smp.mem_available_mb > self.mem_high_mb)
            and smp.error_rate < 0.01
        )
        reason = "steady"
        if pressure:
            self._healthy = 0
            if now - self._last_change >= self.cooldown_s:
                new = max(self.s.http_concurrency_min, int(self.http_limit * 0.7))
                if new != self.http_limit:
                    self.http_limit, self._last_change = new, now
                reason = "decrease: " + ", ".join(pressure)
            else:
                reason = "pressure (cooldown): " + ", ".join(pressure)
        elif healthy:
            self._healthy += 1
            if self._healthy >= self.healthy_needed and now - self._last_change >= self.cooldown_s:
                if self.http_limit < self.s.http_concurrency:
                    self.http_limit += 1
                    self._last_change = now
                    reason = "increase"
                self._healthy = 0
        else:
            self._healthy = 0  # in the hysteresis band: hold
        # browser gate with its own hysteresis
        mem = smp.mem_available_mb
        cont_pressure = bool(
            smp.container_mem_mb and smp.container_limit_mb and smp.container_mem_mb > 0.85 * smp.container_limit_mb
        )
        if self.browser_allowed and ((mem is not None and mem < self.s.browser_mem_pause_mb) or cont_pressure):
            self.browser_allowed = False
        elif not self.browser_allowed and (mem is None or mem > self.s.browser_mem_resume_mb) and not cont_pressure:
            self.browser_allowed = True
        self.last_decision = Decision(self.http_limit, self.browser_allowed, reason)
        return self.last_decision

    def snapshot(self) -> dict:
        d = asdict(self.last) if self.last else {}
        d.update(http_limit=self.http_limit, browser_allowed=self.browser_allowed, reason=self.last_decision.reason)
        return d
