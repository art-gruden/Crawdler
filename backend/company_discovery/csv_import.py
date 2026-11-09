"""CSV import from company directories.

Profiles:
  generic           – any directory export with at least a company-name column
  standort_sachsen  – export of the WFS "Firmendatenbank Sachsen" (firmen.standort-sachsen.de);
                      maps its technology categories MICRO! / MASCHINE! / LIFE SCIENCES! and NACE codes

Encoding (UTF-8 with or without BOM, Windows-1252) and delimiter (, ; tab |) are detected.
Header names are matched case- and accent-insensitively against the aliases below.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterator
from dataclasses import dataclass

from .industries import classify_nace, classify_text, classify_wfs
from .regions import find_region_by_name, fold, parent_state
from .service import Candidate

ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "company", "company name", "firma", "firmenname", "unternehmen", "unternehmensname", "firmierung"),
    "website": (
        "website",
        "url",
        "homepage",
        "webseite",
        "internet",
        "web",
        "internetadresse",
        "www",
        "domain",
        "contact website",
        "webadresse",
    ),
    "street": ("street", "strasse", "str", "adresse", "address", "anschrift", "strasse hausnummer"),
    "housenumber": ("housenumber", "hausnummer", "hnr", "nr"),
    "postcode": ("postcode", "plz", "postleitzahl", "zip", "postal code"),
    "city": ("city", "ort", "stadt", "gemeinde", "town", "sitz"),
    "district": ("district", "landkreis", "kreis", "region", "kreisfreie stadt landkreis", "kreisname"),
    "state": ("state", "bundesland", "land"),
    "industry": ("industry", "branche", "branchen", "sector", "wirtschaftszweig text", "taetigkeit", "tatigkeit"),
    "category": (
        "category",
        "kategorie",
        "technologie",
        "technologien",
        "technologiefeld",
        "technologiefelder",
        "zukunftsfeld",
        "zukunftsfelder",
        "cluster",
    ),
    "nace": ("nace", "wz", "wz 2008", "wz2008", "nace code", "wirtschaftszweig", "branchenschluessel"),
    "lat": ("lat", "latitude", "breite"),
    "lon": ("lon", "lng", "longitude", "laenge"),
    "source_id": ("source id", "id", "firmen id", "firmenid", "datensatz id", "record id"),
    "source_url": ("source url", "profil url", "detail url", "eintrag url", "link"),
    "employees": ("employees", "mitarbeiter", "beschaftigte", "beschaeftigte", "mitarbeiterzahl"),
}

PROFILES = {
    "generic": {
        "label": "Generic company list",
        "source": "csv:generic",
        "license": None,
        "attribution": None,
        "source_url": None,
    },
    "standort_sachsen": {
        "label": "Firmendatenbank Sachsen (WFS) export",
        "source": "csv:standort_sachsen",
        "license": "see-provider-terms",
        "attribution": "Quelle: Wirtschaftsförderung Sachsen GmbH, Firmendatenbank Sachsen",
        "source_url": "https://firmen.standort-sachsen.de/company/de/",
    },
}


class CsvFormatError(ValueError):
    pass


@dataclass
class RowError:
    line: int
    message: str


def _norm_header(h: str) -> str:
    return fold(h.replace("_", " ").replace("-", " "))


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def sniff_dialect(sample: str) -> type[csv.Dialect] | csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        first = sample.splitlines()[0] if sample else ""
        counts = {d: first.count(d) for d in ",;\t|"}
        delim = max(counts, key=counts.get) if any(counts.values()) else ","

        class _D(csv.excel):
            delimiter = delim

        return _D


def map_columns(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    normalized = [_norm_header(h) for h in headers]
    for field, aliases in ALIASES.items():
        alias_set = {fold(a) for a in aliases}
        for idx, h in enumerate(normalized):
            if h in alias_set and field not in mapping:
                mapping[field] = idx
    return mapping


def inspect_header(raw: bytes) -> tuple[list[str], dict[str, int]]:
    """Validate quickly before a job is queued. Raises CsvFormatError."""
    text = decode(raw[: 64 * 1024])
    if not text.strip():
        raise CsvFormatError("The file is empty")
    dialect = sniff_dialect(text[: 16 * 1024])
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        headers = next(reader)
    except StopIteration as exc:
        raise CsvFormatError("No header row") from exc
    mapping = map_columns(headers)
    if "name" not in mapping:
        raise CsvFormatError(
            "No company-name column found. Expected one of: " + ", ".join(ALIASES["name"]) + f". Got: {headers[:12]}"
        )
    return headers, mapping


def _cell(row: list[str], mapping: dict[str, int], field: str) -> str | None:
    idx = mapping.get(field)
    if idx is None or idx >= len(row):
        return None
    value = row[idx].strip()
    return value or None


def _float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def parse_rows(
    raw: bytes,
    *,
    profile: str,
    max_rows: int,
    source: str,
    license: str | None,
    attribution: str | None,
    source_url: str | None,
    use_source_ids: bool = True,
) -> Iterator[Candidate | RowError]:
    """Yield candidates and row errors. ``use_source_ids`` should only be true when the
    source name is specific to one directory, otherwise IDs from different files collide."""
    if profile not in PROFILES:
        raise CsvFormatError(f"unknown profile {profile!r}")
    text = decode(raw)
    dialect = sniff_dialect(text[: 16 * 1024])
    reader = csv.reader(io.StringIO(text), dialect)
    headers = next(reader, None)
    if headers is None:
        raise CsvFormatError("No header row")
    mapping = map_columns(headers)
    if "name" not in mapping:
        raise CsvFormatError("No company-name column found")

    count = 0
    for row in reader:
        line = reader.line_num
        if not any(c.strip() for c in row):
            continue
        count += 1
        if count > max_rows:
            yield RowError(line, f"row limit of {max_rows} reached; remaining rows ignored")
            return
        name = _cell(row, mapping, "name")
        if not name:
            yield RowError(line, "missing company name")
            continue
        if len(name) > 300:
            yield RowError(line, "company name longer than 300 characters")
            continue

        street = _cell(row, mapping, "street")
        hnr = _cell(row, mapping, "housenumber")
        if street and hnr and hnr not in street:
            street = f"{street} {hnr}"
        postcode = _cell(row, mapping, "postcode")
        if postcode:
            m = re.search(r"\b\d{5}\b", postcode)
            postcode = m.group(0) if m else None

        city = _cell(row, mapping, "city")
        region = find_region_by_name(_cell(row, mapping, "district")) or find_region_by_name(city)
        if region is not None and region.kind == "state":
            region = None
        state_code = None
        if region is not None:
            state_code = parent_state(region).code
        else:
            state = find_region_by_name(_cell(row, mapping, "state"))
            state_code = state.code if state is not None and state.kind == "state" else None
        if region is None and city and (cr := find_region_by_name(city)) and cr.kind == "kreisfreie_stadt":
            region = cr

        category = _cell(row, mapping, "category")
        nace = _cell(row, mapping, "nace")
        industry_text = _cell(row, mapping, "industry")
        industries: list[str] = []
        data: dict[str, object] = {}
        if profile == "standort_sachsen" and category:
            wfs_ind, cats = classify_wfs(category)
            industries += wfs_ind
            data["wfs_categories"] = cats
        elif category:
            industries += classify_text(category)
            data["category"] = category
        industries += classify_nace(nace)
        industries += classify_text(industry_text)
        if nace:
            data["nace"] = nace
        if industry_text:
            data["industry"] = industry_text[:300]
        employees = _cell(row, mapping, "employees")
        if employees:
            data["employees"] = employees[:40]
        if profile == "standort_sachsen" and state_code is None:
            state_code = "DE-SN"

        yield Candidate(
            source=source,
            source_record_id=(f"id:{sid}" if use_source_ids and (sid := _cell(row, mapping, "source_id")) else None),
            source_url=_cell(row, mapping, "source_url") or source_url,
            name=name,
            websites=[_cell(row, mapping, "website")],
            street=street,
            postcode=postcode,
            city=city,
            district_key=region.code if region else None,
            district_name=region.name if region else None,
            state_code=state_code,
            lat=_float(_cell(row, mapping, "lat")),
            lon=_float(_cell(row, mapping, "lon")),
            industries=list(dict.fromkeys(industries)),
            data=data,
            license=license,
            attribution=attribution,
        )
