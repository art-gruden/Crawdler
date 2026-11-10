"""Telegram marketing report built from the same numbers as the dashboard.

Every figure comes from ``jobstats`` (one counting rule, one snapshot); nothing is estimated and no text is
produced by a language model. The wording says "discovered": the posts never claim that vacancies were
published on a given day, never claim to cover the whole DACH market, and list only countries, specializations
and cities that actually have newly discovered vacancies in the period.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from . import jobstats
from .jobstats import Period, StatFilters, StatsError

LANGUAGES = ("uk", "ru", "en", "de")
FORMATS = ("plain", "html", "markdownv2")
TOP_CHOICES = (3, 5, 10)
MAX_MESSAGE = 4096  # Telegram's limit for one text message
BOT_LINK_RE = re.compile(r"^https://t\.me/[A-Za-z][A-Za-z0-9_]{3,31}(\?start=[A-Za-z0-9_-]{1,64})?$")

FLAGS = {"DE": "🇩🇪", "AT": "🇦🇹", "CH": "🇨🇭"}
COUNTRY_NAMES = {
    "DE": {"en": "Germany", "de": "Deutschland", "uk": "Німеччина", "ru": "Германия"},
    "AT": {"en": "Austria", "de": "Österreich", "uk": "Австрія", "ru": "Австрия"},
    "CH": {"en": "Switzerland", "de": "Schweiz", "uk": "Швейцарія", "ru": "Швейцария"},
}


def _plural_slavic(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _headline(lang: str, n: int) -> str:
    if lang == "uk":
        return f"🚀 Знайдено {{n}} {_plural_slavic(n, 'нову вакансію', 'нові вакансії', 'нових вакансій')}!"
    if lang == "ru":
        return f"🚀 Найдено {{n}} {_plural_slavic(n, 'новую вакансию', 'новые вакансии', 'новых вакансий')}!"
    if lang == "de":
        return "🚀 {n} neue Stelle entdeckt!" if n == 1 else "🚀 {n} neue Stellen entdeckt!"
    return "🚀 {n} new job opportunity discovered!" if n == 1 else "🚀 {n} new job opportunities discovered!"


TEXT = {
    "en": {
        "specs": "💼 Top specializations:",
        "cities": "📍 Top cities:",
        "period": "📅 Period:",
        "cta": "🤖 Find jobs matching your experience and skills.",
        "source": "Source: DACH Career Crawler — newly discovered vacancies in our database.",
        "empty": "No new vacancies were discovered in this period.",
        "other_countries": "+{n} in several or unspecified countries",
    },
    "de": {
        "specs": "💼 Top-Fachbereiche:",
        "cities": "📍 Top-Städte:",
        "period": "📅 Zeitraum:",
        "cta": "🤖 Finde Stellen, die zu deiner Erfahrung und deinen Skills passen.",
        "source": "Quelle: DACH Career Crawler — neu entdeckte Stellen in unserer Datenbank.",
        "empty": "In diesem Zeitraum wurden keine neuen Stellen entdeckt.",
        "other_countries": "+{n} in mehreren oder unbekannten Ländern",
    },
    "uk": {
        "specs": "💼 Топ спеціалізацій:",
        "cities": "📍 Топ міст:",
        "period": "📅 Період:",
        "cta": "🤖 Знайдіть вакансії під ваш досвід і навички.",
        "source": "Джерело: DACH Career Crawler — нещодавно знайдені вакансії в нашій базі.",
        "empty": "За цей період нових вакансій не знайдено.",
        "other_countries": "+{n} у кількох або невизначених країнах",
    },
    "ru": {
        "specs": "💼 Топ специализаций:",
        "cities": "📍 Топ городов:",
        "period": "📅 Период:",
        "cta": "🤖 Найдите вакансии под ваш опыт и навыки.",
        "source": "Источник: DACH Career Crawler — недавно найденные вакансии в нашей базе.",
        "empty": "За этот период новых вакансий не найдено.",
        "other_countries": "+{n} в нескольких или неопределённых странах",
    },
}

_MD2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def validate_bot_link(link: str | None) -> str | None:
    if link is None or not str(link).strip():
        return None
    link = str(link).strip()
    if not BOT_LINK_RE.fullmatch(link):
        raise StatsError("bot link must look like https://t.me/<bot_username>")
    return link


def esc(s: str, fmt: str) -> str:
    """Escape dynamic text for the target format. Applied to every piece of text, static or dynamic."""
    if fmt == "html":
        return html.escape(s, quote=False)
    if fmt == "markdownv2":
        return _MD2_SPECIAL.sub(r"\\\1", s)
    return s


def _bold(s: str, fmt: str) -> str:
    if fmt == "html":
        return f"<b>{esc(s, fmt)}</b>"
    if fmt == "markdownv2":
        return f"*{esc(s, fmt)}*"
    return s


def _link(url: str, fmt: str) -> str:
    label = url.removeprefix("https://")
    if fmt == "html":
        return f'<a href="{html.escape(url, quote=True)}">{esc(label, fmt)}</a>'
    if fmt == "markdownv2":
        safe = url.replace("\\", "\\\\").replace(")", "\\)")
        return f"[{esc(label, fmt)}]({safe})"
    return url


def _num(n: int, lang: str) -> str:
    sep = {"en": ",", "de": ".", "uk": " ", "ru": " "}[lang]
    return f"{n:,}".replace(",", sep)


@dataclass(frozen=True)
class ReportOptions:
    language: str = "en"
    top_specializations: int = 5
    top_cities: int = 5
    include_bot_link: bool = True
    include_countries: bool = True
    fmt: str = "plain"
    bot_link: str | None = None

    def validate(self) -> ReportOptions:
        if self.language not in LANGUAGES:
            raise StatsError(f"language must be one of {', '.join(LANGUAGES)}")
        if self.fmt not in FORMATS:
            raise StatsError(f"format must be one of {', '.join(FORMATS)}")
        if self.top_specializations not in TOP_CHOICES or self.top_cities not in TOP_CHOICES:
            raise StatsError("top_specializations and top_cities must be 3, 5 or 10")
        validate_bot_link(self.bot_link)
        return self

    def config(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "top_specializations": self.top_specializations,
            "top_cities": self.top_cities,
            "include_bot_link": self.include_bot_link,
            "include_countries": self.include_countries,
            "format": self.fmt,
        }


def gather(db: Session, period: Period, f: StatFilters, snapshot: dt.datetime, opts: ReportOptions) -> dict[str, Any]:
    """The report's numbers, straight from the dashboard functions."""
    total = jobstats.count_new(db, f, period.start, period.upto)
    ctry = jobstats.countries(db, period, f, snapshot)
    specs = jobstats.specializations(db, period, f, snapshot, level="specialization", sort="sel", limit=None)
    city = jobstats.cities(db, period, f, snapshot, mode="unique", level="city", sort="sel", limit=None)
    top_specs = [r for r in specs["rows"] if r["id"] is not None and r["sel"] > 0][: opts.top_specializations]
    top_cities = [r for r in city["rows"] if r["kind"] == "city" and r["sel"] > 0][: opts.top_cities]
    by_country = {r["code"]: r["sel"] for r in ctry["rows"] if r["code"] in jobstats.DACH}
    return {
        "new_jobs_total": total,
        "countries": by_country,
        "other_countries": max(0, total - sum(by_country.values())),
        "specializations": [
            {"slug": r["slug"], "name": r["name"], "name_de": r["name_de"], "count": r["sel"]} for r in top_specs
        ],
        "cities": [
            {"city": r["city"], "country_code": r["country_code"], "region_code": r["region_code"], "count": r["sel"]}
            for r in top_cities
        ],
        "classified_pct": specs["completeness"]["classified_pct"],
    }


