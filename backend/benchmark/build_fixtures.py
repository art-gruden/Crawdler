"""Generates the offline benchmark/test websites under benchmark/sites/ plus ground truth.

All companies and domains are fictional (``gcc-fixture-*.de``). ATS hosts use the real
platform host patterns with fictional accounts (``fixturecorp.jobs.personio.de``) because
detection depends on them; in test mode nothing is ever sent over the network.

Run:  python -m benchmark.build_fixtures
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).parent / "sites"
D = "fixture-gcc.de"  # every site gets its own registrable domain: gcc-fixture-<name>.de


def dom(name: str) -> str:
    return f"gcc-fixture-{name}.de"


NAV_NOISE = """
<nav><a href="/">Start</a> <a href="/produkte">Produkte</a> <a href="/shop">Shop</a> <a href="/news">News</a>
<a href="/news/2024/archiv">Archiv</a> <a href="/datenschutz">Datenschutz</a> <a href="/impressum">Impressum</a>
<a href="/kontakt">Kontakt</a> <a href="/events/kalender">Kalender</a> <a href="/downloads/katalog.zip">Katalog</a></nav>
"""


def page(title: str, body: str, head: str = "") -> str:
    return (
        f"<!doctype html><html lang=de><head><meta charset=utf-8><title>{title}</title>{head}</head>"
        f"<body><header>{NAV_NOISE}</header><main>{body}</main><footer><a href='/datenschutz'>Datenschutz</a>"
        f"</footer></body></html>"
    )


def detail(title: str, city: str, aufgaben: list[str], profil: list[str], extra: str = "") -> str:
    li = lambda xs: "".join(f"<li>{x}</li>" for x in xs)  # noqa: E731
    return page(
        title,
        f"""<article><h1>{title}</h1><p class="location">Standort: {city}</p>{extra}
