"""Normalization of raw postings (from any strategy) into one structure.

Rules that matter for data quality:
* ``date_posted`` comes only from the source. Discovery time is stored separately and is
  never substituted.
* Original source values are kept in ``source_values`` (bounded, whitelisted).
* Descriptions are plain text; HTML never reaches the database.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html as html_lib
import re
import unicodedata
from dataclasses import dataclass, field

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from gcc_core.geo import GeoResult, Place, RemoteScope, remote_scope, resolve_location

from . import taxonomy
from .skills import find_languages, find_skills

NORMALIZATION_VERSION = 2  # bump to make the maintenance backfill re-derive stored postings

# --- raw posting ------------------------------------------------------------------


@dataclass
class RawJob:
    title: str
    source_url: str
    method: str  # ats | jsonld | html | content | pdf | browser | llm
    application_url: str | None = None
    career_page_url: str | None = None
    location_raw: str | None = None
    locations: list[dict] = field(default_factory=list)  # [{"city","postcode","region","country"}]
    remote_raw: str | None = None
    employment_raw: list[str] = field(default_factory=list)
    date_posted_raw: str | None = None
    valid_through_raw: str | None = None
    description_html: str | None = None
    description_text: str | None = None
    salary: dict | None = None  # {"min","max","currency","period","raw"}
    ats_type: str | None = None
    ats_account: str | None = None
    ats_job_id: str | None = None
    hiring_org: str | None = None
    confidence: float = 0.5
    source_values: dict = field(default_factory=dict)
    rendered: bool = False  # came from the browser


@dataclass
class Job:
    title: str
    title_norm: str
    source_url: str
    method: str
    application_url: str | None
    career_page_url: str | None
    location_raw: str | None
    city: str | None
    postcode: str | None
    region: str | None
    country: str | None
    remote_type: str
    employment_types: list[str]
    date_posted: dt.date | None
    valid_through: dt.datetime | None
    description: str | None
    requirements: list[str]
    responsibilities: list[str]
    languages: list[dict]
    skills: list[tuple[str, str]]
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    salary_period: str | None
    salary_raw: str | None
    ats_type: str | None
    ats_account: str | None
    ats_job_id: str | None
    confidence: float
    content_hash: str
    source_values: dict
    # --- DACH platform fields (all optional so older constructions keep working) ---
    geo: GeoResult | None = None
    locations_struct: list[dict] = field(default_factory=list)
    work_model: str = "UNKNOWN"  # REMOTE | HYBRID | ONSITE | UNKNOWN
    remote_scope: str = "unknown"
    remote_countries: list[str] = field(default_factory=list)
    employment_kinds: list[str] = field(default_factory=lambda: ["UNKNOWN"])
    specialization_id: int | None = None
    specialization_confidence: float = 0.0
    secondary_specializations: list[tuple[int, float]] = field(default_factory=list)
    seniority: str = "UNKNOWN"
    salary_basis: str = "unknown"  # gross | net | unknown
    salary_confidence: float | None = None
    description_hash: str | None = None
    quality_score: float = 0.0
    quality: dict = field(default_factory=dict)


# --- text ---------------------------------------------------------------------------

BLOCK_TAGS = {
    "p",
    "div",
    "li",
    "br",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "tr",
    "section",
    "article",
    "ul",
    "ol",
    "dd",
    "dt",
}
WS = re.compile(r"[ \t   ]+")


def html_to_text(markup: str, max_chars: int = 40_000) -> str:
    """Plain text with line breaks at block boundaries; scripts/styles dropped."""
    if not markup:
        return ""
    if "<" not in markup:
        return clean_text(html_lib.unescape(markup))[:max_chars]
    tree = HTMLParser(markup)
    for node in tree.css("script, style, noscript, template, svg, iframe, form, button, nav, footer"):
        node.decompose()
    root = tree.body or tree.root
    if root is None:
        return ""
    parts: list[str] = []
    for node in root.traverse(include_text=True):
        if node.tag == "-text":
            parts.append(node.text_content or "")
        elif node.tag in BLOCK_TAGS:
            parts.append("\n- " if node.tag == "li" else "\n")
    return clean_text("".join(parts))[:max_chars]


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text.replace("\r", "\n"))
    lines = [WS.sub(" ", ln).strip() for ln in text.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln in ("-", ""):
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(ln)
    return "\n".join(out).strip()


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = text.replace("ß", "ss")
    return "".join(c for c in text if not unicodedata.combining(c))


GENDER_TAG = re.compile(
    r"\(\s*(?:[mwfdixg]\s*[/|,]\s*)+[mwfdixg]\s*\)|\(\s*all genders?\s*\)|\(\s*gn\s*\)|\(\s*w/m/d\s*\)|"
    r"\b[mwfd]\s*/\s*[mwfd]\s*/\s*[dix]\b|\*(?:in|innen)\b",
    re.I,
)


def normalize_title(title: str) -> str:
    t = GENDER_TAG.sub(" ", title)
    t = re.sub(r"[^\w+#./ -]", " ", fold(t))
    return re.sub(r"\s+", " ", t).strip()


def tidy_title(title: str) -> str:
    title = html_lib.unescape(re.sub(r"<[^>]+>", " ", title or ""))
    return re.sub(r"\s+", " ", title).strip()[:300]


# --- remote / employment ----------------------------------------------------------

REMOTE = re.compile(
    r"\b(100\s*%\s*remote|full(y|y)? remote|fully remote|remote[- ]first|remote[- ]only|komplett remote|"
    r"vollständig remote|ausschließlich remote|deutschlandweit remote|work from anywhere|telecommute|"
    r"(100|komplett)\s*%?\s*(home[- ]?office|mobiles arbeiten)|100\s*%\s*(télétravail|teletravail|remoto)|"
    r"full[- ]?remote|télétravail complet|teletravail complet|lavoro (completamente )?da remoto)\b",
    re.I,
)
HYBRID = re.compile(
    r"\b(hybrid|hybride|ibrido|teilweise remote|remote[- ]option|home[- ]?office[- ]?(möglich|option|anteil|tage)|"
    r"mobiles arbeiten|mobile work|flexible[rn]? arbeitsort|\d\s*tage?\s*(home[- ]?office|remote)|"
    r"télétravail (partiel|possible)|teletravail (partiel|possible)|smart working|lavoro agile)\b",
    re.I,
)

EMPLOYMENT = [
    ("full_time", re.compile(r"\b(full[_ -]?time|vollzeit|vz\b)", re.I)),
    ("part_time", re.compile(r"\b(part[_ -]?time|teilzeit|tz\b)", re.I)),
    ("contract", re.compile(r"\b(contract(or)?|freelance|freiberuf|befristet|temporary|temporär|projektbasis)", re.I)),
    ("permanent", re.compile(r"\b(permanent|unbefristet|festanstellung)", re.I)),
    ("working_student", re.compile(r"\b(werkstudent|working student)", re.I)),
    ("internship", re.compile(r"\b(intern(ship)?|praktik(um|ant))", re.I)),
    (
        "apprenticeship",
        re.compile(r"\b(ausbildung|azubi|auszubildende|apprentice|duales studium|dual(es)? stud)", re.I),
    ),
    ("mini_job", re.compile(r"\b(minijob|geringfügig|520|538|556)\s*(€|euro)?\b", re.I)),
    ("thesis", re.compile(r"\b(abschlussarbeit|masterarbeit|bachelorarbeit|thesis)", re.I)),
]
SCHEMA_EMPLOYMENT = {
    "FULL_TIME": "full_time",
    "PART_TIME": "part_time",
    "CONTRACTOR": "contract",
    "TEMPORARY": "contract",
    "INTERN": "internship",
    "VOLUNTEER": "volunteer",
    "PER_DIEM": "contract",
    "OTHER": None,
}


def employment_types(values: list[str], title: str = "") -> list[str]:
    out: list[str] = []
    for v in values:
        key = str(v).strip().upper().replace("-", "_").replace(" ", "_")
        if key in SCHEMA_EMPLOYMENT:
            mapped = SCHEMA_EMPLOYMENT[key]
            if mapped:
                out.append(mapped)
            continue
        out.extend(name for name, rx in EMPLOYMENT if rx.search(str(v)))
    out.extend(
        name
        for name, rx in EMPLOYMENT
        if name in ("working_student", "internship", "apprenticeship", "thesis") and rx.search(title)
    )
    return list(dict.fromkeys(out))


# --- employment kinds (normalized enum, several allowed) ----------------------------------------

EMPLOYMENT_KINDS = (
    "FULL_TIME",
    "PART_TIME",
    "CONTRACT",
    "FREELANCE",
    "TEMPORARY",
    "INTERNSHIP",
    "APPRENTICESHIP",
    "WORKING_STUDENT",
    "OTHER",
    "UNKNOWN",
)
_KIND_RX = [
    ("FULL_TIME", re.compile(r"\b(full[_ -]?time|vollzeit|temps plein|tempo pieno|vz\b)", re.I)),
    ("PART_TIME", re.compile(r"\b(part[_ -]?time|teilzeit|temps partiel|tempo parziale|halbtags|tz\b)", re.I)),
    ("FREELANCE", re.compile(r"\b(freelanc\w*|freiberuf\w*|independent contractor|auf honorarbasis)", re.I)),
    ("CONTRACT", re.compile(r"\b(contract(or)?\b|werkvertrag|projektbasis|auf projektbasis|contrat de mission)", re.I)),
    (
        "TEMPORARY",
        re.compile(
            r"(?<![a-zäöü])(befristet\w*|temporary|temporär|zeitarbeit|arbeitnehmerüberlassung|fixed[- ]term|"
            r"\bcdd\b|tempo determinato|saisonal\w*|aushilfe|per[_ ]diem)",
            re.I,
        ),
    ),
    ("INTERNSHIP", re.compile(r"\b(intern(ship)?\b|praktik(um|ant\w*)|stagiaire|tirocinio|\bstage\b)", re.I)),
    (
        "APPRENTICESHIP",
        re.compile(
            r"\b(ausbildung|azubi|auszubildende\w*|apprentice\w*|duales? studium|dual(es)? stud\w*|alternance|"
            r"apprentissage|apprendistato|lehrling|lehrstelle)",
            re.I,
        ),
    ),
    ("WORKING_STUDENT", re.compile(r"\b(werkstudent\w*|working student|studentische[rn]? (hilfskraft|aushilfe)|student job)", re.I)),
    (
        "OTHER",
        re.compile(r"\b(minijob|geringfügig\w*|ehrenamt\w*|volunteer|abschlussarbeit|masterarbeit|bachelorarbeit|thesis)", re.I),
    ),
]
_SCHEMA_KIND = {
    "FULL_TIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "CONTRACTOR": "CONTRACT",
    "TEMPORARY": "TEMPORARY",
    "INTERN": "INTERNSHIP",
    "VOLUNTEER": "OTHER",
    "PER_DIEM": "TEMPORARY",
    "OTHER": "OTHER",
}
_PENSUM = re.compile(
    r"(?:pensum|beschäftigungsgrad|arbeitspensum|taux d.activit[ée]|tasso di occupazione|workload)\s*[:\-]?\s*"
    r"(\d{2,3})\s*(?:%|(?:[-–]|bis|à|a)\s*(\d{2,3})\s*%)|\b(\d{2,3})\s*[-–]\s*(100)\s*%"
)
_TITLE_KINDS = {"INTERNSHIP", "APPRENTICESHIP", "WORKING_STUDENT", "OTHER", "FREELANCE"}


def employment_kinds(values: list[str], title: str = "", text: str = "") -> list[str]:
    """Normalized employment kinds. Unknown stays UNKNOWN; "unbefristet" (permanent) is not a kind."""
    out: list[str] = []
    for v in values:
        key = str(v).strip().upper().replace("-", "_").replace(" ", "_")
        if key in _SCHEMA_KIND:
            out.append(_SCHEMA_KIND[key])
            continue
        out.extend(k for k, rx in _KIND_RX if rx.search(str(v)))
    out.extend(k for k, rx in _KIND_RX if k in _TITLE_KINDS and rx.search(title))
    if not out:  # weak evidence from the text, only the clearest hours markers
        head = text[:2500]
        out.extend(k for k, rx in _KIND_RX if k in ("FULL_TIME", "PART_TIME") and rx.search(head))
    blob = " ".join([*map(str, values), title, text[:2500]])
    if m := _PENSUM.search(blob):
        lo = int(m[1] or m[3])
        hi = int(m[2] or m[4] or lo)
        if hi >= 100:
            out.append("FULL_TIME")
        if lo < 100:
            out.append("PART_TIME")
    out = list(dict.fromkeys(out))
    return out or ["UNKNOWN"]


WORK_MODEL = {"remote": "REMOTE", "hybrid": "HYBRID", "onsite": "ONSITE", "unknown": "UNKNOWN"}


def remote_type(*texts: str | None, schema_location_type: str | None = None) -> str:
    if schema_location_type and "TELECOMMUTE" in schema_location_type.upper():
        return "remote"
    joined = " ".join(t for t in texts if t)
    if not joined:
        return "unknown"
    if REMOTE.search(joined):
        return "hybrid" if HYBRID.search(joined) and re.search(r"\bhybrid\b", joined, re.I) else "remote"
    if HYBRID.search(joined):
        return "hybrid"
    if re.search(r"\b(vor ort|onsite|on-site|in office|präsenz|sur site|in sede)\b", joined, re.I):
        return "onsite"
    return "unknown"


# --- dates -------------------------------------------------------------------------

DE_DATE = re.compile(r"\b(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})\b")
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})")
DE_MONTHS = {
    "januar": 1,
    "jan": 1,
    "februar": 2,
    "feb": 2,
    "märz": 3,
    "maerz": 3,
    "mär": 3,
    "march": 3,
    "april": 4,
    "apr": 4,
    "mai": 5,
    "may": 5,
    "juni": 6,
    "june": 6,
    "jun": 6,
    "juli": 7,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "oktober": 10,
    "october": 10,
    "okt": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "dezember": 12,
    "december": 12,
    "dez": 12,
    "dec": 12,
    "january": 1,
    "february": 2,
}
WORD_DATE = re.compile(r"\b(\d{1,2})\.?\s+([A-Za-zäÄ]{3,9})\.?\s+(\d{4})\b")


def parse_date(value) -> dt.date | None:
    """ISO 8601, epoch milliseconds, '01.02.2026', '1. Februar 2026'. Implausible dates → None."""
    if value is None or value == "":
        return None
    d = None
    if isinstance(value, (int, float)):
        try:
            ts = value / 1000 if value > 10_000_000_000 else value
            d = dt.datetime.fromtimestamp(ts, dt.UTC).date()
        except (OverflowError, OSError, ValueError):
            return None
    else:
        s = str(value).strip()
        if m := ISO_DATE.search(s):
            try:
                d = dt.date(int(m[1]), int(m[2]), int(m[3]))
            except ValueError:
                return None
        elif m := DE_DATE.search(s):
            try:
                d = dt.date(int(m[3]), int(m[2]), int(m[1]))
            except ValueError:
                return None
        elif (m := WORD_DATE.search(s)) and m[2].lower() in DE_MONTHS:
            try:
                d = dt.date(int(m[3]), DE_MONTHS[m[2].lower()], int(m[1]))
            except ValueError:
                return None
    if d is None:
        return None
    today = dt.datetime.now(dt.UTC).date()
    if d.year < 2000 or d > today + dt.timedelta(days=2):
        return None  # placeholders such as 1970-01-01 or future "posted" dates
    return d


def parse_datetime(value) -> dt.datetime | None:
    if not value:
        return None
    s = str(value).strip()
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.UTC)
    except ValueError:
        day = None
        if m := DE_DATE.search(s):
            try:
                day = dt.date(int(m[3]), int(m[2]), int(m[1]))
            except ValueError:
                return None
        return dt.datetime.combine(day, dt.time(23, 59), dt.UTC) if day else None


# --- location -----------------------------------------------------------------------

POSTCODE_CITY = re.compile(r"\b(\d{5})\s+([A-ZÄÖÜ][\wäöüß.\- ]{1,40}?)(?=$|[,;/()|]|\s{2}|\s-\s|\n)")
KNOWN_CITIES = (
    "Dresden",
    "Leipzig",
    "Chemnitz",
    "Zwickau",
    "Plauen",
    "Görlitz",
    "Freiberg",
    "Bautzen",
    "Pirna",
    "Meißen",
    "Freital",
    "Radebeul",
    "Riesa",
    "Grimma",
    "Döbeln",
    "Hoyerswerda",
    "Mittweida",
    "Annaberg-Buchholz",
    "Berlin",
    "München",
    "Hamburg",
    "Köln",
    "Frankfurt am Main",
    "Frankfurt",
    "Stuttgart",
    "Düsseldorf",
    "Nürnberg",
    "Hannover",
    "Bremen",
    "Dortmund",
    "Essen",
    "Erfurt",
    "Jena",
    "Halle (Saale)",
    "Halle",
    "Magdeburg",
    "Potsdam",
    "Cottbus",
    "Karlsruhe",
    "Mannheim",
    "Bonn",
    "Münster",
    "Wien",
    "Zürich",
)
COUNTRY = {
    "DE": "DE",
    "DEU": "DE",
    "GERMANY": "DE",
    "DEUTSCHLAND": "DE",
    "AT": "AT",
    "AUSTRIA": "AT",
    "ÖSTERREICH": "AT",
    "CH": "CH",
    "SCHWEIZ": "CH",
    "SWITZERLAND": "CH",
    "PL": "PL",
    "POLAND": "PL",
    "CZ": "CZ",
    "CZECHIA": "CZ",
    "NL": "NL",
    "US": "US",
    "USA": "US",
    "UNITED STATES": "US",
}


def parse_location(raw: str | None, structured: list[dict] | None = None) -> dict:
    loc = {"city": None, "postcode": None, "region": None, "country": None}
    for s in structured or []:
        for k in loc:
            if not loc[k] and s.get(k):
                loc[k] = str(s[k]).strip()[:80]
        if loc["city"]:
            break
    if loc["country"]:
        loc["country"] = COUNTRY.get(loc["country"].upper(), loc["country"][:2].upper())
    if raw and not loc["city"]:
        if m := POSTCODE_CITY.search(raw):
            loc["postcode"], loc["city"] = m[1], m[2].strip()
        else:
            for city in KNOWN_CITIES:
                if re.search(rf"(?<!\w){re.escape(city)}(?!\w)", raw, re.I):
                    loc["city"] = city
                    break
            else:
                first = re.split(r"[,;|/(]", raw)[0].strip()
                if 2 <= len(first) <= 40 and not re.search(
                    r"\d|remote|home|hybrid|deutschlandweit|germany", first, re.I
                ):
                    loc["city"] = first
    if raw and not loc["postcode"] and (m := re.search(r"\b(0[1-9]\d{3}|[1-9]\d{4})\b", raw)):
        loc["postcode"] = m[1]
    if not loc["country"] and raw:
        # only an explicit country word counts; postcodes and "known cities" are not evidence
        if re.search(r"\bdeutschland\b|\bgermany\b", raw, re.I):
            loc["country"] = "DE"
        elif re.search(r"\bösterreich\b|\boesterreich\b|\baustria\b", raw, re.I):
            loc["country"] = "AT"
        elif re.search(r"\bschweiz\b|\bswitzerland\b|\bsuisse\b|\bsvizzera\b", raw, re.I):
            loc["country"] = "CH"
    return loc


# --- salary ------------------------------------------------------------------------

_CUR = r"(?:€|EUR\b|Euro\b|CHF\b|SFr\.?|Fr\.(?=\s*\d)|Franken\b|US\$|USD\b|\$|GBP\b|£)"
_NUM = r"\d{1,3}(?:[.\s'’]\d{3})+|\d{2,6}"
_DEC = r"(?:[,.](?:\d{2}|[-–]))?"
_PER = (
    r"(?P<per>(?:brutto\s*)?(?:pro|per|/|im|je|par|al)\s*(?:jahr|year|monat|month|stunde|hour|std|an\b|mois|anno|mese|ora)|"
    r"jährlich|monatlich|p\.\s?a\.|annually|annuel\w*|mensuel\w*|annuo|mensile)?"
)
SALARY = re.compile(
    rf"(?P<cur>{_CUR})?\s*(?P<a>{_NUM}){_DEC}\s*(?P<k>k|tsd\.?|T€)?\s*(?P<cur1>{_CUR})?\s*"
    rf"(?:[-–—]|bis|to|und)\s*(?P<cur2>{_CUR})?\s*(?P<b>{_NUM}){_DEC}\s*(?P<k2>k|tsd\.?|T€)?\s*(?P<cur3>{_CUR})?\s*"
    + _PER,
    re.I,
)
_SINGLE = re.compile(
    rf"(?P<lead>ab|from|mindestens|min\.?|starting at|bis zu|up to|max\.?|maximal|"
    rf"(?:jahres|monats|grund|brutto)?(?:gehalt|salary|vergütung|verguetung|verdienst|lohn|compensation|salaire|stipendio)"
    rf")\W{{0,15}}(?P<cur>{_CUR})?\s*(?P<a>{_NUM}){_DEC}\s*(?P<k>k|tsd\.?|T€)?\s*(?P<cur2>{_CUR})?\s*" + _PER,
    re.I,
)
_CUR_ISO = {"€": "EUR", "eur": "EUR", "euro": "EUR", "chf": "CHF", "sfr": "CHF", "sfr.": "CHF", "fr.": "CHF",
            "franken": "CHF", "usd": "USD", "$": "USD", "us$": "USD", "gbp": "GBP", "£": "GBP"}


def currency_code(token: str | None) -> str | None:
    """ISO 4217 code for a currency symbol/word; unknown input stays unknown (no EUR default)."""
    if not token:
        return None
    t = token.strip().lower()
    if t in _CUR_ISO:
        return _CUR_ISO[t]
    return t.upper() if re.fullmatch(r"[a-z]{3}", t) else None


def _num(s: str, k: str | None) -> float:
    v = float(re.sub(r"[.\s'’]", "", s))
    return v * 1000 if k else v


def _basis(text: str, start: int, end: int) -> str:
    window = text[max(0, start - 40) : end + 40]
    if re.search(r"\bbrutto|\bgross\b|\blordo\b|\bbrut\b", window, re.I):
        return "gross"
    if re.search(r"\bnetto\b|\bnet\b(?!work)|\bnetto\b", window, re.I):
        return "net"
    return "unknown"


def _period(per: str, lo: float, currency: str | None) -> tuple[str | None, bool]:
    """(period, explicit). A missing unit is guessed from the magnitude and flagged as inferred."""
    per = (per or "").lower()
    if per:
        if re.search(r"stunde|hour|std|\bora\b", per):
            return "hour", True
        if re.search(r"monat|month|mois|mese|mensu", per):
            return "month", True
        return "year", True
    guess = "year" if lo >= 15_000 else "month" if lo >= 1000 else "hour" if lo < 200 else None
    return guess, False


def salary_from_text(text: str) -> dict | None:
    """Salary range/amount with currency, period, gross/net and a confidence. Needs a currency sign.

    Nothing is converted between periods and the currency is never assumed.
    """
    body = text[:30_000]
    cands: list[tuple[float, float | None, str | None, str, tuple[int, int]]] = []
    for m in SALARY.finditer(body):
        cur = currency_code(m["cur"] or m["cur1"] or m["cur2"] or m["cur3"])
        if not cur:
            continue
        lo, hi = _num(m["a"], m["k"] or m["k2"]), _num(m["b"], m["k2"] or m["k"])
        if not (0 < lo <= hi):
            continue
        cands.append((lo, hi, cur, m["per"] or "", m.span()))
        break
    else:
        for m in _SINGLE.finditer(body):
            cur = currency_code(m["cur"] or m["cur2"])
            if not cur:
                continue
            v = _num(m["a"], m["k"])
            lead = m["lead"].lower()
            if lead in ("bis zu", "up to", "max", "max.", "maximal"):
                lo, hi = None, v
            elif lead in ("ab", "from", "mindestens", "min", "min.", "starting at"):
                lo, hi = v, None
            else:
                lo = hi = v
            cands.append((lo, hi, cur, m["per"] or "", m.span()))
            break
    if not cands:
        return None
    lo, hi, cur, per, (st, en) = cands[0]
    ref = lo if lo is not None else hi
    period, explicit = _period(per, ref, cur)
    plausible = {"year": (10_000, 600_000), "month": (800, 50_000), "hour": (8, 400)}
    if period in plausible:
        lo_ok, hi_ok = plausible[period]
        if not (lo_ok <= ref <= hi_ok):
            return None
    return {
        "min": lo,
        "max": hi,
        "currency": cur,
        "period": period,
        "raw": body[st:en].strip()[:120],
        "basis": _basis(body, st, en),
        "confidence": 0.85 if explicit else 0.5,
    }


# --- sections ----------------------------------------------------------------------

RESP_HEAD = re.compile(
    r"^(ihre|deine|eure|die)?\s*(aufgaben|tätigkeiten|verantwortung|aufgabengebiet|tätigkeitsbereich)|"
    r"^(was (dich|sie) erwartet|das erwartet (dich|sie)|was du (bei uns )?(tust|machst|bewegst))|"
    r"^(your (tasks|role|responsibilities|mission)|responsibilities|what you('| wi)ll do|the role|job description)",
    re.I | re.M,
)
REQ_HEAD = re.compile(
    r"^(ihr|dein|euer|das)?\s*(profil|qualifikation(en)?|anforderungen|kenntnisse|voraussetzungen)|"
    r"^(was (du|sie) mitbring(st|en)|das bring(st du|en sie) mit|was wir (von dir|von ihnen)? ?erwarten|"
    r"damit überzeugst du)|"
    r"^(your (profile|skills|qualifications)|requirements|qualifications|what you bring|who you are|"
    r"what we('re| are) looking for|must have)",
    re.I | re.M,
)
OTHER_HEAD = re.compile(
    r"^(wir bieten|was wir (dir|ihnen) bieten|unser angebot|benefits|what we offer|das bieten wir|"
    r"deine vorteile|ihre vorteile|über uns|about us|kontakt|bewerbung|so geht es weiter|interessiert\??|"
    r"ansprechpartner|why us|nice to have)",
    re.I,
)


def split_sections(text: str) -> tuple[list[str], list[str]]:
    resp: list[str] = []
    req: list[str] = []
    current = None
    for line in text.split("\n"):
        stripped = line.strip().strip(":").strip()
        if not stripped:
            continue
        is_heading = len(stripped) <= 70 and not stripped.startswith("- ")
        if is_heading and RESP_HEAD.search(stripped):
            current = resp
            continue
        if is_heading and REQ_HEAD.search(stripped):
            current = req
            continue
        if is_heading and OTHER_HEAD.search(stripped):
            current = None
            continue
        if current is not None and len(current) < 25:
            item = stripped.lstrip("-•·*▪► ").strip()
            if 3 <= len(item) <= 400:
                current.append(item)
    return resp, req


# --- final normalization ------------------------------------------------------------


def content_hash(*parts: str | None) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(re.sub(r"\s+", " ", fold(p or "")).strip().encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def _primary_place(places: list[Place]) -> Place | None:
    if not places:
        return None
    return max(places, key=lambda p: (p.city is not None, p.confidence))


def _region_name(code: str | None) -> str | None:
    from gcc_core.geo_data import REGIONS

    return REGIONS[code][1] if code in REGIONS else None


def quality_report(
    *,
    description: str | None,
    geo: GeoResult | None,
    employment: list[str],
    work_model: str,
    has_salary: bool,
    languages: list[dict],
    spec_conf: float,
    date_posted,
    application_url: str | None,
    sections: bool,
    extraction_confidence: float,
    salary_confidence: float | None,
) -> tuple[float, dict]:
    """Completeness and confidence. A missing field lowers completeness only; it says nothing negative."""
    desc_len = len(description or "")
    best_geo = max((p.confidence for p in geo.places), default=0.0) if geo else 0.0
    city_level = bool(geo and any(p.city for p in geo.places))
    fields = {
        "description": 1.0 if desc_len >= 300 else 0.5 if desc_len >= 80 else 0.0,
        "location": (1.0 if city_level else 0.6) if best_geo else 0.0,
        "employment_type": 0.0 if employment == ["UNKNOWN"] else 1.0,
        "work_model": 0.0 if work_model == "UNKNOWN" else 1.0,
        "salary": 1.0 if has_salary else 0.0,
        "languages": 1.0 if languages else 0.0,
        "specialization": 1.0 if spec_conf >= 0.5 else 0.5 if spec_conf > 0 else 0.0,
        "published_at": 1.0 if date_posted else 0.0,
        "application_url": 1.0 if application_url else 0.0,
        "requirements": 1.0 if sections else 0.0,
    }
    weights = {
        "description": 0.2, "location": 0.2, "employment_type": 0.1, "work_model": 0.1, "salary": 0.1,
        "languages": 0.05, "specialization": 0.1, "published_at": 0.05, "application_url": 0.05, "requirements": 0.05,
    }
    completeness = round(sum(fields[k] * w for k, w in weights.items()), 2)
    conf = {
        "location": round(best_geo, 2) if best_geo else None,
        "specialization": round(spec_conf, 2) if spec_conf else None,
        "salary": salary_confidence,
        "languages": round(min((x["confidence"] for x in languages), default=0), 2) or None,
        "extraction": round(extraction_confidence, 2),
    }
    missing = [k for k, v in fields.items() if v == 0.0]
    score = round(0.6 * completeness + 0.4 * extraction_confidence, 2)
    return score, {
        "completeness": completeness,
        "missing": missing,
        "field_confidence": {k: v for k, v in conf.items() if v is not None},
        "note": "missing fields are unknown, not negative",
    }


def normalize(raw: RawJob, *, max_chars: int = 40_000, context_country: str | None = None) -> Job | None:
    title = tidy_title(raw.title)
    if len(title) < 3:
        return None
    text = raw.description_text or html_to_text(raw.description_html or "", max_chars)
    text = clean_text(text)[:max_chars] if text else ""
    loc = parse_location(raw.location_raw, raw.locations)
    geo = resolve_location(raw.location_raw, raw.locations, context_country=context_country)
    primary = _primary_place(geo.places)
    resp, req = split_sections(text)
    sal = raw.salary or (salary_from_text(text) if text else None)
    rt = remote_type(
        raw.remote_raw,
        raw.location_raw,
        title,
        text[:6000],
        schema_location_type=raw.source_values.get("jobLocationType"),
    )
    work_model = WORK_MODEL[rt]
    rscope: RemoteScope = remote_scope([raw.remote_raw, raw.location_raw, title, text[:6000]], work_model, geo.places)
    languages = find_languages(text, req)
    skills = find_skills(f"{title}\n{text}")
    kinds = employment_kinds(raw.employment_raw, title, text)
    cls = taxonomy.classify(title, text, [k for k, _ in skills])
    sv = {k: v for k, v in raw.source_values.items() if v not in (None, "", [], {})}
    for k, v in (
        ("datePosted", raw.date_posted_raw),
        ("validThrough", raw.valid_through_raw),
        ("employmentType", raw.employment_raw or None),
        ("location", raw.location_raw),
    ):
        if v and k not in sv:
            sv[k] = v
    # city/country columns: what the resolver established; otherwise the textual city with NO country
    city = primary.city if primary and primary.city else loc["city"]
    country = primary.country if primary else loc["country"]
    region = _region_name(primary.region) if primary and primary.region else loc["region"]
    postcode = (primary.postcode if primary and primary.postcode else None) or loc["postcode"]
    sal_currency = currency_code(sal.get("currency")) if sal else None
    sal_conf = None
    if sal:
        sal_conf = sal.get("confidence")
        if sal_conf is None:
            sal_conf = 0.9 if sal_currency else 0.4
    description_hash = hashlib.sha1(re.sub(r"\s+", " ", fold(text)).encode()).hexdigest() if text else None
    qscore, qdetail = quality_report(
        description=text,
        geo=geo,
        employment=kinds,
        work_model=work_model,
        has_salary=bool(sal),
        languages=languages,
        spec_conf=cls.confidence if cls.primary_id else 0.0,
        date_posted=parse_date(raw.date_posted_raw),
        application_url=raw.application_url,
        sections=bool(req or resp),
        extraction_confidence=max(0.0, min(1.0, raw.confidence)),
        salary_confidence=sal_conf,
    )
    return Job(
        title=title,
        title_norm=normalize_title(title),
        source_url=raw.source_url,
        method=raw.method,
        application_url=raw.application_url,
        career_page_url=raw.career_page_url,
        location_raw=(raw.location_raw or "")[:300] or None,
        city=city,
        postcode=postcode,
        region=region,
        country=country,
        remote_type=rt,
        employment_types=employment_types(raw.employment_raw, title),
        date_posted=parse_date(raw.date_posted_raw),
        valid_through=parse_datetime(raw.valid_through_raw),
        description=text or None,
        requirements=req,
        responsibilities=resp,
        languages=languages,
        skills=skills,
        salary_min=sal.get("min") if sal else None,
        salary_max=sal.get("max") if sal else None,
        salary_currency=sal_currency,
        salary_period=sal.get("period") if sal else None,
        salary_raw=sal.get("raw") if sal else None,
        ats_type=raw.ats_type,
        ats_account=raw.ats_account,
        ats_job_id=str(raw.ats_job_id) if raw.ats_job_id is not None else None,
        confidence=round(max(0.0, min(1.0, raw.confidence)), 2),
        content_hash=content_hash(title, city, text[:4000]),
        source_values=_bounded(sv),
        geo=geo,
        locations_struct=list(raw.locations or [])[:10],
        work_model=work_model,
        remote_scope=rscope.scope,
        remote_countries=rscope.countries,
        employment_kinds=kinds,
        specialization_id=cls.primary_id,
        specialization_confidence=cls.confidence if cls.primary_id else 0.0,
        secondary_specializations=cls.secondary,
        seniority=cls.seniority,
        salary_basis=(sal.get("basis") or "unknown") if sal else "unknown",
        salary_confidence=sal_conf,
        description_hash=description_hash,
        quality_score=qscore,
        quality=qdetail,
    )


def _bounded(d: dict, limit: int = 4000) -> dict:
    out = {}
    for k, v in d.items():
        s = str(v)
        out[k] = v if len(s) <= limit // 4 else s[: limit // 4]
    return out
