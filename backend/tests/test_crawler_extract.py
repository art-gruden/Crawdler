"""Extraction and normalization units (no database, no network)."""

from __future__ import annotations

import datetime as dt

import pytest

from career_crawler import ats
from career_crawler.discovery import Frontier, parse_sitemap
from career_crawler.extract import html as h
from career_crawler.extract.jsonld import extract_jsonld
from career_crawler.extract.normalize import (
    RawJob,
    employment_types,
    normalize,
    normalize_title,
    parse_date,
    parse_location,
    remote_type,
    salary_from_text,
    split_sections,
)
from career_crawler.extract.skills import find_languages, find_skills
from career_crawler.urls import canonicalize, link_score


def test_canonicalize_strips_tracking_and_sessions():
    assert (
        canonicalize("HTTPS://Example.DE:443/Karriere/?utm_source=x&b=2&a=1#top")
        == "https://example.de/Karriere/?a=1&b=2"
    )
    assert canonicalize("/jobs;jsessionid=ABC?gclid=1", "https://x.de/a/") == "https://x.de/jobs"
    assert canonicalize("mailto:jobs@x.de") is None
    assert canonicalize("javascript:void(0)") is None


@pytest.mark.parametrize(
    "url,anchor,positive",
    [
        ("https://x.de/karriere", "Karriere", True),
        ("https://x.de/jobs/devops-engineer", "DevOps Engineer (m/w/d)", True),
        ("https://x.de/ueber-uns/arbeiten-bei-uns", "Arbeiten bei uns", True),
        ("https://x.de/careers", "Join our team", True),
        ("https://x.de/datenschutz", "Datenschutz", False),
        ("https://x.de/shop/warenkorb", "Warenkorb", False),
        ("https://x.de/news/2024/archiv", "Archiv", False),
        ("https://x.de/katalog.zip", "Katalog", False),
        ("https://x.de/events/kalender", "Kalender", False),
    ],
)
def test_link_score(url, anchor, positive):
    assert (link_score(url, anchor) > 0) is positive


def test_dates_come_from_source_only():
    assert parse_date("2026-10-01T08:00:00Z") == dt.date(2026, 10, 1)
    assert parse_date("01.10.2026") == dt.date(2026, 10, 1)
    assert parse_date("1. Oktober 2026") == dt.date(2026, 10, 1)
    assert parse_date("1970-01-01") is None  # placeholder
    assert parse_date((dt.date.today() + dt.timedelta(days=30)).isoformat()) is None  # future "posted" date
    assert parse_date(None) is None
    job = normalize(RawJob(title="Admin (m/w/d)", source_url="https://x.de/j", method="html"))
    assert job.date_posted is None  # never replaced by the discovery date


def test_location_remote_employment_salary():
    assert parse_location("01067 Dresden, Deutschland") == {
        "city": "Dresden",
        "postcode": "01067",
        "region": None,
        "country": "DE",
    }
    assert parse_location("Leipzig / Remote")["city"] == "Leipzig"
    assert remote_type("100% remote") == "remote"
    assert remote_type("Hybrid, 2 Tage Home-Office") == "hybrid"
    assert remote_type("", schema_location_type="TELECOMMUTE") == "remote"
    assert remote_type("Wir sind ein Team") == "unknown"
    assert employment_types(["FULL_TIME"], "Werkstudent IT (m/w/d)") == ["full_time", "working_student"]
    assert employment_types(["Vollzeit, unbefristet"]) == ["full_time", "permanent"]
    s = salary_from_text("Gehalt: 55.000 – 70.000 € brutto pro Jahr")
    assert (s["min"], s["max"], s["period"]) == (55000, 70000, "year")
    assert salary_from_text("Wir haben 120 Mitarbeiter und 3 Standorte") is None


def test_skills_and_languages():
    skills = [s for s, _ in find_skills("Kubernetes, Terraform und Go; CI/CD mit GitLab; AWS. Let's go home")]
    assert {"Kubernetes", "Terraform", "Go", "CI/CD", "AWS"} <= set(skills)
    assert "Go" not in [s for s, _ in find_skills("Wir go-live im Mai und gehen gemeinsam los")]
    langs = {x["language"]: x for x in find_languages("Sehr gute Deutschkenntnisse (C1) und gute Englischkenntnisse.")}
    # CEFR only when written: "gute Englischkenntnisse" is a descriptor, never a fabricated B1
    assert langs["de"]["level"] == "C1" and langs["en"]["level"] is None and langs["en"]["descriptor"] == "good"
    assert find_languages("Wir sind ein deutsches Unternehmen") == []
    opt = find_languages("Fluent English; German is a plus")
    assert {x["language"]: x["required"] for x in opt} == {"en": True, "de": False}


