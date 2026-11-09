"""Optional LLM extraction fallback (disabled by default; no paid API required).

Talks to any OpenAI-compatible chat-completions endpoint configured by the operator
(e.g. a local Ollama: ``CRAWLER_LLM_URL=http://ollama:11434/v1/chat/completions``). The
endpoint is operator infrastructure, so it is called with a plain client — the
third-party page content only goes *into* the prompt, never decides where we connect.

Used only for career pages where every deterministic strategy found nothing. Results get
low confidence and method ``llm``; they never close other postings.
"""

from __future__ import annotations

import json
import re
import time

import httpx

from .config import CrawlerSettings
from .extract.normalize import RawJob

PROMPT = (
    "Extract job vacancies from the career page text below. Return JSON only: "
    '{"jobs":[{"title":str,"location":str|null,"employment_type":str|null,"url":str|null,'
    '"date_posted":str|null}]}. Only list real open positions explicitly present in the text; '
    'return {"jobs":[]} if there are none. Do not invent dates or URLs.'
)


class LlmBudget:
    def __init__(self, per_hour: int):
        self.per_hour = per_hour
        self._calls: list[float] = []

    def take(self) -> bool:
        now = time.time()
        self._calls = [t for t in self._calls if now - t < 3600]
        if len(self._calls) >= self.per_hour:
            return False
        self._calls.append(now)
        return True


async def extract_with_llm(
    settings: CrawlerSettings, text: str, page_url: str, *, client: httpx.AsyncClient | None = None
) -> list[RawJob]:
    if not (settings.llm_enabled and settings.llm_url):
        return []
    body = {
        "model": settings.llm_model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": text[: settings.llm_max_chars]},
        ],
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {}
    own = client is None
    client = client or httpx.AsyncClient(timeout=settings.llm_timeout_s, trust_env=False)
    try:
        r = await client.post(settings.llm_url, json=body, headers=headers)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
    finally:
        if own:
            await client.aclose()
    m = re.search(r"\{.*\}", content, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return []
    out = []
    lowered = text.lower()
    for j in (data.get("jobs") or [])[:50]:
        title = str(j.get("title") or "").strip()
        if len(title) < 4 or title.lower()[:40] not in lowered:  # must appear verbatim: no hallucinated postings
            continue
        out.append(
            RawJob(
                title=title,
                source_url=page_url,
                method="llm",
                career_page_url=page_url,
                location_raw=j.get("location"),
                employment_raw=[j["employment_type"]] if j.get("employment_type") else [],
                date_posted_raw=j.get("date_posted")
                if j.get("date_posted") and str(j["date_posted"]) in text
                else None,
                confidence=0.4,
                source_values={"llm_model": settings.llm_model},
            )
        )
    return out
