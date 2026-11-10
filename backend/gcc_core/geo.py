"""Location resolver for DACH (pure, no database).

Design rules
------------
* A country is assigned only from evidence: an explicit country/region token, a structured
  source field, or a gazetteer city that exists in exactly one country. A 5-digit postcode, the
  language of a posting or "most vacancies are German" is *not* evidence.
* Every place carries ``confidence`` and ``source`` so weak assignments stay visible.
* What cannot be resolved is reported explicitly: ``unresolved`` entries with a reason
  (``ambiguous``, ``country_unknown``) and a status of ``resolved`` / ``partial`` /
  ``ambiguous`` / ``unknown``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .geo_data import CITIES, COUNTRIES, OTHER_COUNTRIES, REGIONS

DACH = ("DE", "AT", "CH")
EU_IN_DACH = ("DE", "AT")  # Switzerland is not an EU member

# --- folding ------------------------------------------------------------------------------------


def fold(text: str) -> str:
    """Symmetric search key: München = Muenchen = Munchen, Straße = Strasse, Zürich = Zuerich."""
    t = unicodedata.normalize("NFKD", (text or "").lower().replace("ß", "ss"))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"ae|oe|ue", lambda m: m.group(0)[0], t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return t.strip()


# --- indexes ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    city: str
    region: str  # ISO 3166-2 code
    country: str


_COUNTRY_INDEX: dict[str, str] = {}
for _code, _c in COUNTRIES.items():
    for _a in [_c["name"], _c["name_local"], *_c["aliases"]]:
        _COUNTRY_INDEX[fold(_a)] = _code
for _code, _names in OTHER_COUNTRIES.items():
    for _a in [*_names, _code]:
        _COUNTRY_INDEX.setdefault(fold(_a), _code)

_REGION_INDEX: dict[str, set[str]] = {}
for _code, (_name, _local, _kind, _aliases) in REGIONS.items():
    for _a in [_name, _local, *_aliases]:
        _REGION_INDEX.setdefault(fold(_a), set()).add(_code)

_CITY_INDEX: dict[str, list[Candidate]] = {}
for _city, _region, _aliases in CITIES:
    _cand = Candidate(_city, _region, _region.split("-")[0])
    for _a in [_city, *_aliases]:
        _lst = _CITY_INDEX.setdefault(fold(_a), [])
        if _cand not in _lst:
            _lst.append(_cand)

# Names where the larger / far more common place is meant unless the text says otherwise.
DOMINANT = {
    "frankfurt": "Frankfurt am Main",
    "freiburg": "Freiburg im Breisgau",
}

# Region-level words that are also gazetteer cities (city-states, cantons named after their capital).
CITY_STATES = {"DE-BE", "DE-HH", "DE-HB", "AT-9"}

_NON_PLACE = {
    "remote",
    "home office",
    "homeoffice",
    "hybrid",
    "mobil",
    "mobiles arbeiten",
    "flexibel",
    "flexible",
    "diverse",
    "mehrere standorte",
    "various",
    "multiple locations",
    "n a",
    "tbd",
    "tba",
    "onsite",
    "on site",
    "vor ort",
    "worldwide",
    "weltweit",
    "global",
    "europa",
    "europe",
    "eu",
    "emea",
    "anywhere",
    "ueberall",
    "uberall",
    "home",
    "null",
    "none",
    "unknown",
}
_NATIONWIDE = {"deutschlandweit": "DE", "bundesweit": "DE", "osterreichweit": "AT", "schweizweit": "CH"}
_PREFIX = re.compile(
    r"^(?:standort(?:e)?|location|ort|arbeitsort|einsatzort|dienstort|sitz|raum|grossraum|region|nahe|umgebung|"
    r"umgebung von|bei|near|greater|metropolregion|im raum|stadt|city of|in|am standort)\s*[:\-]?\s+",
    re.I,
)
_REGION_PREFIX = re.compile(r"^(?:kanton|canton|cantone|bundesland|freistaat|land)\s+", re.I)
_GENDER = re.compile(r"\(\s*(?:[mwfdxig*]\s*[/|,*]\s*)+[mwfdxig*]\s*\)|\(\s*all genders?\s*\)|\(\s*gn\s*\)", re.I)
_SPLIT = re.compile(r"\s*(?:[;|•·\n]|,|\s[–—-]\s|\s/\s|\s&\s|\sund\s|\soder\s|\sand\s|\sor\s)\s*", re.I)
_POST_FIRST = re.compile(r"^(?:[A-Za-z]{1,2}[- ])?(\d{4,5})\s+(.+)$")
_POST_LAST = re.compile(r"^(.+?)\s+(\d{4,5})$")


@dataclass
class Place:
    country: str | None
    region: str | None = None
    city: str | None = None
    postcode: str | None = None
    lat: float | None = None
    lon: float | None = None
    confidence: float = 0.9
    source: str = "gazetteer"  # structured | gazetteer | text | context | legacy

    @property
    def key(self) -> tuple[str | None, str | None, str]:
        return (self.country, self.region, fold(self.city or ""))


@dataclass
class GeoResult:
    status: str = "unknown"  # resolved | partial | ambiguous | unknown
    places: list[Place] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)  # {"text","reason","candidates"?}

    @property
    def countries(self) -> list[str]:
        return sorted({p.country for p in self.places if p.country})

    @property
    def regions(self) -> list[str]:
        return sorted({p.region for p in self.places if p.region})

    def detail(self) -> dict:
        d: dict = {"status": self.status}
        if self.unresolved:
            d["unresolved"] = self.unresolved[:5]
        return d


# --- token classification ------------------------------------------------------------------------


@dataclass
class _Item:
    kind: str  # country | region | city | place | nationwide
    text: str
    countries: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    candidates: tuple[Candidate, ...] = ()
    postcode: str | None = None


def _strip(tok: str) -> str:
    tok = _GENDER.sub(" ", tok)
    tok = tok.strip(" \t.:-–—·•*\"'")
    prev = None
    while prev != tok:
        prev = tok
        tok = _PREFIX.sub("", tok).strip()
    return tok


def _lookup_city(name: str) -> list[Candidate]:
    f = fold(name)
    if f in _CITY_INDEX:
        return _CITY_INDEX[f]
    # "Leipzig Zentrum", "Dresden Neustadt": a gazetteer city followed by a district word
    parts = f.split()
    if len(parts) >= 2 and " ".join(parts[:-1]) in _CITY_INDEX:
        return _CITY_INDEX[" ".join(parts[:-1])]
    if len(parts) >= 2 and parts[-1] in {"stadt", "city"} and " ".join(parts[:-1]) in _CITY_INDEX:
        return _CITY_INDEX[" ".join(parts[:-1])]
    return []


def _classify(raw_tok: str, *, force_region: bool = False) -> list[_Item]:
    tok = _strip(raw_tok)
    if not tok or len(tok) > 80:
        return []
    forced = bool(_REGION_PREFIX.match(tok))
    tok = _REGION_PREFIX.sub("", tok).strip()
    f = fold(tok)
    if not f or f in _NON_PLACE:
        return []
    # parentheses: "Dresden (Sachsen)", "Halle (Saale)" -> try whole first
    if "(" in tok and f not in _CITY_INDEX:
        base = re.sub(r"\([^)]*\)", " ", tok).strip()
        if base != tok:  # balanced parentheses were removed; an unclosed "(" is dropped below (never recurse on it)
            inner = re.findall(r"\(([^)]*)\)", tok)
            out = _classify(base) if base else []
            for part in inner:
                out += _classify(part)
            return out
    tok = tok.replace("(", " ").replace(")", " ").strip()
    if f in _NATIONWIDE:
        return [_Item("nationwide", tok, countries=(_NATIONWIDE[f],))]
    if f in ("dach", "dach region", "dach raum"):
        return [_Item("country", tok, countries=DACH)]
    pc = None
    if m := _POST_FIRST.match(tok):
        pc, tok = m[1], m[2].strip()
        f = fold(tok)
    elif m := _POST_LAST.match(tok):
        pc, tok = m[2], m[1].strip()
        f = fold(tok)
    if f in _COUNTRY_INDEX and not pc:
        return [_Item("country", tok, countries=(_COUNTRY_INDEX[f],))]
    cands = _lookup_city(tok)
    regions = tuple(sorted(_REGION_INDEX.get(f, ())))
    if forced and regions:
        return [_Item("region", tok, regions=regions)]
    if cands:
        return [_Item("city", tok, candidates=tuple(cands), regions=regions, postcode=pc)]
    if regions:
        return [_Item("region", tok, regions=regions)]
    if force_region:
        return []
    if re.fullmatch(r"[^\W\d_][\w .'\-/ß]{1,40}", tok, re.U) and not re.search(
        r"remote|home|hybrid|office|worldwide|weltweit|europe|europa|\bem?ea\b|team|bereich", tok, re.I
    ):
        return [_Item("place", tok, postcode=pc)]
    return []


# --- resolution ----------------------------------------------------------------------------------


def _place_from(c: Candidate, *, pc: str | None, conf: float, source: str) -> Place:
    return Place(c.country, c.region, c.city, pc, confidence=conf, source=source)


def _resolve_group(
    head: _Item, regions: set[str], countries: set[str], context_country: str | None
) -> tuple[Place | None, dict | None]:
    # Region/country qualifiers narrow candidates; conflicting qualifiers make the group unresolved.
    if regions:
        countries = {r.split("-")[0] for r in regions} if not countries else countries & {r.split("-")[0] for r in regions}
    if head.kind == "city":
        cands = list(head.candidates)
        narrowed = cands
        if regions:
            narrowed = [c for c in cands if c.region in regions]
        elif countries:
            narrowed = [c for c in cands if c.country in countries]
        qualified = bool(regions or countries)
        if len(narrowed) == 1:
            return _place_from(narrowed[0], pc=head.postcode, conf=0.95 if qualified else 0.9, source="gazetteer"), None
        if len(narrowed) > 1:
            if context_country and not qualified:
                by_ctx = [c for c in narrowed if c.country == context_country]
                if len(by_ctx) == 1:
                    return _place_from(by_ctx[0], pc=head.postcode, conf=0.7, source="context"), None
            dom = DOMINANT.get(fold(head.text))
            if dom and not qualified:
                pick = next((c for c in narrowed if c.city == dom), None)
                if pick:
                    return _place_from(pick, pc=head.postcode, conf=0.6, source="gazetteer"), None
            return None, {
                "text": head.text,
                "reason": "ambiguous",
                "candidates": [{"country": c.country, "region": c.region, "city": c.city} for c in narrowed][:5],
            }
        # qualifiers contradict the gazetteer: the source's own statement wins, at lower confidence
        country = sorted(countries)[0] if len(countries) == 1 else None
        if country:
            region = sorted(regions)[0] if len(regions) == 1 else None
            return Place(country, region, head.text, head.postcode, confidence=0.6, source="text"), None
        return None, {"text": head.text, "reason": "conflicting_qualifiers"}
    # unknown place name: needs a stated country or region
    country = sorted(countries)[0] if len(countries) == 1 else None
    if country:
        region = sorted(regions)[0] if len(regions) == 1 else None
        return Place(country, region, head.text, head.postcode, confidence=0.7, source="text"), None
    return None, {"text": head.text, "reason": "country_unknown", **({"postcode": head.postcode} if head.postcode else {})}


def resolve_location(
    raw: str | None,
    structured: list[dict] | None = None,
    *,
    context_country: str | None = None,
) -> GeoResult:
    """Resolve free text and/or structured address dicts into places.

    ``structured`` items look like ``{"city","postcode","region","country"}`` (JSON-LD, ATS).
    ``context_country`` (the employer's country) only breaks ties between gazetteer candidates.
    """
    result = GeoResult()
    seen: set = set()

    def add(p: Place) -> None:
        if p.key not in seen:
            seen.add(p.key)
            result.places.append(p)

    structured = list(structured or [])
    if raw and structured and not any((s.get("city") or "").strip() for s in structured):
        # only a country/region was given: it qualifies the free text instead of replacing it
        quals = [(s.get("region") or "").strip() for s in structured] + [(s.get("country") or "").strip() for s in structured]
        extra = ", ".join(q for q in quals if q)
        if extra:
            raw = f"{raw}, {extra}"
            structured = []
    for s in structured:
        _resolve_structured(s, add, result, context_country)

    if raw and not result.places:
        _resolve_text(raw, add, result, context_country)
    elif raw and not structured:
        _resolve_text(raw, add, result, context_country)

    if result.places and not result.unresolved:
        result.status = "resolved"
    elif result.places:
        result.status = "partial"
    elif any(u.get("reason") == "ambiguous" for u in result.unresolved):
        result.status = "ambiguous"
    elif result.unresolved:
        result.status = "partial"
    else:
        result.status = "unknown"
    return result


def _resolve_structured(s: dict, add, result: GeoResult, context_country: str | None) -> None:
    city = (s.get("city") or "").strip()
    pc = (s.get("postcode") or "").strip() or None
    reg_txt = (s.get("region") or "").strip()
    ctry_txt = (s.get("country") or "").strip()
    country = _COUNTRY_INDEX.get(fold(ctry_txt)) if ctry_txt else None
    regions: set[str] = set()
    if reg_txt:
        regions = set(_REGION_INDEX.get(fold(reg_txt), ()))
        if not regions and country and re.fullmatch(r"[A-Za-z]{2}|\d", reg_txt):
            code = f"{country}-{reg_txt.upper()}"
            if code in REGIONS:
                regions = {code}
        if country:
            regions = {r for r in regions if r.startswith(country + "-")}
    countries = {country} if country else set()
    if city:
        items = _classify(city)
        head = next((i for i in items if i.kind in ("city", "place")), None)
        if head is None and items and items[0].kind in ("region", "country"):
            head = None
        if head is not None:
            head.postcode = head.postcode or pc
            place, bad = _resolve_group(head, regions, countries, context_country)
            if place:
                if place.source == "gazetteer":
                    place.source = "structured"
                add(place)
            if bad:
                result.unresolved.append(bad)
            return
        if city and not items:
            result.unresolved.append({"text": city[:80], "reason": "country_unknown"})
            return
    # no usable city: region or country level
    if regions:
        for r in sorted(regions):
            add(Place(r.split("-")[0], r, None, pc, confidence=0.9, source="structured"))
    elif country:
        add(Place(country, None, None, pc, confidence=0.9, source="structured"))
    elif ctry_txt:
        result.unresolved.append({"text": ctry_txt[:80], "reason": "country_unknown"})


def _resolve_text(raw: str, add, result: GeoResult, context_country: str | None) -> None:
    items: list[_Item] = []
    for tok in _SPLIT.split(raw[:300]):
        items += _classify(tok)
    # group: a city/place head followed by qualifiers (region/country) until the next head
    groups: list[tuple[_Item, set[str], set[str]]] = []
    lead_regions: set[str] = set()
    lead_countries: set[str] = set()
    for it in items:
        if it.kind in ("city", "place"):
            groups.append((it, set(), set()))
        elif it.kind in ("region", "country", "nationwide"):
            r = set(it.regions)
            c = set(it.countries)
            if groups:
                head, hr, hc = groups[-1]
                hr |= r
                hc |= c
            else:
                lead_regions |= r
                lead_countries |= c
    for head, hr, hc in groups:
        # qualifiers that follow a head belong to it; leading qualifiers are a default
        regions = hr or set(lead_regions)
        countries = hc or set(lead_countries)
        # a "qualifier" that is really the next sentence-level region of another city is rare; ignore
        place, bad = _resolve_group(head, regions, countries, context_country)
        if place:
            add(place)
        if bad:
            result.unresolved.append(bad)
    if not groups:
        # region / country level locations ("Sachsen", "Deutschland", "DACH")
        if lead_regions:
            for r in sorted(lead_regions):
                add(Place(r.split("-")[0], r, None, None, confidence=0.85, source="text"))
        elif lead_countries:
            for c in sorted(lead_countries):
                add(Place(c, None, None, None, confidence=0.85, source="text"))


# --- remote eligibility --------------------------------------------------------------------------

_CTRY_WORDS = (
    r"germany|deutschland|german|österreich|oesterreich|austria|schweiz|switzerland|suisse|svizzera|"
    r"dach|eu\b|europe|europa|europaweit|eu-weit|worldwide|weltweit|global|anywhere|deutschlandweit|bundesweit"
)
_REMOTE_KW = r"remote|home[- ]?office|mobiles? arbeiten|telearbeit|telecommut\w*|télétravail|smart working|lavoro da remoto"
_REMOTE_NEAR = re.compile(
    rf"(?:{_REMOTE_KW})[^.\n;]{{0,45}}?\b(?P<a>{_CTRY_WORDS})|\b(?P<b>{_CTRY_WORDS})[^.\n;]{{0,25}}?(?:{_REMOTE_KW})",
    re.I,
)
_REMOTE_WIDE = re.compile(
    r"\b(work from anywhere|remote[- ](?:first )?(?:worldwide|global\w*)|weltweit remote|remote weltweit|"
    r"fully remote (?:and )?worldwide|worldwide remote)\b",
    re.I,
)


@dataclass
class RemoteScope:
    scope: str = "unknown"  # unknown | worldwide | europe | eu | dach | countries
    countries: list[str] = field(default_factory=list)  # DACH countries a candidate may live in


def remote_scope(texts: list[str | None], work_model: str, places: list[Place]) -> RemoteScope:
    """Where may a remote/hybrid employee live? Only stated scopes count; "Remote" alone is unknown."""
    if work_model not in ("REMOTE", "HYBRID"):
        return RemoteScope()
    blob = " \n ".join(t for t in texts if t)[:8000]
    if _REMOTE_WIDE.search(blob):
        return RemoteScope("worldwide", [])
    found: set[str] = set()
    scope = None
    for m in _REMOTE_NEAR.finditer(blob):
        w = fold(m.group("a") or m.group("b") or "")
        if w in ("worldwide", "weltweit", "global", "anywhere"):
            scope = "worldwide"
        elif w == "dach":
            found |= set(DACH)
            scope = scope or "dach"
        elif w in ("eu", "eu weit"):
            found |= set(EU_IN_DACH)
            scope = scope or "eu"
        elif w in ("europe", "europa", "europaweit"):
            found |= set(DACH)
            scope = scope or "europe"
        elif code := _COUNTRY_INDEX.get(w):
            found.add(code)
            scope = "countries"
        elif w in ("deutschlandweit", "bundesweit"):
            found.add("DE")
            scope = "countries"
        elif w == "german":
            continue
    if scope == "worldwide":
        return RemoteScope("worldwide", [])
    if found:
        dach = sorted(found & set(DACH))
        if scope == "countries" and len(found) > 0:
            return RemoteScope("countries", sorted(found))
        return RemoteScope(scope or "countries", dach)
    # "Remote" with a country-level location ("Germany (Remote)") counts as that country
    country_only = [p for p in places if p.city is None and p.region is None and p.country]
    if country_only and work_model == "REMOTE" and len(country_only) == len(places):
        return RemoteScope("countries", sorted({p.country for p in country_only}))
    return RemoteScope()


# --- keys / ISO helpers --------------------------------------------------------------------------


def location_key(country: str, region: str | None, city: str | None) -> str:
    return f"{country}|{region or ''}|{fold(city or '')}"


def country_of_region(code: str | None) -> str | None:
    return code.split("-")[0] if code and "-" in code else None


def region_country_valid(code: str) -> bool:
    return code in REGIONS
