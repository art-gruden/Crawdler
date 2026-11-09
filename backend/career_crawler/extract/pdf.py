"""Job descriptions published as PDF (bounded: size, pages, text)."""

from __future__ import annotations

import io
import re

from ..urls import JOB_DETAIL_HINT
from .normalize import GENDER_TAG, RawJob, clean_text

MAX_PAGES = 6


def pdf_text(data: bytes, max_chars: int = 40_000) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return ""
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            return ""
        parts = []
        for page in reader.pages[:MAX_PAGES]:
            parts.append(page.extract_text() or "")
            if sum(map(len, parts)) > max_chars:
                break
    except Exception:  # malformed PDFs are common; never crash the scan
        return ""
    return clean_text("\n".join(parts))[:max_chars]


def extract_pdf(data: bytes, url: str, *, career_page: str | None = None, anchor: str = "") -> RawJob | None:
    text = pdf_text(data)
    if len(text) < 150:
        return None
    title = None
    anchor = re.sub(
        r"\s*[-–|]?\s*(stellenanzeige|stellenausschreibung|download)?\s*\(?pdf\)?\s*$", "", anchor or "", flags=re.I
    )
    for line in text.split("\n")[:25] + [anchor]:
        line = line.strip()
        if 6 <= len(line) <= 140 and (GENDER_TAG.search(line) or JOB_DETAIL_HINT.search(line)):
            title = line
            break
    if not title:
        return None
    conf = 0.5 + (0.15 if GENDER_TAG.search(title) else 0)
    if re.search(r"aufgaben|profil|anforderungen|responsibilities|requirements", text, re.I):
        conf += 0.1
    return RawJob(
        title=title,
        source_url=url,
        method="pdf",
        career_page_url=career_page,
        description_text=text,
        confidence=conf,
        source_values={"format": "pdf"},
    )
