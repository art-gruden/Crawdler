"""Geography resolution and DACH-aware extraction (fixtures only)."""

from __future__ import annotations

import pytest

from career_crawler.extract.normalize import RawJob, normalize
from career_crawler.extract.skills import find_languages
from gcc_core.geo import remote_scope, resolve_location

CASES = [
    ("Berlin", "DE", "DE-BE", "Berlin"),
    ("Dresden", "DE", "DE-SN", "Dresden"),
    ("München", "DE", "DE-BY", "München"),
    ("Munich, Germany", "DE", "DE-BY", "München"),
    ("Muenchen", "DE", "DE-BY", "München"),
    ("Wien", "AT", "AT-9", "Wien"),
    ("Vienna, Austria", "AT", "AT-9", "Wien"),
    ("Graz", "AT", "AT-6", "Graz"),
    ("Linz", "AT", "AT-4", "Linz"),
    ("Zürich", "CH", "CH-ZH", "Zürich"),
    ("Zurich, Switzerland", "CH", "CH-ZH", "Zürich"),
    ("Basel", "CH", "CH-BS", "Basel"),
    ("Genève", "CH", "CH-GE", "Genève"),
    ("Geneva", "CH", "CH-GE", "Genève"),
]


@pytest.mark.parametrize("raw,country,region,city", CASES)
def test_known_cities_resolve_with_evidence(raw, country, region, city):
    r = resolve_location(raw)
    assert r.status == "resolved", r.detail()
    p = r.places[0]
    assert (p.country, p.region, p.city) == (country, region, city)
    assert p.confidence > 0 and p.source


def test_postcode_is_not_country_evidence():
    r = resolve_location("01067")
    assert r.status in ("unknown", "partial")
    assert not r.countries


def test_unknown_place_is_never_silently_german():
    r = resolve_location("Wilsdruff")
    assert r.status != "resolved"
    assert "DE" not in r.countries


def test_same_city_name_in_two_countries_is_ambiguous_without_evidence():
    assert resolve_location("Rheinfelden").status == "ambiguous"
    r = resolve_location("Rheinfelden", [{"country": "CH"}])
    assert r.status == "resolved" and r.places[0].region == "CH-AG"


def test_structured_country_breaks_the_tie_and_region_names_work():
    r = resolve_location("Freiburg, Schweiz")
    assert r.places[0].country == "CH" and r.places[0].city == "Fribourg"
    r = resolve_location("Dresden, Sachsen")
    assert r.places[0].region == "DE-SN"


def test_multi_location_and_dach_wide_postings():
    r = resolve_location("Berlin; Wien; Zürich")
    assert set(r.countries) == {"DE", "AT", "CH"}
    r = resolve_location("DACH")
    assert set(r.countries) == {"DE", "AT", "CH"}
    assert all(p.city is None for p in r.places)


def test_remote_restricted_to_countries_is_not_worldwide():
    places = resolve_location("Wien").places
    rs = remote_scope(["Remote in Austria"], "REMOTE", places)
    assert rs.scope == "countries" and rs.countries == ["AT"]
    assert remote_scope(["100% remote"], "REMOTE", []).scope == "unknown"
    assert remote_scope(["Remote, anywhere in the world"], "REMOTE", []).scope == "worldwide"


def test_company_country_only_breaks_ties():
    raw = RawJob(title="Entwickler", source_url="https://x.example/1", method="html", location_raw="Rheinfelden")
    assert normalize(raw).country is None
    assert normalize(raw, context_country="CH").country == "CH"


def _norm(title, text, **kw):
    return normalize(RawJob(title=title, source_url="https://x.example/j", method="html", description_text=text, **kw))


