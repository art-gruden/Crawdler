"""Website URL normalization and the domain key used for deduplication.

``normalize_website("WWW.Müller-GmbH.de/karriere?utm_source=x#top")`` →
``NormalizedUrl(url="https://www.xn--mller-gmbh-xhb.de/karriere", host=..., domain="xn--mller-gmbh-xhb.de")``

The dedup key is the registrable domain (eTLD+1, Public Suffix List incl. private
suffixes, bundled snapshot, no network access). Sites on shared hosting platforms
are keyed by their full host so that unrelated companies are not merged.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import tldextract

_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)

# Registrable domains under which many unrelated customers live (not in the PSL).
SHARED_HOSTING = frozenset(
    {
        "jimdo.com",
        "jimdosite.com",
        "jimdofree.com",
        "business.site",
        "wordpress.com",
        "webnode.page",
        "webnode.com",
        "site123.me",
        "strikingly.com",
        "one.com",
        "beepworld.de",
        "npage.de",
        "de.tl",
    }
)

# Social networks, directories and platforms: a link there is not a company website.
NOT_COMPANY_WEBSITE = frozenset(
    {
        "facebook.com",
        "fb.com",
        "instagram.com",
        "linkedin.com",
        "xing.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "tiktok.com",
        "t.me",
        "wa.me",
        "google.com",
        "google.de",
        "goo.gl",
        "g.page",
        "gelbeseiten.de",
        "dasoertliche.de",
        "dastelefonbuch.de",
        "11880.com",
        "kleinanzeigen.de",
        "ebay.de",
        "ebay.com",
        "amazon.de",
        "yelp.de",
        "yelp.com",
        "tripadvisor.de",
        "booking.com",
        "indeed.com",
        "stepstone.de",
        "wikipedia.org",
        "openstreetmap.org",
        "wer-zu-wem.de",
        "northdata.de",
        "firmenwissen.de",
    }
)

TRACKING_PARAMS = re.compile(r"^(utm_[a-z]+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|_ga|yclid|ref|source)$", re.I)
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_SCHEME = re.compile(r"^([a-z][a-z0-9+.\-]*):", re.I)


class UrlRejected(ValueError):
    """The value cannot be used as a company website. ``reason`` is a short machine code."""

    def __init__(self, reason: str, value: str = ""):
        super().__init__(f"{reason}: {value!r}")
        self.reason = reason
        self.value = value


@dataclass(frozen=True)
class NormalizedUrl:
    url: str
    host: str
    domain: str
    scheme_guessed: bool
    original: str


def split_multi(raw: str | None) -> list[str]:
    """OSM allows several values separated by ';'. Return non-empty parts in order."""
    if not raw:
        return []
    return [p.strip() for p in re.split(r"\s*;\s*", raw) if p.strip()]


def _clean(raw: str) -> str:
    value = raw.strip().strip("<>\"'").strip()
    value = value.replace("\\", "/")
    if any(ch.isspace() for ch in value):
        raise UrlRejected("contains_whitespace", raw)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise UrlRejected("control_characters", raw)
    return value


def normalize_host(host: str) -> str:
    host = host.strip().rstrip(".").lower()
    if not host:
        raise UrlRejected("empty_host", host)
    if host.startswith("[") or _is_ip(host):
        raise UrlRejected("ip_address_host", host)
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UrlRejected("invalid_host", host) from exc
    labels = ascii_host.split(".")
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        raise UrlRejected("invalid_host", host)
    if ascii_host in ("localhost",) or ascii_host.endswith((".localhost", ".local", ".internal", ".lan")):
        raise UrlRejected("local_host", host)
    return ascii_host


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def domain_key(host: str) -> str:
    """Registrable domain used for deduplication."""
    ext = _EXTRACT(host)
    if not ext.suffix or not ext.domain:
        raise UrlRejected("no_public_suffix", host)
    registrable = ext.top_domain_under_public_suffix
    if registrable in SHARED_HOSTING:
        return host.removeprefix("www.")
    return registrable


_DOUBLED_SCHEME = re.compile(r"^https?://(https?)(?::)?//", re.IGNORECASE)


def normalize_website(
    raw: str | None, *, allowed_ports: frozenset[int] | set[int] = frozenset({80, 443})
) -> NormalizedUrl:
    if raw is None or not str(raw).strip():
        raise UrlRejected("empty", raw or "")
    original = str(raw)
    value = _clean(original)
    # Directory exports sometimes double the scheme: "http://http//www.x.de",
    # "http://https://www.x.de". Keep the inner (intended) scheme.
    value = _DOUBLED_SCHEME.sub(lambda m: m.group(1).lower() + "://", value, count=1)

    guessed = False
    if value.startswith("//"):
        value = "https:" + value
        guessed = True
    m = _SCHEME.match(value)
    if m and value[m.end() :].startswith("//"):
        scheme = m.group(1).lower()
        if scheme not in ("http", "https"):
            raise UrlRejected("unsupported_scheme", original)
    elif m and m.group(1).lower() in ("mailto", "tel", "javascript", "data", "file", "ftp"):
        raise UrlRejected("unsupported_scheme", original)
    else:
        value = "https://" + value
        guessed = True

    parts = urlsplit(value)
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UrlRejected("credentials_in_url", original)
    try:
        port = parts.port
    except ValueError as exc:
        raise UrlRejected("invalid_port", original) from exc
    host = normalize_host(parts.hostname or "")
    scheme = parts.scheme.lower()

    default_port = 443 if scheme == "https" else 80
    if port is not None and port != default_port and port not in allowed_ports:
        raise UrlRejected("non_standard_port", original)

    domain = domain_key(host)
    if domain in NOT_COMPANY_WEBSITE:
        raise UrlRejected("not_company_website", original)

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    query_pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    url = urlunsplit((scheme, netloc, path, urlencode(query_pairs), ""))
    return NormalizedUrl(url=url, host=host, domain=domain, scheme_guessed=guessed, original=original)


def first_valid_website(
    values: list[str | None], *, allowed_ports: frozenset[int] | set[int] = frozenset({80, 443})
) -> tuple[NormalizedUrl | None, str | None]:
    """Try candidates in order (incl. ';'-separated parts). Returns (url, first rejection reason)."""
    first_reason = None
    for value in values:
        for part in split_multi(value):
            try:
                return normalize_website(part, allowed_ports=allowed_ports), None
            except UrlRejected as exc:
                first_reason = first_reason or exc.reason
    return None, first_reason or "empty"


def display_domain(domain: str | None) -> str | None:
    """Unicode form for display: xn--mller-xxx.de → müller-….de."""
    if not domain:
        return domain
    try:
        return domain.encode("ascii").decode("idna")
    except UnicodeError:
        return domain
