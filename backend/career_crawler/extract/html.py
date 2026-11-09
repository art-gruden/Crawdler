"""HTML page classification, link extraction, job-list and job-detail extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from ..urls import CAREER_STRONG, JOB_DETAIL_HINT, canonicalize, is_pagination
from .normalize import GENDER_TAG, REQ_HEAD, RESP_HEAD, RawJob, clean_text, html_to_text, tidy_title

NO_VACANCIES = re.compile(
    r"(derzeit|aktuell|momentan|zurzeit|zur zeit|leider)\s+(sind\s+)?(haben\s+wir\s+)?(keine|kein)\s+"
    r"(offenen?\s+)?(stellen|stellenangebote|vakanzen|jobs|positionen|ausschreibungen)|"
    r"keine\s+offenen\s+stellen|"
    r"no (current )?(open )?(positions|vacancies|openings|jobs) (available|at the moment|currently)|"
    r"there are (currently )?no (open )?(positions|vacancies|openings)|"
    r"initiativbewerbung\s+(ist|sind)\s+(jederzeit|immer)",
    re.I,
)
JS_REQUIRED = re.compile(
    r"(enable javascript|javascript (is )?(required|disabled)|javascript aktivieren|aktivieren sie javascript|"
    r"you need to enable javascript|bitte javascript)",
    re.I,
)
JOB_PATH = re.compile(
    r"/(jobs?|stellen(angebote?)?|stelle|karriere|career|careers|vacanc(y|ies)|positions?|openings?|"
    r"jobboerse|jobbörse|stellenanzeige|ausschreibung)(/|-|_)[^/]{4,}",
    re.I,
)
APPLY = re.compile(r"(jetzt\s+)?(online[- ])?(bewerben|bewerbung starten|apply( now)?|zur bewerbung)", re.I)
LOCATION_LABEL = re.compile(
    r"(?:standort|einsatzort|arbeitsort|dienstort|location|ort|arbeitsplatz)\s*[:|]\s*([^\n|]{2,80})", re.I
)
APP_ROOTS = ("#app", "#root", "#__next", "#__nuxt", "[ng-app]", "app-root", "#q-app")


@dataclass
class Link:
    url: str
    anchor: str
    rel: str = ""
    context: str = ""
    tag: str = "a"


@dataclass
class PageSignals:
    page_type: str  # job_detail | job_list | career_landing | no_vacancies | js_shell | irrelevant
    title: str
    text_len: int
    job_links: list[Link] = field(default_factory=list)
    pagination: list[str] = field(default_factory=list)
    career_score: float = 0.0
    js_shell: bool = False
    technologies: list[str] = field(default_factory=list)


def parse(markup: str) -> LexborHTMLParser:
    return LexborHTMLParser(markup)


def links(tree: LexborHTMLParser, base: str, limit: int = 3000) -> list[Link]:
    base_node = tree.css_first("base[href]")
    if base_node is not None:
        base = canonicalize(base_node.attributes.get("href") or "", base) or base
    out: list[Link] = []
    for node in tree.css("a[href], area[href], link[rel][href], iframe[src]"):
        if len(out) >= limit:
            break
        attrs = node.attributes
        href = attrs.get("href") or attrs.get("src") or ""
        if node.tag == "link" and "next" not in (attrs.get("rel") or "").lower():
            continue
        if href.startswith(("mailto:", "tel:", "javascript:", "data:", "#")):
            continue
        url = canonicalize(href, base)
        if not url:
            continue
        anchor = re.sub(r"\s+", " ", node.text(separator=" ") or attrs.get("title") or attrs.get("aria-label") or "")
        ctx = ""
        parent = node.parent
        if parent is not None and node.tag == "a":
            ctx = re.sub(r"\s+", " ", parent.text(separator=" ") or "")[:200]
        out.append(Link(url=url, anchor=anchor.strip()[:200], rel=attrs.get("rel") or "", context=ctx, tag=node.tag))
    return out


def script_sources(tree: LexborHTMLParser, limit_inline: int = 200_000) -> tuple[list[str], str]:
    srcs = [n.attributes.get("src") or "" for n in tree.css("script[src]")]
    srcs += [n.attributes.get("src") or "" for n in tree.css("iframe[src]")]
    inline = []
    total = 0
    for n in tree.css("script:not([src])"):
        t = n.text(deep=True) or ""
        if total + len(t) > limit_inline:
            break
        total += len(t)
        inline.append(t)
    return [s for s in srcs if s], "\n".join(inline)


def page_title(tree: LexborHTMLParser) -> str:
    h1 = tree.css_first("h1")
    if h1 is not None and h1.text(strip=True):
        title = tidy_title(h1.text(separator=" "))
        if m := LOCATION_LABEL.search(title):  # unclosed <h1> swallowed the next line
            title = title[: m.start()].strip(" -–|,")
        return title
    og = tree.css_first('meta[property="og:title"]')
    if og is not None and og.attributes.get("content"):
        return tidy_title(og.attributes["content"])
    t = tree.css_first("title")
    return tidy_title(t.text()) if t is not None else ""


def technologies(tree: LexborHTMLParser, srcs: list[str]) -> list[str]:
    tech = []
    gen = tree.css_first('meta[name="generator"]')
    g = (gen.attributes.get("content") or "").lower() if gen is not None else ""
    joined = " ".join(srcs).lower()
    for name, rx in (
        ("wordpress", r"wordpress|wp-content"),
        ("typo3", r"typo3"),
        ("drupal", r"drupal"),
        ("joomla", r"joomla"),
        ("next.js", r"_next/"),
        ("nuxt", r"_nuxt/"),
        ("react", r"react"),
        ("angular", r"angular|zone\.js"),
        ("vue", r"vue(\.min)?\.js"),
        ("wix", r"wix|parastorage"),
        ("jimdo", r"jimdo"),
        ("shopify", r"shopify"),
    ):
        if re.search(rx, g) or re.search(rx, joined):
            tech.append(name)
    return tech


def is_job_like_anchor(text: str) -> bool:
    return 6 <= len(text) <= 160 and bool(JOB_DETAIL_HINT.search(text) or GENDER_TAG.search(text))


def find_job_links(all_links: list[Link], page_url: str) -> list[Link]:
    """Links that look like individual postings on a list page."""
    out: dict[str, Link] = {}
    page_path = urlsplit(page_url).path.rstrip("/")
    by_prefix: dict[str, list[Link]] = {}
    for ln in all_links:
        if ln.tag not in ("a", "area") or ln.url.rstrip("/") == page_url.rstrip("/"):
            continue
        path = urlsplit(ln.url).path
        if is_job_like_anchor(ln.anchor) and not re.search(r"\b(alle|all|weitere|more|mehr)\b", ln.anchor, re.I):
            out.setdefault(ln.url, ln)
            continue
        if JOB_PATH.search(path) and path.rstrip("/") != page_path and 8 <= len(ln.anchor) <= 160:
            prefix = path.rstrip("/").rsplit("/", 1)[0]
            by_prefix.setdefault(prefix, []).append(ln)
    for prefix, group in by_prefix.items():
        slugs = {urlsplit(g.url).path.rstrip("/").rsplit("/", 1)[-1] for g in group}
        if len(slugs) >= 2 and CAREER_STRONG.search(prefix + "/"):
            for g in group:
                out.setdefault(g.url, g)
    return list(out.values())


def classify(tree: LexborHTMLParser, url: str, all_links: list[Link], *, has_jobposting: bool) -> PageSignals:
    title = page_title(tree)
    body = tree.body
    visible = clean_text(body.text(separator="\n")) if body is not None else ""
    text_len = len(visible)
    job_links = find_job_links(all_links, url)
    pagination = [ln.url for ln in all_links if is_pagination(ln.url, ln.anchor, ln.rel)]
    head = f"{title} {urlsplit(url).path}"  # path only: a host like "nocareer.de" is not evidence
    career_score = (2.0 if CAREER_STRONG.search(head) else 0.0) + min(
        3.0, len(CAREER_STRONG.findall(visible[:20_000])) * 0.3
    )
    srcs, _inline = script_sources(tree, limit_inline=0)
    shell = text_len < 600 and (
        any(tree.css_first(sel) is not None for sel in APP_ROOTS)
        or JS_REQUIRED.search(visible) is not None
        or len(srcs) >= 3
    )
    if has_jobposting:
        ptype = "job_detail"
    elif NO_VACANCIES.search(visible[:30_000]) and len(job_links) < 2:
        ptype = "no_vacancies"
    elif len(job_links) >= 2:
        ptype = "job_list"
    elif (GENDER_TAG.search(title) or JOB_DETAIL_HINT.search(title)) and (
        RESP_HEAD.search(visible) or REQ_HEAD.search(_heading_text(tree)) or APPLY.search(visible)
    ):
        ptype = "job_detail"
    elif shell:
        ptype = "js_shell"
    elif career_score >= 2.0 or len(job_links) == 1:
        ptype = "career_landing"
    else:
        ptype = "irrelevant"
    return PageSignals(
        page_type=ptype,
        title=title,
        text_len=text_len,
        job_links=job_links,
        pagination=pagination,
        career_score=career_score,
        js_shell=shell,
        technologies=technologies(tree, srcs),
    )


def _heading_text(tree: LexborHTMLParser) -> str:
    return "\n".join(n.text(separator=" ").strip() for n in tree.css("h2, h3, h4, strong, b")[:80])


def _main_node(tree: LexborHTMLParser):
    for sel in (
        "[itemprop='description']",
        "article",
        "main",
        "[role='main']",
        ".job-description",
        ".jobdescription",
        ".stellenanzeige",
        ".job-detail",
        ".job",
        "#content",
        ".content",
    ):
        node = tree.css_first(sel)
        if node is not None and len(node.text(strip=True)) > 300:
            return node
    return tree.body


def extract_detail(
    tree: LexborHTMLParser,
    url: str,
    markup: str,
    *,
    method: str = "html",
    career_page: str | None = None,
    use_trafilatura: bool = True,
) -> RawJob | None:
    title = page_title(tree)
    if not title or not (GENDER_TAG.search(title) or JOB_DETAIL_HINT.search(title) or len(title) <= 120):
        return None
    main = _main_node(tree)
    desc = html_to_text(main.html if main is not None else markup)
    used = method
    if use_trafilatura and len(desc) < 400:
        better = content_text(markup, url)
        if better and len(better) > len(desc):
            desc, used = better, "content" if method == "html" else method
    head_text = desc[:3000]
    loc = None
    if m := LOCATION_LABEL.search(head_text):
        loc = m.group(1).strip(" .,;")
    else:
        for sel in (
            ".location",
            ".job-location",
            "[class*='location']",
            "[class*='standort']",
            "[itemprop='jobLocation']",
        ):
            n = tree.css_first(sel)
            if n is not None and 2 <= len(n.text(strip=True)) <= 120:
                loc = n.text(separator=" ").strip()
                break
    apply_url = None
    for a in tree.css("a[href]"):
        if APPLY.search(a.text(separator=" ") or ""):
            apply_url = canonicalize(a.attributes.get("href") or "", url)
            if apply_url:
                break
    emp = [
        m.group(0)
        for m in re.finditer(
            r"(vollzeit|teilzeit|full[- ]time|part[- ]time|befristet|unbefristet|festanstellung)[^\n]{0,60}",
            head_text,
            re.I,
        )
    ][:4]
    has_sections = bool(RESP_HEAD.search(desc) or REQ_HEAD.search(desc))
    conf = 0.55 + (0.15 if GENDER_TAG.search(title) else 0) + (0.15 if has_sections else 0) + (0.05 if loc else 0)
    if len(desc) < 200:
        conf -= 0.25
    date_raw = None
    if m := re.search(
        r"(veröffentlicht|online seit|datum|posted|published|erstellt)\s*(am|on)?\s*:?\s*"
        r"(\d{1,2}\.\s?\d{1,2}\.\s?\d{4}|\d{4}-\d{2}-\d{2})",
        head_text,
        re.I,
    ):
        date_raw = m.group(3)
    return RawJob(
        title=title,
        source_url=url,
        method=used,
        application_url=apply_url,
        career_page_url=career_page,
        location_raw=loc,
        employment_raw=emp,
        date_posted_raw=date_raw,
        description_text=desc,
        confidence=conf,
        source_values={"page_title": title},
    )


def content_text(markup: str, url: str) -> str | None:
    """Trafilatura main-content extraction (optional dependency)."""
    try:
        import trafilatura
    except ImportError:  # pragma: no cover
        return None
    try:
        out = trafilatura.extract(
            markup, url=url, include_comments=False, include_tables=True, favor_recall=True, deduplicate=True
        )
    except Exception:
        return None
    return clean_text(out) if out else None


def list_entries(tree: LexborHTMLParser, page_url: str, job_links: list[Link], career_page: str) -> list[RawJob]:
    """Minimal postings from a list page (title + URL + location hint); details may enrich them later."""
    out = []
    for ln in job_links:
        title = ln.anchor
        loc = None
        if ln.context and ln.context != ln.anchor:
            rest = ln.context.replace(ln.anchor, " ")
            if m := re.search(
                r"\b\d{5}\s+[A-ZÄÖÜ][\wäöüß-]+|\b(Dresden|Leipzig|Chemnitz|Berlin|München|Hamburg|Remote)\b", rest
            ):
                loc = m.group(0)
        out.append(
            RawJob(
                title=title,
                source_url=ln.url,
                method="html",
                career_page_url=career_page,
                location_raw=loc,
                confidence=0.45,
                source_values={"list_page": page_url},
            )
        )
    return out
