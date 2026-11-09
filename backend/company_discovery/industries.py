"""Industry groups and how they map to OSM tags, NACE (WZ 2008) codes and directory categories.

Each OSM selector is a list of conditions on one element. The same definition is
rendered to Overpass QL and evaluated in Python to label results, so query and
classification never drift apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# (key, op, value): op is "=", "~" (regex, case-sensitive), "~i" (regex, case-insensitive) or "*" (key exists)
Cond = tuple[str, str, str]


@dataclass(frozen=True)
class Industry:
    id: str
    label: str
    description: str
    selectors: tuple[tuple[Cond, ...], ...]
    default: bool = True


INDUSTRIES: tuple[Industry, ...] = (
    Industry(
        "it",
        "IT & Software",
        "IT service providers, software houses, telecommunications (office=it/software/telecommunication)",
        (
            (("office", "~", "^(it|software|telecommunication)$"),),
            (("office", "=", "company"), ("company", "~i", "software|(^|;)it($|;)|information|telecom|computer")),
            (("office", "=", "company"), ("industry", "~i", "software|(^|;)it($|;)|information|telecom")),
        ),
    ),
    Industry(
        "engineering",
        "Engineering",
        "Engineering offices and technical consultancies (office=engineer)",
        ((("office", "~", "^(engineer|engineering)$"),),),
    ),
    Industry(
        "industrial",
        "Industry & Manufacturing",
        "Factories, works and industrial sites with an own website (man_made=works, industrial=*)",
        (
            (("man_made", "=", "works"),),
            (("industrial", "*", ""),),
            (
                ("office", "=", "company"),
                ("industry", "~i", "manufactur|machin|automotive|electronic|semiconductor|metal"),
            ),
        ),
    ),
    Industry(
        "technology",
        "Research & Technology",
        "Research institutes and R&D companies (office=research, amenity=research_institute)",
        (
            (("office", "=", "research"),),
            (("amenity", "=", "research_institute"),),
        ),
    ),
    Industry(
        "life_sciences",
        "Life Sciences",
        "Biotech, pharma and medical technology (industrial/industry tags)",
        (
            (("industrial", "~i", "pharma|biotech|chemi|medical"),),
            (("office", "=", "company"), ("industry", "~i", "pharma|biotech|medical|life.?science")),
        ),
        default=False,
    ),
    Industry(
        "company_other",
        "Other companies",
        "Any office=company with a website. Broad: many non-technical employers.",
        ((("office", "=", "company"),),),
        default=False,
    ),
)

BY_ID = {i.id: i for i in INDUSTRIES}
DEFAULT_IDS = [i.id for i in INDUSTRIES if i.default]


def get_industries(ids: list[str] | None) -> list[Industry]:
    ids = ids or DEFAULT_IDS
    unknown = [i for i in ids if i not in BY_ID]
    if unknown:
        raise KeyError(f"unknown industries: {', '.join(unknown)}")
    return [BY_ID[i] for i in dict.fromkeys(ids)]


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_condition(cond: Cond) -> str:
    key, op, value = cond
    if op == "*":
        return f'["{_q(key)}"]'
    if op == "=":
        return f'["{_q(key)}"="{_q(value)}"]'
    if op == "~":
        return f'["{_q(key)}"~"{_q(value)}"]'
    if op == "~i":
        return f'["{_q(key)}"~"{_q(value)}",i]'
    raise ValueError(op)


def matches(tags: dict[str, str], selector: tuple[Cond, ...]) -> bool:
    for key, op, value in selector:
        if key not in tags:
            return False
        v = tags[key]
        if op == "=" and v != value:
            return False
        if op == "~" and not re.search(value, v):
            return False
        if op == "~i" and not re.search(value, v, re.I):
            return False
    return True


def classify_tags(tags: dict[str, str], candidates: list[Industry] | None = None) -> list[str]:
    result = []
    for ind in candidates or INDUSTRIES:
        if any(matches(tags, sel) for sel in ind.selectors):
            result.append(ind.id)
    return result


# --- NACE Rev. 2 / WZ 2008 division → industries (for CSV imports) -------------------
NACE_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("58.2", ("it",)),  # software publishing
    ("61", ("it",)),  # telecommunications
    ("62", ("it",)),  # computer programming, consultancy
    ("63", ("it",)),  # information services, data processing, hosting
    ("26", ("industrial", "technology")),  # computer, electronic and optical products
    ("27", ("industrial",)),  # electrical equipment
    ("28", ("industrial", "engineering")),  # machinery
    ("29", ("industrial",)),  # motor vehicles
    ("30", ("industrial",)),  # other transport equipment
    ("33", ("industrial",)),  # repair and installation of machinery
    ("20", ("industrial",)),  # chemicals
    ("21", ("life_sciences",)),  # pharmaceuticals
    ("32.5", ("life_sciences",)),  # medical and dental instruments
    ("71", ("engineering",)),  # architectural and engineering activities
    ("72", ("technology",)),  # scientific research and development
)


def classify_nace(code: str | None) -> list[str]:
    if not code:
        return []
    out: list[str] = []
    for part in re.split(r"[;,|/\s]+", code):
        part = part.strip().upper().removeprefix("WZ").removeprefix("NACE").strip(" :-")
        part = re.sub(r"^[A-U](?=\d)", "", part)  # section letter, e.g. "J62.01"
        if not re.match(r"^\d{2}", part):
            continue
        for prefix, industries in NACE_PREFIXES:
            if part.startswith(prefix):
                out.extend(industries)
    return list(dict.fromkeys(out))


# --- Technology categories of the WFS "Firmendatenbank Sachsen" ----------------------
# Only the categories whose content WFS publishes are mapped. Others are kept as tags.
WFS_CATEGORIES: dict[str, tuple[str, ...]] = {
    "MICRO!": ("it", "technology"),  # microelectronics, nanotech, electronics, software, media
    "MASCHINE!": ("industrial", "engineering"),  # mechanical engineering, technical textiles, sensors
    "LIFE SCIENCES!": ("life_sciences", "technology"),  # biotech, medical technology, pharma
}


def classify_wfs(value: str | None) -> tuple[list[str], list[str]]:
    """Return (industries, raw categories) for a directory category cell."""
    if not value:
        return [], []
    cats = [c.strip() for c in re.split(r"[;,|\n]+", value) if c.strip()]
    industries: list[str] = []
    for cat in cats:
        key = cat.upper().replace("  ", " ")
        if not key.endswith("!"):
            key += "!"
        industries.extend(WFS_CATEGORIES.get(key, ()))
    return list(dict.fromkeys(industries)), cats


KEYWORD_INDUSTRIES: tuple[tuple[str, str], ...] = (
    (r"\b(it|edv|software|informatik|it-dienstleist\w*|it-service\w*|rechenzentrum|cloud|telekommunikation)\b", "it"),
    (r"\b(ingenieur\w*|engineering|konstruktion)\b", "engineering"),
    (
        r"\b(maschinenbau|fertigung|produktion|industrie|automotive|halbleiter|mikroelektronik|elektronik)\b",
        "industrial",
    ),
    (r"\b(forschung|research|institut|technologie\w*)\b", "technology"),
    (r"\b(biotech\w*|pharma\w*|medizintechnik|life sciences)\b", "life_sciences"),
)


def classify_text(value: str | None) -> list[str]:
    if not value:
        return []
    low = value.lower()
    return list(dict.fromkeys(ind for pattern, ind in KEYWORD_INDUSTRIES if re.search(pattern, low)))