<h2>Deine Aufgaben</h2><ul>{li(aufgaben)}</ul><h2>Dein Profil</h2><ul>{li(profil)}</ul>
<h2>Wir bieten</h2><ul><li>30 Tage Urlaub</li><li>Jobticket</li></ul>
<p><a href="/bewerbung">Jetzt bewerben</a></p></article>""",
    )


def w(site: str, rel: str, content: str | bytes) -> None:
    p = ROOT / site / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")


def routes(site: str, data: dict) -> None:
    w(site, "_routes.json", json.dumps(data, indent=2))


def home(site: str, career_link: str | None, extra: str = "") -> None:
    link = f'<a href="{career_link}">Karriere</a>' if career_link else ""
    w(
        site,
        "index.html",
        page(
            "Startseite",
            f"<h1>Willkommen</h1><p>Wir sind ein Unternehmen in Sachsen.</p>{link}{extra}"
            "<p>Unsere Produkte <a href='/produkte'>ansehen</a></p>",
        ),
    )
    w(
        site,
        "produkte.html",
        page("Produkte", "<h1>Produkte</h1><a href='/produkte/a'>A</a><a href='/produkte/b'>B</a>"),
    )
    w(
        site,
        "news.html",
        page("News", "<h1>News</h1>" + "".join(f"<a href='/news/{i}'>Meldung {i}</a>" for i in range(8))),
    )
    w(site, "datenschutz.html", page("Datenschutz", "<h1>Datenschutz</h1><p>Text</p>"))


def tiny_pdf(lines: list[str]) -> bytes:
    """Minimal valid single-page PDF with Helvetica text (no external dependencies)."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content = "BT /F1 11 Tf 50 800 Td 14 TL " + " ".join(f"({esc(ln)}) Tj T*" for ln in lines) + " ET"
    data = content.encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def build() -> dict:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    expected: dict[str, dict] = {}

    # 1 static HTML ----------------------------------------------------------------------------
    s = dom("static")
    home(s, "/karriere")
    w(s, "robots.txt", "User-agent: *\nDisallow: /intern/\n")
    w(
        s,
        "karriere.html",
        page(
            "Karriere",
            """<h1>Karriere bei Static GmbH</h1><h2>Offene Stellen</h2><ul>
<li><a href="/karriere/devops-engineer">DevOps Engineer (m/w/d)</a> – 01067 Dresden</li>
<li><a href="/karriere/linux-administrator">Linux Administrator (m/w/d)</a> – Leipzig</li>
<li><a href="/karriere/werkstudent-it">Werkstudent IT-Support (m/w/d)</a> – Dresden</li></ul>
<a href="/intern/login">Intranet</a>""",
        ),
    )
    w(
        s,
        "karriere/devops-engineer.html",
        detail(
            "DevOps Engineer (m/w/d)",
            "01067 Dresden",
            [
                "Betrieb unserer Kubernetes-Cluster",
                "Automatisierung mit Terraform und Ansible",
                "Monitoring mit Prometheus",
            ],
            ["Erfahrung mit Linux und Docker", "Sehr gute Deutschkenntnisse (C1)", "Gute Englischkenntnisse"],
            "<p>Vollzeit, unbefristet. Hybrid: 2 Tage Home-Office pro Woche.</p><p>Veröffentlicht am 15.10.2026</p>",
        ),
    )
    w(
        s,
        "karriere/linux-administrator.html",
        detail(
            "Linux Administrator (m/w/d)",
            "04109 Leipzig",
            ["Administration von Debian-Servern", "Pflege der Backup-Infrastruktur"],
            ["Bash, Python", "Deutsch fließend"],
            "<p>Vollzeit</p><p>Gehalt: 48.000 – 58.000 € brutto pro Jahr</p>",
        ),
    )
    w(
        s,
        "karriere/werkstudent-it.html",
        detail(
            "Werkstudent IT-Support (m/w/d)",
            "Dresden",
            ["Support für Kolleginnen und Kollegen"],
            ["Studium der Informatik"],
            "<p>Teilzeit, 20 Std./Woche</p>",
        ),
    )
    expected[s] = {
        "career_pages": [f"https://{s}/karriere"],
        "outcome": "jobs_found",
        "jobs": [
            {
                "title": "DevOps Engineer (m/w/d)",
                "city": "Dresden",
                "postcode": "01067",
                "employment_types": ["full_time", "permanent"],
                "remote_type": "hybrid",
                "date_posted": "2026-10-15",
                "skills": ["Kubernetes", "Terraform", "Ansible", "Prometheus"],
            },
            {
                "title": "Linux Administrator (m/w/d)",
                "city": "Leipzig",
                "employment_types": ["full_time"],
                "salary_min": 48000,
            },
            {
                "title": "Werkstudent IT-Support (m/w/d)",
                "city": "Dresden",
                "employment_types": ["part_time", "working_student"],
            },
        ],
    }

    # 2 JSON-LD -------------------------------------------------------------------------------
    s = dom("jsonld")
    home(s, "/jobs")
    w(
        s,
        "jobs.html",
        page(
            "Jobs",
            """<h1>Jobs</h1><a href="/jobs/sre-1001">Site Reliability Engineer (m/w/d)</a>
<a href="/jobs/backend-1002">Backend Developer Go (m/w/d)</a>""",
        ),
    )

    def ld(title, ident, city, pc, date, extra=None):
        d = {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": title,
            "datePosted": date,
            "identifier": {"@type": "PropertyValue", "name": "JSONLD GmbH", "value": ident},
            "employmentType": ["FULL_TIME"],
            "hiringOrganization": {"@type": "Organization", "name": "JSONLD GmbH"},
            "jobLocation": {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": city,
                    "postalCode": pc,
                    "addressCountry": "DE",
                },
            },
            "description": f"<p>Wir suchen {title}.</p><h3>Deine Aufgaben</h3><ul><li>Kubernetes und Go</li></ul>"
            "<h3>Dein Profil</h3><ul><li>Erfahrung mit AWS</li><li>Englisch fließend</li></ul>",
        }
        d.update(extra or {})
        return f'<script type="application/ld+json">{json.dumps(d, ensure_ascii=False)}</script>'

    w(
        s,
        "jobs/sre-1001.html",
        page(
            "SRE",
            "<h1>Site Reliability Engineer (m/w/d)</h1><p>Details</p>",
            ld(
                "Site Reliability Engineer (m/w/d)",
                "1001",
                "Dresden",
                "01069",
                "2026-10-20",
                {
                    "jobLocationType": "TELECOMMUTE",
                    "baseSalary": {
                        "@type": "MonetaryAmount",
                        "currency": "EUR",
                        "value": {
                            "@type": "QuantitativeValue",
                            "minValue": 60000,
                            "maxValue": 75000,
                            "unitText": "YEAR",
                        },
                    },
                },
            ),
        ),
    )
    w(
        s,
        "jobs/backend-1002.html",
        page(
            "Backend",
            "<h1>Backend Developer Go (m/w/d)</h1>",
            ld("Backend Developer Go (m/w/d)", "1002", "Chemnitz", "09111", "2026-09-30"),
        ),
    )
    expected[s] = {
        "career_pages": [f"https://{s}/jobs"],
        "outcome": "jobs_found",
        "jobs": [
            {
                "title": "Site Reliability Engineer (m/w/d)",
                "city": "Dresden",
                "postcode": "01069",
                "remote_type": "remote",
                "date_posted": "2026-10-20",
                "salary_min": 60000,
                "salary_max": 75000,
                "employment_types": ["full_time"],
            },
            {
                "title": "Backend Developer Go (m/w/d)",
                "city": "Chemnitz",
                "date_posted": "2026-09-30",
                "employment_types": ["full_time"],
            },
        ],
    }

    # 3 Personio --------------------------------------------------------------------------------
    s = dom("personio")
    home(s, "https://fixturecorp.jobs.personio.de/")
    p = "fixturecorp.jobs.personio.de"
    positions = [
        ("2001", "Cloud Engineer (m/w/d)", "Dresden", "full-time", "2026-10-01T08:00:00+00:00"),
        ("2002", "IT-Systemadministrator (m/w/d)", "Leipzig", "full-time", "2026-08-15T08:00:00+00:00"),
        ("2003", "Werkstudent DevOps (m/w/d)", "Dresden", "part-time", "2026-10-25T08:00:00+00:00"),
    ]
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>\n<workzag-jobs>"
        + "".join(
            f"<position><id>{i}</id><subcompany>Fixture Corp</subcompany><office>{o}</office><department>IT</department>"
            f"<name>{t}</name><jobDescriptions><jobDescription><name>Deine Aufgaben</name><value><![CDATA[<ul><li>Terraform"
            f" und Kubernetes</li></ul>]]></value></jobDescription><jobDescription><name>Dein Profil</name><value><![CDATA["
            f"<ul><li>Linux</li><li>Deutsch C1</li></ul>]]></value></jobDescription></jobDescriptions>"
            f"<employmentType>permanent</employmentType><schedule>{sch}</schedule><createdAt>{c}</createdAt></position>"
            for i, t, o, sch, c in positions
        )
        + "</workzag-jobs>"
    )
    w(p, "xml", xml)
    routes(p, {"/xml": {"file": "xml", "content_type": "application/xml"}})
    w(p, "index.html", page("Jobs", "<h1>Fixture Corp Jobs</h1>"))
    expected[s] = {
        "career_pages": ["https://fixturecorp.jobs.personio.de/xml"],
        "outcome": "jobs_found",
        "ats": "personio",
        "jobs": [{"title": t, "city": o, "date_posted": c[:10], "ats_job_id": i} for i, t, o, _s, c in positions],
    }

    # 4 Greenhouse (embed script) -------------------------------------------------------------------
    s = dom("greenhouse")
    home(s, "/karriere")
    w(
        s,
        "karriere.html",
        page(
            "Karriere",
            """<h1>Karriere</h1><div id="grnhse_app"></div>
<script src="https://boards.greenhouse.io/embed/job_board/js?for=fixturegh"></script>""",
        ),
    )
    gh = {
        "jobs": [
            {
                "id": 3001,
                "title": "Platform Engineer",
                "updated_at": "2026-11-01T10:00:00Z",
                "first_published": "2026-10-10T10:00:00Z",
                "location": {"name": "Leipzig, Germany"},
                "absolute_url": "https://job-boards.greenhouse.io/fixturegh/jobs/3001",
                "content": "&lt;p&gt;Kubernetes, Helm and GCP.&lt;/p&gt;&lt;h3&gt;Requirements&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;Fluent English&lt;/li&gt;&lt;/ul&gt;",
                "metadata": [{"name": "Employment Type", "value": "Full-time"}],
            },
            {
                "id": 3002,
                "title": "Security Engineer (remote)",
                "updated_at": "2026-11-02T10:00:00Z",
                "location": {"name": "Remote - Germany"},
                "absolute_url": "https://job-boards.greenhouse.io/fixturegh/jobs/3002",
                "content": "&lt;p&gt;100% remote. Vault, AWS.&lt;/p&gt;",
                "metadata": [],
            },
        ],
        "meta": {"total": 2},
    }
    w("boards-api.greenhouse.io", "jobs.json", json.dumps(gh))
    routes(
        "boards-api.greenhouse.io",
        {"/v1/boards/fixturegh/jobs?content=true": {"file": "jobs.json", "content_type": "application/json"}},
    )
    w("boards.greenhouse.io", "index.html", "<html></html>")
    expected[s] = {
        "career_pages": [f"https://{s}/karriere"],
        "outcome": "jobs_found",
        "ats": "greenhouse",
        "jobs": [
            {
                "title": "Platform Engineer",
                "city": "Leipzig",
                "date_posted": "2026-10-10",
                "employment_types": ["full_time"],
            },
            {"title": "Security Engineer (remote)", "remote_type": "remote", "date_posted": None},
        ],
    }

    # 5 Lever (EU) ------------------------------------------------------------------------------------
    s = dom("lever")
    home(s, "https://jobs.eu.lever.co/fixturelever")
    lv = [
        {
            "id": "a1b2c3",
            "text": "Data Engineer",
            "hostedUrl": "https://jobs.eu.lever.co/fixturelever/a1b2c3",
            "applyUrl": "https://jobs.eu.lever.co/fixturelever/a1b2c3/apply",
            "createdAt": 1790000000000,
            "categories": {"location": "Dresden", "commitment": "Full-time", "team": "Data"},
            "workplaceType": "hybrid",
            "description": "<p>Python, Spark and PostgreSQL.</p>",
            "lists": [{"text": "Requirements", "content": "<li>SQL</li>"}],
            "salaryRange": {"min": 55000, "max": 70000, "currency": "EUR", "interval": "per-year-salary"},
        }
    ]
    w("api.eu.lever.co", "postings.json", json.dumps(lv))
    routes(
        "api.eu.lever.co",
        {
            "/v0/postings/fixturelever?mode=json&skip=0&limit=100": {
                "file": "postings.json",
                "content_type": "application/json",
            }
        },
    )
    w("jobs.eu.lever.co", "index.html", "<html></html>")
    expected[s] = {
        "career_pages": ["https://api.eu.lever.co/v0/postings/fixturelever?mode=json"],
        "outcome": "jobs_found",
        "ats": "lever",
        "jobs": [
            {
                "title": "Data Engineer",
                "city": "Dresden",
                "remote_type": "hybrid",
                "date_posted": "2026-09-21",
                "salary_min": 55000,
                "employment_types": ["full_time"],
            }
        ],
    }

    # 6 external ATS redirect -----------------------------------------------------------------------------
    s = dom("redirect")
    home(s, "/karriere")
    routes(s, {"/karriere": {"status": 301, "location": "https://fixturecorp2.jobs.personio.de/"}})
    p2 = "fixturecorp2.jobs.personio.de"
    w(p2, "index.html", page("Jobs", "<h1>Jobs</h1>"))
    w(
        p2,
        "xml",
        "<?xml version='1.0'?><workzag-jobs><position><id>4001</id><office>Chemnitz</office>"
        "<name>Mechatroniker (m/w/d)</name><schedule>full-time</schedule><createdAt>2026-10-05T00:00:00+00:00"
        "</createdAt></position></workzag-jobs>",
    )
    routes(p2, {"/xml": {"file": "xml", "content_type": "application/xml"}})
    expected[s] = {
        "career_pages": ["https://fixturecorp2.jobs.personio.de/xml"],
        "outcome": "jobs_found",
        "ats": "personio",
        "jobs": [{"title": "Mechatroniker (m/w/d)", "city": "Chemnitz", "date_posted": "2026-10-05"}],
    }

    # 7 JavaScript-rendered --------------------------------------------------------------------------------
    s = dom("jsapp")
    home(s, "/karriere")
    w(
        s,
        "karriere.html",
        """<!doctype html><html><head><title>Karriere</title></head><body><div id="app"></div>
<noscript>Bitte JavaScript aktivieren.</noscript><script src="/static/vendor.js"></script><script src="/static/app.js"></script>
<script src="/static/chunk.js"></script></body></html>""",
    )
    w(s, "static/vendor.js", "/* vendor */")
    w(s, "static/chunk.js", "/* chunk */")
    w(
        s,
        "static/app.js",
        """fetch('/api/jobs.json').then(r => r.json()).then(jobs => {
  const app = document.getElementById('app');
  app.innerHTML = '<h1>Karriere</h1><h2>Offene Stellen</h2><ul>' + jobs.map(j =>
    `<li><a href="/karriere/${j.slug}">${j.title}</a> – ${j.city}</li>`).join('') + '</ul>';
});""",
    )
    w(
        s,
        "api/jobs.json",
        json.dumps(
            [
                {"slug": "embedded-dev", "title": "Embedded Software Entwickler (m/w/d)", "city": "Dresden"},
                {"slug": "fpga", "title": "FPGA Ingenieur (m/w/d)", "city": "Dresden"},
            ]
        ),
    )
    expected[s] = {
        "career_pages": [f"https://{s}/karriere"],
        "outcome": "needs_browser",
        "needs_browser": True,
        "jobs": [
            {"title": "Embedded Software Entwickler (m/w/d)", "city": "Dresden"},
            {"title": "FPGA Ingenieur (m/w/d)", "city": "Dresden"},
        ],
    }

    # 8 pagination ---------------------------------------------------------------------------------------------
    s = dom("paginated")
    home(s, "/stellenangebote")
    titles = [
        ("Elektroniker (m/w/d)", "Freiberg"),
        ("Softwareentwickler C++ (m/w/d)", "Chemnitz"),
        ("Projektmanager (m/w/d)", "Zwickau"),
        ("Fachinformatiker Systemintegration (m/w/d)", "Plauen"),
        ("Qualitätsingenieur (m/w/d)", "Chemnitz"),
        ("Vertriebsingenieur (m/w/d)", "Dresden"),
    ]
    for pg in (1, 2, 3):
        items = titles[(pg - 1) * 2 : pg * 2]
        nxt = f'<a href="/stellenangebote?page={pg + 1}" rel="next">Weiter</a>' if pg < 3 else ""
        body = (
            "<h1>Stellenangebote</h1><ul>"
            + "".join(
                f'<li><a href="/stellenangebote/{i + (pg - 1) * 2}">{t}</a> {c}</li>' for i, (t, c) in enumerate(items)
            )
            + f"</ul>{nxt}"
        )
        if pg == 1:
            w(s, "stellenangebote.html", page("Stellenangebote", body))
        w(s, f"stellenangebote__page-{pg}.html", page("Stellenangebote", body))
    for i, (t, c) in enumerate(titles):
        w(s, f"stellenangebote/{i}.html", detail(t, c, ["Aufgabe A", "Aufgabe B"], ["Profil A"], "<p>Vollzeit</p>"))
    expected[s] = {
        "career_pages": [f"https://{s}/stellenangebote"],
        "outcome": "jobs_found",
        "jobs": [{"title": t, "city": c} for t, c in titles],
    }

    # 9 PDF job descriptions ---------------------------------------------------------------------------------------
    s = dom("pdf")
    home(s, "/karriere")
    w(
        s,
        "karriere.html",
        page(
            "Karriere",
            """<h1>Karriere</h1><p>Unsere offenen Stellen als PDF:</p>
<a href="/files/stellenanzeige-netzwerkadministrator.pdf">Netzwerkadministrator (m/w/d) – Stellenanzeige (PDF)</a>
<a href="/files/agb.pdf">AGB (PDF)</a>""",
        ),
    )
    w(
        s,
        "files/stellenanzeige-netzwerkadministrator.pdf",
        tiny_pdf(
            [
                "Netzwerkadministrator (m/w/d)",
                "Standort: 01445 Radebeul",
                "Ihre Aufgaben:",
                "- Betrieb von Firewalls und VPN",
                "- Pflege der Linux-Server",
                "Ihr Profil:",
                "- Erfahrung mit Cisco und Linux",
                "- Gute Deutschkenntnisse",
                "Vollzeit, unbefristet. Bewerbung an jobs@example.org",
            ]
        ),
    )
    w(s, "files/agb.pdf", tiny_pdf(["Allgemeine Geschaeftsbedingungen"]))
    expected[s] = {
        "career_pages": [f"https://{s}/karriere"],
        "outcome": "jobs_found",
        "jobs": [{"title": "Netzwerkadministrator (m/w/d)"}],
    }

    # 10 no vacancies ------------------------------------------------------------------------------------------------
    s = dom("novacancy")
    home(s, "/karriere")
    w(
        s,
        "karriere.html",
        page(
            "Karriere",
            "<h1>Karriere</h1><p>Derzeit haben wir leider keine offenen Stellen. "
            "Initiativbewerbungen sind jederzeit willkommen.</p>",
        ),
    )
    expected[s] = {"career_pages": [f"https://{s}/karriere"], "outcome": "no_vacancies", "jobs": []}

    # 11 rate limited -------------------------------------------------------------------------------------------------
    s = dom("ratelimit")
    home(s, "/karriere")
    routes(s, {"/": {"status": 429, "headers": {"retry-after": "120"}, "body": "Too many requests"}})
    expected[s] = {"career_pages": [], "outcome": "rate_limited", "jobs": [], "retry_after_s": 120}

    # 12 timeout on career page ----------------------------------------------------------------------------------------
    s = dom("timeout")
    home(s, "/karriere")
    routes(s, {"/karriere": {"delay_s": 0.01}})
    expected[s] = {"career_pages": [], "outcome": "no_career_page", "jobs": [], "expect_error": "unreachable"}

    # 13 malformed HTML + broken JSON-LD ----------------------------------------------------------------------------------
    s = dom("malformed")
    home(s, "/jobs")
    w(
        s,
        "jobs.html",
        "<html><head><title>Jobs</title><body><h1>Jobs</h1><ul><li><a href='/jobs/1'>Kundenberater (m/w/d)"
        "<li><a href='/jobs/2'>Lagerist (m/w/d)</a><div><p>unclosed",
    )
    w(
        s,
        "jobs/1.html",
        '<html><head><script type=\'application/ld+json\'>{"@context":"https://schema.org","@type":'
        '"JobPosting","title":"Kundenberater (m/w/d)","description":"Zeile 1\nZeile 2",'
        '"jobLocation":{"address":{"addressLocality":"Bautzen",}},}</script><body><h1>Kundenberater',
    )
    w(
        s,
        "jobs/2.html",
        "<html><body><h1>Lagerist (m/w/d)<p>Standort: Görlitz<h2>Ihre Aufgaben<ul><li>Kommissionierung"
        "<li>Wareneingang</ul><h2>Ihr Profil<ul><li>Staplerschein",
    )
    expected[s] = {
        "career_pages": [f"https://{s}/jobs"],
        "outcome": "jobs_found",
        "jobs": [
            {"title": "Kundenberater (m/w/d)", "city": "Bautzen"},
            {"title": "Lagerist (m/w/d)", "city": "Görlitz"},
        ],
    }

    # 14 no career page at all (early stopping) -----------------------------------------------------------------------------
    s = dom("nocareer")
    w(
        s,
        "index.html",
        page(
            "Start",
            "<h1>Maschinenbau</h1>"
            + "".join(
                f"<a href='/produkte/maschine-{i}'>Maschine {i}</a><a href='/referenzen/{i}'>Referenz {i}</a>"
                for i in range(30)
            )
            + "<a href='/ueber-uns'>Über uns</a><a href='/team'>Team</a>",
        ),
    )
    w(s, "ueber-uns.html", page("Über uns", "<h1>Über uns</h1><a href='/geschichte'>Geschichte</a>"))
    w(s, "team.html", page("Team", "<h1>Team</h1>"))
    expected[s] = {"career_pages": [], "outcome": "no_career_page", "jobs": []}

    # 15 career page only in sitemap -----------------------------------------------------------------------------------------
    s = dom("sitemap")
    w(s, "index.html", page("Start", "<h1>Willkommen</h1><a href='/leistungen'>Leistungen</a>"))
    w(s, "robots.txt", f"User-agent: *\nAllow: /\nSitemap: https://{s}/sitemap_index.xml\n")
    w(
        s,
        "sitemap_index.xml",
        f"<?xml version='1.0'?><sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
        f"<sitemap><loc>https://{s}/post-sitemap.xml</loc></sitemap>"
        f"<sitemap><loc>https://{s}/page-sitemap.xml</loc></sitemap></sitemapindex>",
    )
    w(
        s,
        "page-sitemap.xml",
        "<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
        + "".join(
            f"<url><loc>https://{s}{u}</loc></url>"
            for u in ["/", "/leistungen", "/unternehmen/karriere/offene-stellen", "/datenschutz"]
        )
        + "</urlset>",
    )
    w(
        s,
        "post-sitemap.xml",
        "<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
        + "".join(f"<url><loc>https://{s}/blog/{i}</loc></url>" for i in range(50))
        + "</urlset>",
    )
    w(
        s,
        "unternehmen/karriere/offene-stellen.html",
        page(
            "Offene Stellen",
            """<h1>Offene Stellen</h1>
<a href="/unternehmen/karriere/offene-stellen/servicetechniker">Servicetechniker (m/w/d)</a>
<a href="/unternehmen/karriere/offene-stellen/buchhalter">Buchhalter (m/w/d)</a>""",
        ),
    )
    w(
        s,
        "unternehmen/karriere/offene-stellen/servicetechniker.html",
        detail("Servicetechniker (m/w/d)", "08523 Plauen", ["Wartung"], ["Ausbildung"], "<p>Vollzeit</p>"),
    )
    w(
        s,
        "unternehmen/karriere/offene-stellen/buchhalter.html",
        detail("Buchhalter (m/w/d)", "Plauen", ["Buchhaltung"], ["DATEV"], "<p>Teilzeit</p>"),
    )
    expected[s] = {
        "career_pages": [f"https://{s}/unternehmen/karriere/offene-stellen"],
        "outcome": "jobs_found",
        "jobs": [
            {"title": "Servicetechniker (m/w/d)", "city": "Plauen", "postcode": "08523"},
            {"title": "Buchhalter (m/w/d)", "city": "Plauen"},
        ],
    }

    # 16 bot challenge -------------------------------------------------------------------------------------------------------
    s = dom("challenge")
    w(
        s,
        "index.html",
        "<html><head><title>Just a moment...</title></head><body><div id='cf-chl-widget'>Checking your "
        "browser before accessing</div><script src='/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1'>"
        "</script></body></html>",
    )
    expected[s] = {"career_pages": [], "outcome": "access_restricted", "jobs": []}

    (ROOT / "_expected.json").write_text(json.dumps(expected, indent=2, ensure_ascii=False), encoding="utf-8")
    return expected


if __name__ == "__main__":
    e = build()
    print(f"built {len(e)} sites, {sum(len(v['jobs']) for v in e.values())} expected postings in {ROOT}")
