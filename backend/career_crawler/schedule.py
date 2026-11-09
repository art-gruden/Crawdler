"""Adaptive recrawl intervals."""

from __future__ import annotations

import datetime as dt
import random

from .config import CrawlerSettings

ERROR_BACKOFF_H = {
    "unreachable": 12.0,
    "robots_unavailable": 24.0,
    "fetch_error": 12.0,
    "time_budget": 24.0,
}


def next_interval_h(
    s: CrawlerSettings,
    *,
    outcome: str,
    new_jobs: int,
    open_jobs: int,
    changed: bool,
    last_new_job_at: dt.datetime | None,
    last_change_at: dt.datetime | None,
    error_streak: int,
    retry_after_s: float | None,
    now: dt.datetime,
    rng: random.Random | None = None,
) -> float:
    rng = rng or random
    if outcome == "rate_limited":
        hours = max((retry_after_s or 0) / 3600, 6.0) * (2 ** min(error_streak, 4))
    elif outcome in ("access_restricted", "robots_disallowed"):
        hours = s.interval_max_h  # never worked around; look again rarely
    elif outcome in ERROR_BACKOFF_H or outcome.startswith("site_unreachable"):
        hours = ERROR_BACKOFF_H.get(outcome, 12.0) * (2 ** min(error_streak, 5))
    elif outcome == "no_career_page":
        hours = s.interval_no_career_h
    elif outcome in ("no_vacancies", "career_page_no_jobs"):
        hours = s.interval_no_jobs_h
    elif outcome == "needs_browser":
        hours = s.interval_normal_h
    else:
        recent_new = new_jobs > 0 or (last_new_job_at is not None and now - last_new_job_at < dt.timedelta(days=7))
        if recent_new:
            hours = s.interval_active_h
        else:
            hours = s.interval_normal_h
            # a site that has not changed for weeks is checked less often (but at least weekly)
            if not changed and last_change_at is not None and now - last_change_at > dt.timedelta(days=21):
                hours = min(hours * 2, s.interval_no_jobs_h)
        if open_jobs == 0:
            hours = max(hours, s.interval_no_jobs_h)
    hours = min(max(hours, 1.0), s.interval_max_h)
    return round(hours * rng.uniform(0.9, 1.1), 2)  # jitter spreads the load
