"""How the crawler identifies itself: User-Agent, robots.txt product token, operator contact.

One source of truth: the ``CRAWLER_USER_AGENT`` setting. Everything else is derived from it, so
there is no second identity that could drift from what the sites actually see.

Run ``python -m gcc_core.botinfo`` (inside the api container) to print the active values.
No secrets are read or printed.
"""

from __future__ import annotations

import re

# RFC 9309 §2.2.1: a crawler's product token consists of letters, digits, "_" and "-".
_PRODUCT = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_COMPATIBLE = re.compile(r"\(\s*compatible\s*;\s*([A-Za-z][A-Za-z0-9_-]*)", re.IGNORECASE)
_PRODUCT_WITH_VERSION = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)(?:/\S*)?")
_COMMENT = re.compile(r"\([^)]*\)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_GENERIC_BROWSER_TOKENS = {"mozilla"}


def robots_token(user_agent: str) -> str:
    """The product token robots.txt groups are matched against.

    ``GermanyCareerCrawler/0.1 (+https://x/bot)``                      -> ``GermanyCareerCrawler``
    ``Mozilla/5.0 (compatible; GermanyCareerCrawler/0.1; +https://x)`` -> ``GermanyCareerCrawler``
    """
    ua = (user_agent or "").strip()
    m = _COMPATIBLE.search(ua)
    if m:
        return m.group(1)
    for m in _PRODUCT_WITH_VERSION.finditer(_COMMENT.sub(" ", ua)):
        if m.group(1).lower() not in _GENERIC_BROWSER_TOKENS:
            return m.group(1)
    return ua  # nothing recognisable: fall back to the whole string (matches nothing special)


def contact_email(configured: str | None, user_agent: str) -> str | None:
    """``BOT_CONTACT_EMAIL`` if it looks like an address, else an address already published in
    the User-Agent, else ``None``. Never invents one."""
    if configured and _EMAIL.fullmatch(configured.strip()):
        return configured.strip()
    m = _EMAIL.search(user_agent or "")
    return m.group(0) if m else None


def describe() -> dict:
    """Public, non-secret description of the crawler identity and politeness policy."""
    from career_crawler.config import get_crawler_settings
    from career_crawler.fetch import MAX_CRAWL_DELAY_S

    from .config import get_settings

    s = get_settings()
    c = get_crawler_settings()
    return {
        "name": "Germany Career Crawler",
        "user_agent": s.crawler_user_agent,
        "robots_token": robots_token(s.crawler_user_agent),
        "contact_email": contact_email(s.bot_contact_email, s.crawler_user_agent),
        "per_domain_delay_s": c.per_domain_delay_s,
        "max_crawl_delay_s": MAX_CRAWL_DELAY_S,
    }


def main() -> None:  # pragma: no cover - thin CLI
    info = describe()
    width = max(map(len, info))
    for k, v in info.items():
        print(f"{k:<{width}}  {v if v is not None else '(not configured)'}")
    if not info["contact_email"]:
        print("\nwarning: no contact address - set BOT_CONTACT_EMAIL in .env so site owners can reach you")
    if "example.org" in info["user_agent"] or "your-org" in info["user_agent"]:
        print("warning: CRAWLER_USER_AGENT still contains a placeholder URL")


if __name__ == "__main__":  # pragma: no cover
    main()
