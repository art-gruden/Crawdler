"""Transactional outbox: job.created / updated / closed / reopened and the /changes feed."""

from __future__ import annotations

import datetime as dt
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from career_crawler import events, store
from career_crawler.config import CrawlerSettings
from career_crawler.engine import ScanResult
from career_crawler.models import Job
from gcc_core import botauth
from gcc_core.db import SessionLocal

from .dach_helpers import BOT_TOKEN, company, found, ingest, raw

B = "/api/bot/v1"
AUTH = {"Authorization": f"Bearer {BOT_TOKEN}"}
S = CrawlerSettings(close_after_misses=2, close_min_days=0)
DESC = "Python Entwickler gesucht. Wir bieten spannende Aufgaben rund um Datenplattformen und Cloud. " * 4


def log(db) -> list[tuple[str, int, dict]]:
    db.expire_all()
    return [(r[0], r[1], r[2]) for r in db.execute(text("SELECT event_type, job_id, payload FROM job_events ORDER BY id"))]


def types(db) -> list[str]:
    return [t for t, _, _ in log(db)]


def posting(**kw):
    base = dict(location_raw="Dresden, Sachsen", description_text=DESC)
    base.update(kw)
    return raw("Python Entwickler (m/w/d)", "ev.de", 1, **base)


def scan(db, c, raws, *, complete=True, stop=None) -> int:
    """One crawl of the company: upsert + lifecycle exactly as the runner does it."""
    res = ScanResult(jobs=[found(r, country=c.country_code) for r in raws], complete=complete, stop=stop)
    seen, _n, _u = store.upsert_jobs(db, c.id, res.jobs, {})
    closed = store.apply_lifecycle(db, c.id, res, seen, {}, S)
    db.commit()
    return closed


@pytest.fixture
def co(db):
    return company(db, "ev.de", "Ev GmbH", "DE")


def age(db, c, days=10):
    db.execute(text("UPDATE jobs SET last_seen_at = now() - make_interval(days => :d) WHERE company_id = :c"), {"d": days, "c": c.id})
    db.commit()


def test_one_created_event_and_none_on_unchanged_rescan(db, co):
    scan(db, co, [posting()])
    assert types(db) == ["job.created"]
    for _ in range(3):
        scan(db, co, [posting()])
    assert types(db) == ["job.created"]  # refreshing last_seen/last_checked is not an event


def test_duplicate_postings_in_one_scan_create_one_vacancy(db, co):
    scan(db, co, [posting(), posting()])
    assert types(db) == ["job.created"]


def test_meaningful_change_emits_updated_with_fields(db, co):
    scan(db, co, [posting()])
    scan(db, co, [posting(salary={"min": 50000, "max": 60000, "currency": "EUR", "period": "year", "raw": "50-60k"})])
    ev = log(db)
    assert [t for t, _, _ in ev] == ["job.created", "job.updated"]
    assert "salary" in " ".join(ev[1][2]["fields"])
    scan(db, co, [posting(location_raw="Berlin", salary={"min": 50000, "max": 60000, "currency": "EUR", "period": "year", "raw": "50-60k"})])
    assert types(db)[-1] == "job.updated" and any("location" in f for f in log(db)[-1][2]["fields"])


def test_metadata_only_change_is_not_an_update(db, co):
    scan(db, co, [posting()])
    # same content, different crawl metadata (extraction method, confidence, page order)
    scan(db, co, [posting(confidence=0.99, method="jsonld")])
    assert types(db) == ["job.created"]


def test_trivial_description_edit_is_not_an_update_but_big_one_is(db, co):
    scan(db, co, [posting()])
    scan(db, co, [posting(description_text=DESC + " ")])
    assert types(db) == ["job.created"]
    scan(db, co, [posting(description_text="Komplett neue Aufgabenbeschreibung mit Go, Rust und Kubernetes im Team. " * 5)])
    assert types(db) == ["job.created", "job.updated"]


