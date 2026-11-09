"""Bounded crawl frontier and sitemap parsing."""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field

from lxml import etree

from .urls import CAREER_STRONG, link_score

_XML = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, recover=True)


@dataclass(order=True)
class Item:
    sort_key: tuple
    url: str = field(compare=False)
    depth: int = field(compare=False, default=0)
    priority: float = field(compare=False, default=0.0)
    reason: str = field(compare=False, default="")
    anchor: str = field(compare=False, default="")
    parent: str | None = field(compare=False, default=None)


class Frontier:
    """Max-priority queue with a hard size bound and URL de-duplication.

    When full, a new item only enters if it beats the current worst one, which is dropped.
    """

    def __init__(self, max_size: int):
        self.max_size = max_size
        self._heap: list[Item] = []
        self._seen: set[str] = set()
        self._counter = itertools.count()
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._heap)

    def seen(self, url: str) -> bool:
        return url in self._seen

    def mark_seen(self, url: str) -> None:
        self._seen.add(url)

    def push(
        self, url: str, priority: float, depth: int, *, reason: str = "", anchor: str = "", parent: str | None = None
    ) -> bool:
        if url in self._seen:
            return False
        item = Item((-priority, depth, next(self._counter)), url, depth, priority, reason, anchor, parent)
        if len(self._heap) >= self.max_size:
            worst = max(self._heap)
            if item >= worst:
                self.dropped += 1
                return False
            self._heap.remove(worst)
            heapq.heapify(self._heap)
            self._seen.discard(worst.url)
            self.dropped += 1
        self._seen.add(url)
        heapq.heappush(self._heap, item)
        return True

    def pop(self) -> Item | None:
        return heapq.heappop(self._heap) if self._heap else None

    def peek_priority(self) -> float | None:
        return self._heap[0].priority if self._heap else None

    def snapshot(self, limit: int = 100) -> list[dict]:
        return [{"url": i.url, "p": i.priority, "d": i.depth, "r": i.reason} for i in sorted(self._heap)[:limit]]


def parse_sitemap(body: bytes, *, max_urls: int) -> tuple[list[str], list[str]]:
    """Returns (child sitemaps, page URLs). Tolerates namespaces and broken XML."""
    if not body.strip():
        return [], []
    try:
        root = etree.fromstring(body, parser=_XML)
    except etree.XMLSyntaxError:
        return [], []
    if root is None:
        return [], []
    tag = etree.QName(root).localname if isinstance(root.tag, str) else ""
    locs = []
    for el in root.iter():
        if isinstance(el.tag, str) and etree.QName(el).localname == "loc" and el.text:
            locs.append(el.text.strip())
            if len(locs) >= max_urls:
                break
    return (locs, []) if tag == "sitemapindex" else ([], locs)


def rank_sitemap_children(children: list[str]) -> list[str]:
    """Career-looking sitemaps first, then page/post sitemaps; product/news sitemaps last."""

    def key(u: str):
        lu = u.lower()
        if CAREER_STRONG.search(lu):
            return 0
        if any(x in lu for x in ("product", "produkt", "news", "post", "blog", "event", "media", "image", "video")):
            return 3
        if any(x in lu for x in ("page", "seite", "sitemap.xml")):
            return 1
        return 2

    return sorted(children, key=key)


def career_urls_from_sitemap(urls: list[str], limit: int = 40) -> list[tuple[str, float]]:
    scored = [(u, link_score(u)) for u in urls]
    scored = [(u, s) for u, s in scored if s >= 3.0]
    scored.sort(key=lambda x: (-x[1], len(x[0])))
    return scored[:limit]
