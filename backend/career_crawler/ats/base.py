"""Plugin interface for career platforms.

Two kinds of plugins:

* **FeedAdapter** – a public ATS endpoint (XML/JSON) for one account. ``detect`` only
  reports an account when the identifier is *visible in evidence* (a link, iframe or
  script URL on a verified company page, or a redirect target). Identifiers are never
  guessed from the company name.
* **PageAdapter** – extracts postings from a fetched HTML page (schema.org JSON-LD,
  generic HTML).

Register new plugins in :mod:`career_crawler.ats` (``FEED_ADAPTERS`` / ``PAGE_ADAPTERS``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from ..extract.normalize import RawJob
    from ..fetch import Fetcher


@dataclass(frozen=True)
class AtsMatch:
    ats_type: str
    account: str
    feed_url: str
    board_url: str
    evidence: str  # the URL / snippet the account was read from
    region: str | None = None


@dataclass
class FeedResult:
    jobs: list[RawJob] = field(default_factory=list)
    complete: bool = True  # a feed lists every open posting: missing ones may be counted as misses
    feed_url: str = ""


class FeedAdapter(Protocol):
    name: str

    def detect(self, url: str) -> AtsMatch | None: ...

    async def fetch(self, fetcher: Fetcher, match: AtsMatch) -> FeedResult: ...


ACCOUNT = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}"
NOT_ACCOUNTS = {"www", "api", "embed", "jobs", "job", "boards", "static", "assets", "cdn", "app", "help", "support"}


def valid_account(acc: str | None) -> bool:
    return bool(acc) and acc.lower() not in NOT_ACCOUNTS and re.fullmatch(ACCOUNT, acc) is not None