def test_failed_or_partial_scan_never_closes(db, co):
    from career_crawler.fetch import CrawlStop

    scan(db, co, [posting()])
    age(db, co)
    for _ in range(4):
        scan(db, co, [], complete=False)  # partial read: not evidence of absence
        scan(db, co, [], complete=True, stop=CrawlStop("HTTP 503", "https://ev.de/jobs", status=503))  # site failed
    assert db.query(Job).one().status == "open" and db.query(Job).one().miss_count == 0
    assert types(db) == ["job.created"]


def test_confirmed_closure_emits_one_closed_event(db, co):
    scan(db, co, [posting()])
    age(db, co)
    assert scan(db, co, [], complete=True) == 0  # first miss
    assert scan(db, co, [], complete=True) == 1  # second consecutive miss -> closed
    assert types(db) == ["job.created", "job.closed"]
    assert log(db)[-1][2]["reason"] == "missed"
    scan(db, co, [], complete=True)
    assert types(db).count("job.closed") == 1  # closed jobs are not closed again


def test_job_seen_again_resets_misses(db, co):
    scan(db, co, [posting()])
    age(db, co)
    scan(db, co, [], complete=True)
    scan(db, co, [posting()])
    scan(db, co, [], complete=True)
    assert db.query(Job).one().status == "open" and types(db) == ["job.created"]


def test_reopen_after_closure_and_not_before(db, co):
    scan(db, co, [posting()])
    age(db, co)
    scan(db, co, [], complete=True)
    scan(db, co, [], complete=True)
    assert types(db)[-1] == "job.closed"
    scan(db, co, [posting()])
    assert types(db) == ["job.created", "job.closed", "job.reopened"]
    assert db.query(Job).one().status == "open"


def test_expired_posting_is_closed_once_and_not_reopened_while_expired(db, co):
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(days=2)).strftime("%Y-%m-%d")
    scan(db, co, [posting(valid_through_raw=past)])
    assert types(db)[:1] == ["job.created"]
    scan(db, co, [posting(valid_through_raw=past)])
    scan(db, co, [posting(valid_through_raw=past)])
    assert types(db).count("job.closed") <= 1 and "job.reopened" not in types(db)


def test_rollback_discards_events(db, co):
    ingest(db, co, [posting()])
    before = len(log(db))
    s = SessionLocal()
    try:
        store.upsert_jobs(s, co.id, [found(raw("Anderes Projekt", "ev.de", 9, location_raw="Wien", description_text=DESC), country="DE")], {})
        assert events.pending(s)
        s.rollback()
        assert not events.pending(s)
    finally:
        s.close()
    assert len(log(db)) == before


def test_events_are_in_the_same_transaction_as_the_state(db, co):
    s = SessionLocal()
    try:
        store.upsert_jobs(s, co.id, [found(posting(), country="DE")], {})
        s.flush()
        assert log(db) == []  # not visible to others before commit
        s.commit()
    finally:
        s.close()
    assert types(db) == ["job.created"]


def test_concurrent_writers_keep_commit_order(db):
    """Ids must follow commit order, otherwise a reader's cursor could skip an event."""
    cs = [company(db, f"w{i}.de", f"W{i} GmbH", "DE") for i in range(4)]
    errors: list[Exception] = []

    def work(c, i):
        try:
            for k in range(5):
                s = SessionLocal()
                store.upsert_jobs(s, c.id, [found(raw(f"Entwickler {i}-{k}", f"w{i}.de", k, location_raw="Berlin", description_text=DESC), country="DE")], {})
                s.commit()
                s.close()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(c, i)) for i, c in enumerate(cs)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    rows = db.execute(text("SELECT id, occurred_at, xmin::text::bigint FROM job_events ORDER BY id")).all()
    assert len(rows) == 20 and [r[0] for r in rows] == sorted(r[0] for r in rows)
    assert len({r[0] for r in rows}) == 20
    # commit order == id order: transaction ids of consecutive events never go backwards between different txns
    xids = [r[2] for r in rows]
    firsts = [xids[0]] + [x for p, x in zip(xids, xids[1:]) if x != p]
    assert len(set(firsts)) == len(firsts)