def test_german_posting_extraction():
    j = _norm(
        "Senior Backend Entwickler Python (m/w/d)",
        "Wir suchen Verstärkung in Teilzeit. Sie bringen Python, Django und PostgreSQL mit. "
        "Deutsch C1 erforderlich, Englisch B2 von Vorteil. Gehalt: 70.000 - 85.000 € brutto pro Jahr. Hybrid möglich.",
        location_raw="Leipzig, Sachsen",
    )
    assert j.seniority == "SENIOR" and j.specialization_id is not None
    assert "PART_TIME" in j.employment_kinds and j.work_model == "HYBRID"
    langs = {x["language"]: x for x in j.languages}
    assert langs["de"]["level"] == "C1" and langs["de"]["requirement"] == "required"
    assert langs["en"]["level"] == "B2" and langs["en"]["requirement"] == "preferred"
    assert (j.salary_min, j.salary_max, j.salary_currency, j.salary_basis) == (70000, 85000, "EUR", "gross")


def test_english_posting_with_chf_salary_and_no_eur_assumption():
    j = _norm(
        "Junior Data Engineer",
        "Join our team in Zurich. SQL, Python, Airflow. Fluent English required. CHF 95'000 - 110'000 per year.",
        location_raw="Zürich, Switzerland",
    )
    assert j.country == "CH" and j.seniority == "JUNIOR"
    assert j.salary_currency == "CHF" and j.salary_min == 95000
    langs = {x["language"]: x for x in j.languages}
    assert langs["en"]["level"] is None and langs["en"]["descriptor"] == "fluent"  # never an invented CEFR level


def test_french_and_italian_postings():
    f = _norm(
        "Développeur logiciel senior",
        "Poste en télétravail partiel à Genève. Français courant, anglais B2. CDI temps plein. Salaire CHF 110 000 à 130 000 par an.",
        location_raw="Genève",
    )
    assert f.country == "CH" and f.work_model == "HYBRID"
    assert f.salary_currency == "CHF"
    i = _norm(
        "Sviluppatore software",
        "Lavoro agile a Lugano. Italiano madrelingua, tedesco B1 richiesto.",
        location_raw="Lugano, Svizzera",
    )
    assert i.country == "CH" and i.work_model == "HYBRID"


def test_missing_salary_is_missing_not_zero():
    j = _norm("Projektmanager", "Spannende Aufgaben in Berlin.", location_raw="Berlin")
    assert j.salary_min is None and j.salary_max is None and j.salary_currency is None


def test_freelance_and_other_employment_kinds():
    j = _norm("DevOps Engineer (Freelance)", "Projektbasis, 6 Monate. Remote.", location_raw="Wien")
    assert "FREELANCE" in j.employment_kinds or "CONTRACT" in j.employment_kinds
    k = _norm("Werkstudent Marketing", "Werkstudent (m/w/d) 20h pro Woche", location_raw="Graz")
    assert "WORKING_STUDENT" in k.employment_kinds


def test_cefr_is_only_taken_when_written():
    langs = {x["language"]: x for x in find_languages("Sie sprechen fließend Deutsch und gut Englisch.")}
    assert langs["de"]["level"] is None and langs["en"]["level"] is None
    rng = {x["language"]: x for x in find_languages("Englisch B2-C1 erforderlich")}
    assert rng["en"]["level"] == "B2"  # range -> lower bound


@pytest.mark.parametrize("raw", ["Dresden (Sachsen", "(", "((", "Berlin (a (b)", "x)(", "Köln (", "(Wien", "a ( b ( c ) d"])
def test_unbalanced_parentheses_never_recurse(raw):
    # regression: "Dresden (Sachsen" recursed forever and aborted the production migration
    r = resolve_location(raw)
    assert r.status in ("resolved", "partial", "ambiguous", "unknown")
    assert normalize(RawJob(title="Entwickler", source_url="https://x.example/p", method="html", location_raw=raw)) is not None


def test_resolver_survives_random_garbage():
    import random

    rnd = random.Random(3)
    alphabet = "()[]Dresden Wien,;/ -üßZ0123 remote\n\t:"
    for _ in range(3000):
        resolve_location("".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 60))))
