"""URL canonicalization and career-relevance scoring of links (German + English)."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from gcc_core.urls import UrlRejected, domain_key, normalize_host

TRACKING_PARAMS = re.compile(
    r"^(utm_[a-z]+|gclid|gbraid|wbraid|fbclid|msclkid|mc_[a-z]+|_hs[a-z]+|hsa_[a-z]+|yclid|igshid|"
    r"ref|referrer|source|src|trk|trackingid|campaign|_ga|_gl|cmpid|wt_mc|sessionid|sid|phpsessid|"
    r"jsessionid|cfid|cftoken|zanpid|share|lang_switch)$",
    re.I,
)
SESSION_IN_PATH = re.compile(r";(jsessionid|sid|phpsessid)=[^/?#]*", re.I)
SKIP_EXTENSIONS = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|bmp|tiff?|mp[34]|m4a|mov|avi|wmv|webm|ogg|wav|zip|rar|7z|gz|tgz|bz2|"
    r"exe|dmg|msi|iso|apk|css|js|mjs|json|woff2?|ttf|eot|otf|docx?|xlsx?|pptx?|odt|ods|ics|vcf|rss|atom|"
    r"dwg|step|stp|stl|epub)$",
    re.I,
)


def canonicalize(url: str, base: str | None = None) -> str | None:
    """Absolute, normalized URL without fragment, tracking parameters or session ids.

    Path case and trailing slashes are kept (servers may distinguish them); the query is
    sorted so parameter order does not create duplicates.
    """
    if base:
        url = urljoin(base, url.strip())
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    try:
        host = normalize_host(parts.hostname or "")
        port = parts.port
    except (UrlRejected, ValueError):
        return None
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = SESSION_IN_PATH.sub("", parts.path or "/")
    path = re.sub(r"/{2,}", "/", path) or "/"
    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k))
    )
    return urlunsplit((scheme, netloc, path, query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def host_of(url: str) -> str:
    try:
        return normalize_host(urlsplit(url).hostname or "")
    except (UrlRejected, ValueError):
        return ""


def same_site(url: str, domain: str) -> bool:
    """True when ``url`` belongs to the company's registrable domain (subdomains included)."""
    host = host_of(url)
    return bool(host) and domain_key(host) == domain


# --- relevance -----------------------------------------------------------------

CAREER_STRONG = re.compile(
    r"(karriere|career|jobs?\b|jobs?[-_/]|stellen(angebot|ausschreibung|markt|anzeige|portal)?|offene[-_ ]?stellen|"
    r"vakanz|vacanc|job[-_ ]?(börse|boerse|portal|board|angebote)|arbeiten[-_ ]?(bei|mit|für|fuer)|"
    r"work[-_ ]?with[-_ ]?us|join[-_ ]?(us|our[-_ ]team)|wir[-_ ]?suchen|bewerb|recruit|hiring|"
    r"opening|positions|ausbildung|praktik|werkstudent|trainee|einstieg|team[-_ ]?verst(ä|ae)rkung)",
    re.I,
)
CAREER_WEAK = re.compile(
    r"(team|über[-_ ]?uns|ueber[-_ ]?uns|about|unternehmen|company|mitarbeit|people|culture)", re.I
)
NEGATIVE = re.compile(
    r"(datenschutz|privacy|impressum|imprint|legal|agb|terms|cookie|disclaimer|barrierefrei|accessibility|"
    r"shop|warenkorb|cart|checkout|basket|produkt|product|katalog|catalog|"
    r"news|presse|press|blog|aktuelles|archiv|archive|magazin|newsletter|events?\b|veranstaltung|kalender|calendar|"
    r"termine|messe|webinar|download|mediathek|media[-_ ]?center|login|logout|anmelden|sign[-_ ]?in|register|"
    r"account|konto|kontakt|contact|anfahrt|standorte?\b|referenz|sitemap\.html|suche|search|tag/|category/|"
    r"wp-json|feed/|xmlrpc|\?replytocom|/page/\d+/?$|print=|share=|mailto:|tel:)",
    re.I,
)
JOB_DETAIL_HINT = re.compile(
    r"(\(\s*[mwfd]\s*/\s*[mwfd]\s*(/\s*[mwfdix]\s*)?\)|\b[mwd]/[wmd](/[dix])?\b|\(all genders?\)|\(gn\)|"
    r"\b(entwickler|engineer|developer|administrator|architekt|architect|consultant|berater|manager|"
    r"techniker|ingenieur|elektroniker|mechatroniker|informatiker|fachinformatiker|sachbearbeiter|"
    r"devops|sre|werkstudent|praktikant|auszubildende?r?|ausbildung zum|ausbildung zur|trainee|"
    r"specialist|spezialist|analyst|scientist|lead|leiter|head of|referent|assistenz|mitarbeiter)\b)",
    re.I,
)
PAGINATION_TEXT = re.compile(
    r"^\s*(weiter|nächste|naechste|next|mehr( laden| anzeigen| jobs)?|more|»|›|>|\d{1,3})\s*$", re.I
)
PAGINATION_URL = re.compile(
    r"([?&](page|seite|p|pg|offset|start|pageNumber|currentPage)=\d+|/(page|seite)/\d+/?$)", re.I
)


def link_score(url: str, anchor: str = "", context: str = "") -> float:
    """Priority of a link for career discovery (higher is better, < 0 means skip)."""
    path = urlsplit(url).path.lower()
    if SKIP_EXTENSIONS.search(path):
        return -1.0
    hay_url = f"{path} {urlsplit(url).query}"
    score = 0.0
    if CAREER_STRONG.search(hay_url):
        score += 3.0
    if CAREER_STRONG.search(anchor):
        score += 3.0
    elif CAREER_STRONG.search(context):
        score += 1.0
    if JOB_DETAIL_HINT.search(anchor):
        score += 2.5
    if CAREER_WEAK.search(hay_url) or CAREER_WEAK.search(anchor):
        score += 0.5
    if NEGATIVE.search(hay_url) and score < 3.0:
        score -= 4.0
    elif NEGATIVE.search(anchor) and score < 3.0:
        score -= 2.0
    if path.endswith(".pdf"):
        score = score - 1.0 if score >= 3 else -1.0  # only job-like PDFs are worth reading
    depth = path.count("/")
    score -= max(0, depth - 4) * 0.2
    return score


def is_pagination(url: str, anchor: str, rel: str = "") -> bool:
    return "next" in rel.lower() or bool(PAGINATION_URL.search(url)) or bool(PAGINATION_TEXT.match(anchor or ""))
