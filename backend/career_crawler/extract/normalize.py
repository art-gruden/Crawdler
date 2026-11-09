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

from .skills import find_languages, find_skills

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
    r"(100|komplett)\s*%?\s*(home[- ]?office|mobiles arbeiten))\b",
    re.I,
)
HYBRID = re.compile(
    r"\b(hybrid|teilweise remote|remote[- ]option|home[- ]?office[- ]?(möglich|option|anteil|tage)|"
    r"mobiles arbeiten|mobile work|flexible[rn]? arbeitsort|\d\s*tage?\s*(home[- ]?office|remote))\b",
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
    if re.search(r"\b(vor ort|onsite|on-site|in office|präsenz)\b", joined, re.I):
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
CITY_COUNTRY = {"Wien": "AT", "Zürich": "CH"}
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
    if not loc["country"]:
        if loc["city"] in CITY_COUNTRY:
            loc["country"] = CITY_COUNTRY[loc["city"]]
        elif (
            (loc["postcode"] and re.fullmatch(r"\d{5}", loc["postcode"]))
            or loc["city"] in KNOWN_CITIES
            or (raw and re.search(r"deutschland|germany", raw, re.I))
        ):
            loc["country"] = "DE"
    return loc


# --- salary ------------------------------------------------------------------------

SALARY = re.compile(
    r"(?P<cur>€|EUR|Euro)?\s*(?P<a>\d{1,3}(?:[.\s']\d{3})+|\d{2,6})(?:[,.]\d{2})?\s*(?P<k>k|tsd\.?|T€)?\s*"
    r"(?:€|EUR|Euro)?\s*(?:[-–—]|bis|to)\s*(?P<cur2>€|EUR|Euro)?\s*(?P<b>\d{1,3}(?:[.\s']\d{3})+|\d{2,6})(?:[,.]\d{2})?"
    r"\s*(?P<k2>k|tsd\.?|T€)?\s*(?P<cur3>€|EUR|Euro)?\s*(?P<per>(brutto\s*)?(pro|per|/|im|je)\s*(jahr|year|monat|month|stunde|hour|std)|"
    r"jährlich|monatlich|p\.\s?a\.|annually)?",
    re.I,
)


def _num(s: str, k: str | None) -> float:
    v = float(re.sub(r"[.\s']", "", s))
    return v * 1000 if k else v


def salary_from_text(text: str) -> dict | None:
    for m in SALARY.finditer(text[:30_000]):
        if not (m["cur"] or m["cur2"] or m["cur3"]):
            continue
        lo, hi = _num(m["a"], m["k"] or m["k2"]), _num(m["b"], m["k2"] or m["k"])
        if not (0 < lo <= hi):
            continue
        per = (m["per"] or "").lower()
        period = (
            "hour"
            if re.search(r"stunde|hour|std", per)
            else "month"
            if re.search(r"monat|month", per)
            else "year"
            if per
            else None
        )
        if period is None:
            period = "year" if lo >= 15_000 else "month" if lo >= 1000 else "hour" if lo < 200 else None
        if period == "year" and not (10_000 <= lo <= 500_000):
            continue
        return {"min": lo, "max": hi, "currency": "EUR", "period": period, "raw": m.group(0).strip()[:120]}
    return None


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


def normalize(raw: RawJob, *, max_chars: int = 40_000) -> Job | None:
    title = tidy_title(raw.title)
    if len(title) < 3:
        return None
    text = raw.description_text or html_to_text(raw.description_html or "", max_chars)
    text = clean_text(text)[:max_chars] if text else ""
    loc = parse_location(raw.location_raw, raw.locations)
    resp, req = split_sections(text)
    sal = raw.salary or (salary_from_text(text) if text else None)
    rt = remote_type(
        raw.remote_raw,
        raw.location_raw,
        title,
        text[:6000],
        schema_location_type=raw.source_values.get("jobLocationType"),
    )
    sv = {k: v for k, v in raw.source_values.items() if v not in (None, "", [], {})}
    for k, v in (
        ("datePosted", raw.date_posted_raw),
        ("validThrough", raw.valid_through_raw),
        ("employmentType", raw.employment_raw or None),
        ("location", raw.location_raw),
    ):
        if v and k not in sv:
            sv[k] = v
    return Job(
        title=title,
        title_norm=normalize_title(title),
        source_url=raw.source_url,
        method=raw.method,
        application_url=raw.application_url,
        career_page_url=raw.career_page_url,
        location_raw=(raw.location_raw or "")[:300] or None,
        city=loc["city"],
        postcode=loc["postcode"],
        region=loc["region"],
        country=loc["country"],
        remote_type=rt,
        employment_types=employment_types(raw.employment_raw, title),
        date_posted=parse_date(raw.date_posted_raw),
        valid_through=parse_datetime(raw.valid_through_raw),
        description=text or None,
        requirements=req,
        responsibilities=resp,
        languages=find_languages(text),
        skills=find_skills(f"{title}\n{text}"),
        salary_min=sal["min"] if sal else None,
        salary_max=sal["max"] if sal else None,
        salary_currency=(sal.get("currency") or "EUR")[:3] if sal else None,
        salary_period=sal.get("period") if sal else None,
        salary_raw=sal.get("raw") if sal else None,
        ats_type=raw.ats_type,
        ats_account=raw.ats_account,
        ats_job_id=str(raw.ats_job_id) if raw.ats_job_id is not None else None,
        confidence=round(max(0.0, min(1.0, raw.confidence)), 2),
        content_hash=content_hash(title, loc["city"], text[:4000]),
        source_values=_bounded(sv),
    )


def _bounded(d: dict, limit: int = 4000) -> dict:
    out = {}
    for k, v in d.items():
        s = str(v)
        out[k] = v if len(s) <= limit // 4 else s[: limit // 4]
    return out
