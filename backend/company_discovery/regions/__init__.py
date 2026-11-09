"""Region registry. Register further federal states in ``STATES``."""

from __future__ import annotations

import re
import unicodedata

from .base import Region
from .sachsen import SACHSEN

STATES: tuple[Region, ...] = (SACHSEN,)

_BY_CODE: dict[str, Region] = {}
for _state in STATES:
    _BY_CODE[_state.code] = _state
    for _child in _state.children:
        _BY_CODE[_child.code] = _child


def fold(value: str) -> str:
    value = value.lower().replace("ß", "ss")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


_BY_NAME: dict[str, Region] = {}
for _r in _BY_CODE.values():
    for _n in (_r.name, *_r.aliases):
        _BY_NAME.setdefault(fold(_n), _r)


def get_region(code: str) -> Region:
    try:
        return _BY_CODE[code]
    except KeyError:
        raise KeyError(f"unknown region {code!r}") from None


def find_region_by_name(name: str | None) -> Region | None:
    if not name:
        return None
    key = fold(name)
    if key in _BY_NAME:
        return _BY_NAME[key]
    stripped = re.sub(r"^(landkreis|kreis|lk|kreisfreie stadt|stadt)\s+", "", key)
    return _BY_NAME.get(stripped) or _BY_NAME.get("landkreis " + stripped)


def expand_leaves(codes: list[str]) -> list[Region]:
    """State codes expand to their Kreise; duplicates are removed, order kept."""
    out: dict[str, Region] = {}
    for code in codes:
        region = get_region(code)
        for leaf in region.children if region.children else (region,):
            out.setdefault(leaf.code, leaf)
    return list(out.values())


def parent_state(region: Region) -> Region:
    return _BY_CODE[region.state_code]


__all__ = ["Region", "STATES", "get_region", "find_region_by_name", "expand_leaves", "parent_state", "fold"]
