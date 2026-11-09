import pytest

from gcc_core.urls import UrlRejected, first_valid_website, normalize_website


@pytest.mark.parametrize(
    "raw, url, domain",
    [
        ("https://www.example-firma.de/", "https://www.example-firma.de/", "example-firma.de"),
        ("WWW.Example-Firma.DE", "https://www.example-firma.de/", "example-firma.de"),
        (
            "http://example-firma.de/karriere?utm_source=osm&lang=de#jobs",
            "http://example-firma.de/karriere?lang=de",
            "example-firma.de",
        ),
        ("//cdn.example-firma.de/x", "https://cdn.example-firma.de/x", "example-firma.de"),
        ("karriere.example-firma.de", "https://karriere.example-firma.de/", "example-firma.de"),
        ("müller-präzision.de", "https://xn--mller-przision-dib09a.de/", "xn--mller-przision-dib09a.de"),
        ("https://www.firma.co.uk", "https://www.firma.co.uk/", "firma.co.uk"),
        ("https://mein-betrieb.jimdosite.com/", "https://mein-betrieb.jimdosite.com/", "mein-betrieb.jimdosite.com"),
        ("https://mein-betrieb.wixsite.com/home", "https://mein-betrieb.wixsite.com/home", "mein-betrieb.wixsite.com"),
        ("<https://example-firma.de>", "https://example-firma.de/", "example-firma.de"),
        ("https://example-firma.de:443/", "https://example-firma.de/", "example-firma.de"),
        ("https://example-firma.de//a//b", "https://example-firma.de/a/b", "example-firma.de"),
    ],
)
def test_normalize(raw, url, domain):
    n = normalize_website(raw)
    assert n.url == url
    assert n.domain == domain


@pytest.mark.parametrize(
    "raw, reason",
    [
        ("", "empty"),
        ("mailto:info@firma.de", "unsupported_scheme"),
        ("ftp://firma.de", "unsupported_scheme"),
        ("javascript:alert(1)", "unsupported_scheme"),
        ("https://user:pw@firma.de", "credentials_in_url"),
        ("http://127.0.0.1/", "ip_address_host"),
        ("http://[::1]/", "ip_address_host"),
        ("http://169.254.169.254/latest/meta-data", "ip_address_host"),
        ("http://localhost:8080", "invalid_host"),
        ("http://intranet.local", "local_host"),
        ("https://firma.de:8443/", "non_standard_port"),
        ("https://www.facebook.com/firma", "not_company_website"),
        ("https://www.xing.com/pages/firma", "not_company_website"),
        ("https://firma de", "contains_whitespace"),
        ("https://-bad-.de", "invalid_host"),
        ("https://firma.invalidtld", "no_public_suffix"),
    ],
)
def test_reject(raw, reason):
    with pytest.raises(UrlRejected) as exc:
        normalize_website(raw)
    assert exc.value.reason == reason


def test_multi_value_takes_first_valid():
    url, reason = first_valid_website(["https://facebook.com/x;https://firma-beispiel.de"])
    assert url.domain == "firma-beispiel.de"
    assert reason is None


def test_first_valid_reports_reason():
    url, reason = first_valid_website(["https://facebook.com/x", None])
    assert url is None and reason == "not_company_website"
    assert first_valid_website([None, ""]) == (None, "empty")


def test_scheme_guess_flag():
    assert normalize_website("firma-beispiel.de").scheme_guessed
    assert not normalize_website("http://firma-beispiel.de").scheme_guessed


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://http//www.4dhome.de", "http://www.4dhome.de/"),
        ("http://https//www.telekom-mms.com", "https://www.telekom-mms.com/"),
        ("http://https://www.x-firma.de/de", "https://www.x-firma.de/de"),
    ],
)
def test_doubled_scheme_repaired(raw, expected):
    assert normalize_website(raw).url == expected
