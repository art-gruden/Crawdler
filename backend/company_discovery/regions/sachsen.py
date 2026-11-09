"""Freistaat Sachsen (DE-SN): 3 kreisfreie Städte and 10 Landkreise since the 2008 district reform.

Kreisschlüssel per Statistisches Bundesamt / OSM boundary tags.
"""

from .base import Region

STATE = "DE-SN"


def _k(code: str, name: str, kind: str, seat: str, *aliases: str) -> Region:
    return Region(code=code, name=name, kind=kind, state_code=STATE, seat=seat, aliases=aliases)


SACHSEN = Region(
    code=STATE,
    name="Sachsen",
    kind="state",
    state_code=STATE,
    seat="Dresden",
    aliases=("Saxony", "Freistaat Sachsen", "SN"),
    children=(
        _k("14612", "Dresden", "kreisfreie_stadt", "Dresden", "Stadt Dresden", "Landeshauptstadt Dresden"),
        _k("14713", "Leipzig", "kreisfreie_stadt", "Leipzig", "Stadt Leipzig"),
        _k("14511", "Chemnitz", "kreisfreie_stadt", "Chemnitz", "Stadt Chemnitz"),
        _k("14625", "Landkreis Bautzen", "landkreis", "Bautzen", "Bautzen"),
        _k("14521", "Erzgebirgskreis", "landkreis", "Annaberg-Buchholz", "Erzgebirge"),
        _k("14626", "Landkreis Görlitz", "landkreis", "Görlitz", "Görlitz"),
        _k("14729", "Landkreis Leipzig", "landkreis", "Borna", "Leipziger Land"),
        _k("14627", "Landkreis Meißen", "landkreis", "Meißen", "Meißen", "Meissen"),
        _k("14522", "Landkreis Mittelsachsen", "landkreis", "Freiberg", "Mittelsachsen"),
        _k("14730", "Landkreis Nordsachsen", "landkreis", "Torgau", "Nordsachsen"),
        _k(
            "14628",
            "Landkreis Sächsische Schweiz-Osterzgebirge",
            "landkreis",
            "Pirna",
            "Sächsische Schweiz-Osterzgebirge",
            "SSO",
        ),
        _k("14523", "Vogtlandkreis", "landkreis", "Plauen", "Vogtland"),
        _k("14524", "Landkreis Zwickau", "landkreis", "Zwickau", "Zwickau"),
    ),
)