# --- /changes feed ------------------------------------------------------------------------------------------------


@pytest.fixture
def api(settings, monkeypatch):
    monkeypatch.setattr(settings, "bot_api_keys", f"telegram:{BOT_TOKEN}")
    monkeypatch.setattr(settings, "bot_rate_limit_per_min", 1000)
    botauth.limiter._mem.clear()
    botauth._load.cache_clear()
    from gcc_core.app import create_app

    yield TestClient(create_app())
    botauth._load.cache_clear()


def changes(api, **params):
    r = api.get(B + "/changes", params=params, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def test_changes_feed_pages_resume_and_replay(db, co, api):
    scan(db, co, [posting(), raw("Zweiter Job", "ev.de", 2, location_raw="Wien", description_text=DESC)])
    scan(db, co, [posting(salary={"min": 1, "max": 2, "currency": "EUR", "period": "year", "raw": "x"}), raw("Zweiter Job", "ev.de", 2, location_raw="Wien", description_text=DESC)])
    first = changes(api, limit=2)
    assert [i["event_type"] for i in first["items"]] == ["job.created", "job.created"] and first["has_more"]
    assert set(first["items"][0]) >= {"event_id", "event_type", "job_id", "occurred_at"}
    second = changes(api, cursor=first["next_cursor"], limit=2)
    assert [i["event_type"] for i in second["items"]] == ["job.updated"] and not second["has_more"]
    ids = [i["event_id"] for i in first["items"] + second["items"]]
    assert ids == sorted(ids) and len(set(ids)) == 3
    # replay with the stored cursor gives the same page (idempotent)
    assert changes(api, cursor=first["next_cursor"], limit=2)["items"] == second["items"]
    # an empty poll echoes the cursor back so the consumer can keep it
    idle = changes(api, cursor=second["next_cursor"])
    assert idle["items"] == [] and idle["next_cursor"] == second["next_cursor"] and not idle["has_more"]
    # new work shows up after the stored cursor
    scan(db, co, [posting(), raw("Zweiter Job", "ev.de", 2, location_raw="Wien", description_text=DESC), raw("Dritter Job", "ev.de", 3, location_raw="Graz", description_text=DESC)])
    more = changes(api, cursor=idle["next_cursor"])
    assert [i["event_type"] for i in more["items"]] == ["job.created"]


def test_changes_head_cursor_skips_history(db, co, api):
    scan(db, co, [posting()])
    head = api.get(B + "/changes/head", headers=AUTH).json()["cursor"]
    assert changes(api, cursor=head)["items"] == []
    scan(db, co, [posting(), raw("Neu", "ev.de", 2, location_raw="Wien", description_text=DESC)])
    assert [i["event_type"] for i in changes(api, cursor=head)["items"]] == ["job.created"]


def test_changes_cursor_validation(db, co, api):
    scan(db, co, [posting()])
    assert api.get(B + "/changes", params={"cursor": "garbage"}, headers=AUTH).status_code == 400
    from career_crawler.search import encode_cursor

    ahead = encode_cursor({"v": 1, "e": 10_000})
    assert api.get(B + "/changes", params={"cursor": ahead}, headers=AUTH).status_code == 409
    assert api.get(B + "/changes", params={"limit": 0}, headers=AUTH).status_code == 422


def test_restart_loses_nothing(db, co, api):
    """Events are in PostgreSQL: a consumer that was down for a while simply continues from its cursor."""
    scan(db, co, [posting()])
    cur = changes(api)["next_cursor"]
    for i in range(2, 6):
        scan(db, co, [posting(), raw(f"Job {i}", "ev.de", i, location_raw="Wien", description_text=DESC)])
    got = []
    while True:
        page = changes(api, cursor=cur, limit=2)
        got += page["items"]
        cur = page["next_cursor"]
        if not page["has_more"]:
            break
    assert [i["event_type"] for i in got].count("job.created") == 4
