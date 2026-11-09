import pytest

from company_discovery.csv_import import CsvFormatError, RowError, inspect_header, parse_rows

from .conftest import FIXTURES


def _parse(raw, profile="generic", **kw):
    defaults = dict(profile=profile, max_rows=1000, source="csv:test", license=None, attribution=None, source_url=None)
    defaults.update(kw)
    return list(parse_rows(raw, **defaults))


def test_generic_cp1252_semicolon():
    raw = (FIXTURES / "generic_semicolon_cp1252.csv").read_bytes()
    headers, mapping = inspect_header(raw)
    assert headers[0] == "Firma" and {"name", "website", "postcode", "city", "district", "industry", "nace"} <= set(
        mapping
    )
    items = _parse(raw)
    cands = [i for i in items if not isinstance(i, RowError)]
    errors = [i for i in items if isinstance(i, RowError)]
    assert len(cands) == 4 and len(errors) == 1
    assert errors[0].message == "missing company name" and errors[0].line == 5
    erz = cands[0]
    assert erz.name == "Erzgebirge Automation GmbH"
    assert erz.district_key == "14521" and erz.state_code == "DE-SN"
    assert erz.industries == ["industrial", "engineering"]
    lausitz = cands[1]
    assert lausitz.industries == ["it"] and lausitz.district_key == "14625"
    baeckerei = cands[2]
    assert baeckerei.name == "Bäckerei Sonnenschein"  # cp1252 decoded
    assert baeckerei.websites == [None] and baeckerei.industries == []
    pleisse = cands[3]
    assert pleisse.district_key == "14713"  # derived from city (kreisfreie Stadt)
    assert pleisse.source_record_id is None  # generic profile: row hash is used


def test_standort_sachsen_profile():
    raw = (FIXTURES / "standort_sachsen_sample.csv").read_bytes()
    cands = _parse(raw, profile="standort_sachsen", source="csv:standort_sachsen", use_source_ids=True)
    assert [c.source_record_id for c in cands] == ["id:SN-1001", "id:SN-1002", "id:SN-1003", "id:SN-1004"]
    vogt = cands[1]
    assert vogt.district_key == "14523" and vogt.state_code == "DE-SN"
    assert vogt.industries == ["industrial", "engineering", "it", "technology"]
    assert vogt.data["wfs_categories"] == ["MASCHINE!", "MICRO!"]
    assert vogt.data["employees"] == "100-249"
    assert cands[2].industries[:2] == ["life_sciences", "technology"]
    assert cands[0].street == "Musterstraße 12"


def test_no_name_column():
    with pytest.raises(CsvFormatError, match="company-name column"):
        inspect_header(b"foo,bar\n1,2\n")


def test_empty_file():
    with pytest.raises(CsvFormatError):
        inspect_header(b"   ")


def test_row_limit():
    raw = b"name,website\n" + b"".join(f"Firma {i},firma{i}.de\n".encode() for i in range(10))
    items = _parse(raw, max_rows=3)
    assert sum(not isinstance(i, RowError) for i in items) == 3
    assert isinstance(items[-1], RowError) and "row limit" in items[-1].message


def test_tab_delimited_utf8_bom():
    raw = "﻿Unternehmen\tHomepage\tPLZ\nTest GmbH\twww.test-gmbh-beispiel.de\t01067 Dresden\n".encode()
    (c,) = _parse(raw)
    assert c.name == "Test GmbH" and c.postcode == "01067"