def test_title_and_sections():
    assert normalize_title("Senior DevOps Engineer (m/w/d) – Kubernetes") == "senior devops engineer kubernetes"
    resp, req = split_sections(
        "Deine Aufgaben:\n- Betrieb\n- Automatisierung\nDein Profil\n- Linux\nWir bieten\n- Obst"
    )
    assert resp == ["Betrieb", "Automatisierung"] and req == ["Linux"]


def test_jsonld_graph_broken_and_microdata():
    html_ = """<script type="application/ld+json">{"@graph":[{"@type":"Organization","name":"X"},
      {"@type":["JobPosting"],"title":"SRE (m/w/d)","datePosted":"2026-10-01","identifier":{"value":"77"},
      "jobLocation":[{"address":{"addressLocality":"Dresden","postalCode":"01069","addressCountry":"DE"}}],
      "baseSalary":{"currency":"EUR","value":{"minValue":"60000","maxValue":70000,"unitText":"YEAR"}}}]}</script>
      <script type="application/ld+json">{"@type":"JobPosting","title":"Broken (m/w/d)","description":"a
      b",}</script>"""
    jobs, has = extract_jsonld(h.parse(html_), "https://x.de/jobs")
    assert has and [j.title for j in jobs] == ["SRE (m/w/d)", "Broken (m/w/d)"]
    n = normalize(jobs[0])
    assert (n.city, n.postcode, n.salary_min, n.salary_period, n.ats_job_id) == (
        "Dresden",
        "01069",
        60000,
        "year",
        "77",
    )
    micro = (
        '<div itemscope itemtype="https://schema.org/JobPosting"><h1 itemprop="title">Elektroniker (m/w/d)</h1></div>'
    )
    assert [j.title for j in extract_jsonld(h.parse(micro), "https://x.de/")[0]] == ["Elektroniker (m/w/d)"]


def test_ats_detection_requires_evidence():
    found = ats.detect(
        [
            "https://acme.jobs.personio.de/job/123",
            "https://boards.greenhouse.io/embed/job_board/js?for=acmegh",
            "https://jobs.eu.lever.co/acmelv/abc",
            "https://www.personio.de/",
            "https://jobs.lever.co/",
        ],
        inline_scripts="var u='https://job-boards.greenhouse.io/othergh';",
    )
    got = {(m.ats_type, m.account) for m in found}
    assert got == {("personio", "acme"), ("greenhouse", "acmegh"), ("lever", "acmelv"), ("greenhouse", "othergh")}
    lv = next(m for m in found if m.ats_type == "lever")
    assert lv.feed_url.startswith("https://api.eu.lever.co/v0/postings/acmelv")
    # no evidence -> nothing, even if the company is called "Acme"
    assert ats.detect(["https://acme.de/karriere"], "Acme GmbH Karriere") == []
    assert ats.detect_url("https://www.jobs.personio.de/") is None


def test_classification_and_job_links():
    page = """<h1>Karriere</h1><ul><li><a href="/jobs/a">DevOps Engineer (m/w/d)</a></li>
    <li><a href="/jobs/b">Buchhalter (w/m/d)</a></li></ul><a href="/datenschutz">Datenschutz</a>"""
    t = h.parse(page)
    sig = h.classify(t, "https://x.de/karriere", h.links(t, "https://x.de/karriere"), has_jobposting=False)
    assert sig.page_type == "job_list" and len(sig.job_links) == 2
    t2 = h.parse("<h1>Karriere</h1><p>Derzeit haben wir leider keine offenen Stellen.</p>")
    assert h.classify(t2, "https://x.de/karriere", [], has_jobposting=False).page_type == "no_vacancies"
    t3 = h.parse('<div id="app"></div><script src="/a.js"></script><noscript>Bitte JavaScript aktivieren</noscript>')
    assert h.classify(t3, "https://x.de/karriere", [], has_jobposting=False).page_type == "js_shell"


def test_frontier_is_bounded_and_deduplicated():
    f = Frontier(3)
    for i, p in enumerate([1, 5, 3, 9, 2]):
        f.push(f"https://x.de/{i}", p, 1)
    assert len(f) == 3
    assert not f.push("https://x.de/3", 100, 1)  # already seen
    assert [f.pop().priority for _ in range(3)] == [9, 5, 3]


def test_sitemap_index_and_xxe_safe():
    idx = b"<?xml version='1.0'?><sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'><sitemap><loc>https://x.de/a.xml</loc></sitemap></sitemapindex>"
    assert parse_sitemap(idx, max_urls=10) == (["https://x.de/a.xml"], [])
    xxe = b"""<?xml version="1.0"?><!DOCTYPE r [<!ENTITY e SYSTEM "file:///etc/passwd">]>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>&e;</loc></url></urlset>"""
    _, urls = parse_sitemap(xxe, max_urls=10)
    assert all("root:" not in u for u in urls)
