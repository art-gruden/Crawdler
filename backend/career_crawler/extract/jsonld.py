"""schema.org/JobPosting from JSON-LD (also inside @graph, ItemList and arrays) and microdata."""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

from .normalize import RawJob

MAX_LD_BLOCK = 2_000_000


def _iter_nodes(obj, depth: int = 0):
    if depth > 8:
        return
    if isinstance(obj, list):
        for item in obj:
            yield from _iter_nodes(item, depth + 1)
    elif isinstance(obj, dict):
        yield obj
        for key in ("@graph", "itemListElement", "item", "mainEntity", "hasPart"):
            if key in obj:
                yield from _iter_nodes(obj[key], depth + 1)


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    values = t if isinstance(t, list) else [t]
    return {str(v).rsplit("/", 1)[-1] for v in values if v}


def _loads_lenient(raw: str):
    raw = raw.strip().strip(";")
    raw = re.sub(r"^\s*<!--|-->\s*$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Common CMS breakage: raw control characters / trailing commas inside strings.
        fixed = re.sub(r"[\x00-\x1f]", " ", raw)
        fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            return None


def _text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, dict):
        return _text(v.get("name") or v.get("@value") or v.get("value"))
    if isinstance(v, list):
        return ", ".join(t for t in (_text(x) for x in v) if t) or None
    s = str(v).strip()
    return s or None


def _locations(posting: dict) -> tuple[list[dict], str | None]:
    locs = posting.get("jobLocation")
    locs = locs if isinstance(locs, list) else [locs] if locs else []
    out, raws = [], []
    for loc in locs[:20]:
        if not isinstance(loc, dict):
            if isinstance(loc, str):
                raws.append(loc)
            continue
        addr = loc.get("address", loc)
        if isinstance(addr, list):
            addr = addr[0] if addr else {}
        if isinstance(addr, str):
            raws.append(addr)
            continue
        if not isinstance(addr, dict):
            continue
        country = addr.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name") or country.get("identifier")
        entry = {
            "city": _text(addr.get("addressLocality")),
            "postcode": _text(addr.get("postalCode")),
            "region": _text(addr.get("addressRegion")),
            "country": _text(country),
            "street": _text(addr.get("streetAddress")),
        }
        out.append(entry)
        raws.append(
            ", ".join(
                x
                for x in (entry["street"], f"{entry['postcode'] or ''} {entry['city'] or ''}".strip(), entry["country"])
                if x
            )
        )
    return out, "; ".join(r for r in raws if r) or None


def _salary(posting: dict) -> dict | None:
    bs = posting.get("baseSalary") or posting.get("estimatedSalary")
    if isinstance(bs, list):
        bs = bs[0] if bs else None
    if not isinstance(bs, dict):
        return None
    val = bs.get("value", bs)
    if isinstance(val, list):
        val = val[0] if val else {}
    if not isinstance(val, dict):
        val = {"value": val}
    try:
        lo = val.get("minValue", val.get("value"))
        hi = val.get("maxValue", lo)
        lo, hi = float(str(lo).replace(",", ".")), float(str(hi).replace(",", "."))
    except (TypeError, ValueError):
        return None
    if lo <= 0 or hi < lo:
        return None
    unit = str(val.get("unitText") or bs.get("unitText") or "").upper()
    period = {"YEAR": "year", "MONTH": "month", "HOUR": "hour", "WEEK": "week", "DAY": "day"}.get(unit)
    return {
        "min": lo,
        "max": hi,
        "currency": str(bs.get("currency") or "EUR")[:3].upper(),
        "period": period,
        "raw": json.dumps(bs, ensure_ascii=False)[:300],
    }


