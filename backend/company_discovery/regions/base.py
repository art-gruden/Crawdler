"""Region model. A federal state is a tree: state → Kreise (Landkreise and kreisfreie Städte).

OpenStreetMap discovery always queries leaf regions (Kreise): this keeps every
Overpass request small and tells us the Kreis of each result without extra lookups.
Adding another federal state means adding one module like ``sachsen.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Region:
    code: str  # Kreisschlüssel (5 digits) for Kreise, ISO 3166-2 for states
    name: str
    kind: str  # state | kreisfreie_stadt | landkreis
    state_code: str  # ISO 3166-2 of the federal state, e.g. DE-SN
    seat: str | None = None  # Kreisstadt / Verwaltungssitz
    children: tuple[Region, ...] = field(default_factory=tuple)
    aliases: tuple[str, ...] = ()

    @property
    def is_leaf(self) -> bool:
        return not self.children

    def overpass_area(self, set_name: str = "a") -> str:
        """Overpass QL that stores the region's area in ``.{set_name}``.

        Kreis boundaries in Germany carry ``de:regionalschluessel`` and
        ``de:amtlicher_gemeindeschluessel`` (5, 8 or 12 digits). Matching the key as a
        prefix on admin_level=6 works with every variant.
        """
        if self.kind == "state":
            return f'area["ISO3166-2"="{self.code}"]["admin_level"="4"]->.{set_name};'
        admin = '["boundary"="administrative"]["admin_level"="6"]'
        return (
            "(\n"
            f'  area{admin}["de:regionalschluessel"~"^{self.code}"];\n'
            f'  area{admin}["de:amtlicher_gemeindeschluessel"~"^{self.code}"];\n'
            f")->.{set_name};"
        )

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "kind": self.kind,
            "state_code": self.state_code,
            "seat": self.seat,
            "children": [c.to_dict() for c in self.children],
        }
