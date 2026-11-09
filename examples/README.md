# Example import files

All companies and domains here are fictional and exist only to show the format.

- `companies_generic.csv`: profile **generic**, semicolon-separated, German headers. Columns are matched by name; order does not matter.
- `standort_sachsen_template.csv`: profile **standort_sachsen**, the layout to ask WFS (info@wfs.saxony.de) for when requesting an export of the Firmendatenbank Sachsen. `Technologiefelder` may contain several fields separated by `,` or `;`: `MICRO!` → IT/technology, `MASCHINE!` → industry/engineering, `LIFE SCIENCES!` → life sciences/technology. `WZ 2008` codes are mapped too (62/63 → IT, 28 → machinery, 71 → engineering, 72 → research …). `Firmen-ID` keeps re-imports duplicate-free.

Recognised header names (case and umlauts ignored): name/Firma/Firmenname/Unternehmen, website/URL/Homepage/Webseite/Internet, Straße/Adresse + Hausnummer, PLZ, Ort/Stadt, Landkreis/Kreis, Bundesland, Branche, Kategorie/Technologiefelder, WZ/NACE, lat/lon, ID/Firmen-ID, Mitarbeiter.