def posting_to_raw(posting: dict, page_url: str, *, method: str = "jsonld") -> RawJob | None:
    title = _text(posting.get("title") or posting.get("name"))
    if not title:
        return None
    locs, loc_raw = _locations(posting)
    org = posting.get("hiringOrganization")
    emp = posting.get("employmentType")
    emp = emp if isinstance(emp, list) else [emp] if emp else []
    ident = posting.get("identifier")
    ident_val = _text(ident.get("value")) if isinstance(ident, dict) else _text(ident)
    url = _text(posting.get("url"))
    apply_url = None
    action = posting.get("potentialAction") or {}
    if isinstance(action, dict):
        target = action.get("target")
        apply_url = _text(target.get("urlTemplate") if isinstance(target, dict) else target)
    apply_url = apply_url or _text(posting.get("directApply") if isinstance(posting.get("directApply"), str) else None)
    loc_type = _text(posting.get("jobLocationType"))
    remote_req = _text(posting.get("applicantLocationRequirements"))
    desc = posting.get("description")
    pieces = [desc if isinstance(desc, str) else _text(desc) or ""]
    for key in ("responsibilities", "qualifications", "skills", "experienceRequirements", "educationRequirements"):
        v = posting.get(key)
        if isinstance(v, str) and v.strip() and v.strip()[:200] not in pieces[0]:
            pieces.append(f"<h3>{key}</h3>{v}")
    conf = 0.9
    if not pieces[0]:
        conf -= 0.2
    if not locs and not loc_raw:
        conf -= 0.1
    return RawJob(
        title=title,
        source_url=page_url,
        method=method,
        application_url=urljoin(page_url, apply_url) if apply_url else None,
        location_raw=loc_raw,
        locations=locs,
        remote_raw=" ".join(x for x in (loc_type, remote_req) if x) or None,
        employment_raw=[str(e) for e in emp if e],
        date_posted_raw=_text(posting.get("datePosted")),
        valid_through_raw=_text(posting.get("validThrough")),
        description_html="\n".join(pieces),
        salary=_salary(posting),
        hiring_org=_text(org),
        ats_job_id=ident_val,
        confidence=conf,
        source_values={
            "url": urljoin(page_url, url) if url else None,
            "identifier": ident_val,
            "jobLocationType": loc_type,
            "hiringOrganization": _text(org),
            "baseSalary": posting.get("baseSalary") if isinstance(posting.get("baseSalary"), dict) else None,
        },
    )


def extract_jsonld(tree: LexborHTMLParser, page_url: str) -> tuple[list[RawJob], bool]:
    """Returns (postings, has_job_markup). ``has_job_markup`` is True even when parsing failed."""
    jobs: list[RawJob] = []
    seen_markup = False
    for node in tree.css('script[type="application/ld+json"], script[type="application/ld+json; charset=utf-8"]'):
        raw = node.text(deep=True) or ""
        if not raw or len(raw) > MAX_LD_BLOCK:
            continue
        if "JobPosting" in raw:
            seen_markup = True
        data = _loads_lenient(raw)
        if data is None:
            continue
        for obj in _iter_nodes(data):
            if "JobPosting" in _types(obj):
                job = posting_to_raw(obj, page_url)
                if job:
                    jobs.append(job)
    if not jobs:
        jobs.extend(_microdata(tree, page_url))
        seen_markup = seen_markup or bool(jobs)
    return jobs, seen_markup


def _microdata(tree: LexborHTMLParser, page_url: str) -> list[RawJob]:
    out = []
    for scope in tree.css('[itemtype*="schema.org/JobPosting"]')[:100]:

        def prop(name, _scope=scope):
            n = _scope.css_first(f'[itemprop="{name}"]')
            if n is None:
                return None
            return (
                n.attributes.get("content")
                or n.attributes.get("datetime")
                or n.attributes.get("href")
                or n.text(separator=" ").strip()
            )

        title = prop("title")
        if not title:
            continue
        desc_node = scope.css_first('[itemprop="description"]')
        out.append(
            RawJob(
                title=title,
                source_url=page_url,
                method="jsonld",
                location_raw=prop("jobLocation") or prop("addressLocality"),
                date_posted_raw=prop("datePosted"),
                valid_through_raw=prop("validThrough"),
                employment_raw=[e for e in [prop("employmentType")] if e],
                description_html=desc_node.html if desc_node else None,
                confidence=0.8,
                source_values={"format": "microdata"},
            )
        )
    return out