def render(period: Period, data: dict[str, Any], opts: ReportOptions) -> str:
    lang, fmt = opts.language, opts.fmt
    t = TEXT[lang]
    n = data["new_jobs_total"]
    a, b = period.display()
    when = a if a == b else f"{a} – {b}"
    lines: list[str] = []

    def put(s: str = "") -> None:
        lines.append(s)

    if n == 0:
        put(esc(t["empty"], fmt))
    else:
        put(_bold(_headline(lang, n).format(n=_num(n, lang)), fmt))
        if opts.include_countries:
            parts = []
            for code in jobstats.DACH:
                c = data["countries"].get(code, 0)
                if c > 0:
                    parts.append(f"{FLAGS[code]} {COUNTRY_NAMES[code][lang]} {_num(c, lang)}")
            if parts:
                put()
                put(esc(" · ".join(parts), fmt))
            if data["other_countries"] > 0 and parts:
                put(esc(t["other_countries"].format(n=_num(data["other_countries"], lang)), fmt))
        if data["specializations"]:
            put()
            put(_bold(t["specs"], fmt))
            for s in data["specializations"]:
                name = s["name_de"] if lang == "de" else s["name"]
                put(esc(f"• {name} — {_num(s['count'], lang)}", fmt))
        if data["cities"]:
            put()
            put(_bold(t["cities"], fmt))
            for c in data["cities"]:
                put(esc(f"• {c['city']} ({c['country_code']}) — {_num(c['count'], lang)}", fmt))
    put()
    put(esc(f"{t['period']} {when}", fmt))
    if n > 0:
        put()
        put(esc(t["cta"], fmt))
        if opts.include_bot_link and opts.bot_link:
            put("👉 " + _link(opts.bot_link, fmt))
    put()
    put(esc(t["source"], fmt))
    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE:
        raise StatsError("the generated post exceeds Telegram's 4096 character limit; reduce the top lists")
    return text


def build(
    db: Session, period: Period, f: StatFilters, snapshot: dt.datetime, opts: ReportOptions
) -> dict[str, Any]:
    opts.validate()
    data = gather(db, period, f, snapshot, opts)
    text = render(period, data, opts)
    warnings: list[str] = []
    if data["new_jobs_total"] and data["classified_pct"] is not None and data["classified_pct"] < 60:
        warnings.append(f"Only {data['classified_pct']} % of the new vacancies have a specialization; the top list is partial.")
    if period.partial:
        warnings.append("The period is still in progress; the numbers are as of the snapshot.")
    if opts.include_bot_link and not opts.bot_link:
        warnings.append("No bot link configured (CRAWLER_STATS_TELEGRAM_BOT_LINK); the post has no link.")
    return {
        "text": text,
        "parse_mode": {"plain": None, "html": "HTML", "markdownv2": "MarkdownV2"}[opts.fmt],
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "length": len(text),
        "empty": data["new_jobs_total"] == 0,
        "period": period.to_dict(),
        "snapshot_at": snapshot.isoformat(),
        "filters": f.as_dict(),
        "options": opts.config(),
        "data": data,
        "warnings": warnings,
    }
