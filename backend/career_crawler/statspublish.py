"""Phase 4: publish an approved statistics post to a Telegram channel. Disabled unless explicitly enabled.

Safety properties (each covered by a test):
* needs ``CRAWLER_STATS_TELEGRAM_ENABLED=true``, a bot token and a chat id, all from the environment only;
  the token never appears in a response, a stored row or a log line;
* the caller must send the sha256 of the text they previewed – a changed text is rejected, so what was
  approved is exactly what is posted;
* one publication per channel + period + configuration (unique ``dedup_key``): repeating a request does not
  post twice; a failed attempt may be retried;
* a minimum gap between attempts and Telegram's ``retry_after`` are respected (no automatic retries);
* an empty report is only posted when ``CRAWLER_STATS_TELEGRAM_POST_EMPTY=true``.
There is no scheduler: nothing is ever posted without an explicit, confirmed API call.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import CrawlerSettings
from .models import StatPublication

log = logging.getLogger("career_crawler.statspublish")
API = "https://api.telegram.org"
IN_FLIGHT_S = 60


class PublishError(Exception):
    def __init__(self, status_code: int, message: str, **extra: Any):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra


def make_client(settings: CrawlerSettings) -> httpx.Client:
    """Overridden in tests with a mock transport."""
    return httpx.Client(timeout=settings.stats_telegram_timeout_s, follow_redirects=False)


def status(settings: CrawlerSettings) -> dict[str, Any]:
    """What the UI may know: flags only, never the token or the chat id."""
    return {
        "enabled": settings.stats_telegram_enabled,
        "configured": bool(settings.stats_telegram_bot_token and settings.stats_telegram_chat_id),
        "post_empty": settings.stats_telegram_post_empty,
        "bot_link": settings.stats_telegram_bot_link,
    }


def scrub(s: str, settings: CrawlerSettings) -> str:
    tok = settings.stats_telegram_bot_token.get_secret_value() if settings.stats_telegram_bot_token else ""
    return s.replace(tok, "***") if tok else s


def dedup_key(channel: str, period_start: dt.datetime, period_end: dt.datetime, config: dict[str, Any]) -> str:
    raw = json.dumps(
        {"c": channel, "s": period_start.isoformat(), "e": period_end.isoformat(), "cfg": config}, sort_keys=True
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _row_out(r: StatPublication) -> dict[str, Any]:
    return {
        "id": r.id,
        "status": r.status,
        "channel": r.channel,
        "period_start": r.period_start.isoformat(),
        "period_end": r.period_end.isoformat(),
        "attempts": r.attempts,
        "message_id": r.message_id,
        "error": r.error,
        "text": r.body,
        "text_sha256": r.text_sha256,
        "config": r.config,
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "sent_at": r.sent_at.isoformat() if r.sent_at else None,
    }


def history(db: Session, limit: int = 50) -> list[dict[str, Any]]:
    rows = db.execute(select(StatPublication).order_by(StatPublication.id.desc()).limit(limit)).scalars()
    return [_row_out(r) for r in rows]


def publish(
    db: Session,
    settings: CrawlerSettings,
    *,
    user: str,
    report: dict[str, Any],
    approved_sha256: str,
    filters: dict[str, Any],
    period_start: dt.datetime,
    period_end: dt.datetime,
) -> dict[str, Any]:
    if not settings.stats_telegram_enabled:
        raise PublishError(403, "Telegram publishing is disabled (CRAWLER_STATS_TELEGRAM_ENABLED=false)")
    if not (settings.stats_telegram_bot_token and settings.stats_telegram_chat_id):
        raise PublishError(409, "Telegram publishing is enabled but the bot token or chat id is not configured")
    if report["sha256"] != approved_sha256:
        raise PublishError(409, "The report changed since it was previewed; generate a new preview and approve it again")
    if report["empty"] and not settings.stats_telegram_post_empty:
        raise PublishError(409, "The report has no new vacancies; empty posts are disabled (CRAWLER_STATS_TELEGRAM_POST_EMPTY)")

    channel = settings.stats_telegram_chat_id
    now = dt.datetime.now(dt.UTC)
    last = db.execute(
        select(StatPublication.updated_at).where(StatPublication.channel == channel).order_by(StatPublication.updated_at.desc()).limit(1)
    ).scalar_one_or_none()
    if last and (now - last).total_seconds() < settings.stats_telegram_min_interval_s:
        raise PublishError(429, "Too many publish attempts; wait a few seconds", retry_after=settings.stats_telegram_min_interval_s)

    config = {**report["options"], "filters": filters}
    key = dedup_key(channel, period_start, period_end, config)
    row = db.execute(select(StatPublication).where(StatPublication.dedup_key == key)).scalar_one_or_none()
    if row is not None:
        if row.status == "sent":
            raise PublishError(409, "This report (period and configuration) was already published", publication=_row_out(row))
        if row.status == "pending" and (now - row.updated_at).total_seconds() < IN_FLIGHT_S:
            raise PublishError(409, "This report is being published right now", publication=_row_out(row))
        row.status, row.body, row.text_sha256, row.updated_at = "pending", report["text"], report["sha256"], now
    else:
        row = StatPublication(
            dedup_key=key,
            channel=channel,
            period_start=period_start,
            period_end=period_end,
            config=config,
            body=report["text"],
            text_sha256=report["sha256"],
            status="pending",
            created_by=user[:64],
        )
        db.add(row)
    try:
        row.attempts = (row.attempts or 0) + 1
        db.commit()  # the unique key makes a concurrent duplicate fail here, before anything is sent
    except IntegrityError:
        db.rollback()
        raise PublishError(409, "This report is being published right now") from None

    payload: dict[str, Any] = {"chat_id": channel, "text": report["text"], "disable_web_page_preview": True}
    if report["parse_mode"]:
        payload["parse_mode"] = report["parse_mode"]
    token = settings.stats_telegram_bot_token.get_secret_value()
    error: str | None = None
    body: dict[str, Any] | None = None
    retry_after: int | None = None
    try:
        with make_client(settings) as client:
            resp = client.post(f"{API}/bot{token}/sendMessage", json=payload)
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or not body.get("ok"):
            desc = (body or {}).get("description") if isinstance(body, dict) else None
            error = scrub(f"Telegram API error {resp.status_code}: {desc or resp.text[:200]}", settings)
            if isinstance(body, dict):
                retry_after = (body.get("parameters") or {}).get("retry_after")
    except httpx.HTTPError as exc:
        error = scrub(f"{type(exc).__name__}: {exc}", settings)

    safe_body = json.loads(scrub(json.dumps(body), settings)) if body is not None else None
    row.response = safe_body
    row.error = error
    row.updated_at = dt.datetime.now(dt.UTC)
    if error is None and isinstance(body, dict):
        row.status = "sent"
        row.sent_at = row.updated_at
        row.message_id = (body.get("result") or {}).get("message_id")
    else:
        row.status = "failed"
    db.commit()
    log.info("stats publication %s id=%s status=%s", key[:12], row.id, row.status)
    out = _row_out(row)
    if row.status == "failed":
        extra: dict[str, Any] = {"publication": out}
        if retry_after:
            extra["retry_after"] = retry_after
        raise PublishError(429 if retry_after else 502, error or "Telegram request failed", **extra)
    return out
