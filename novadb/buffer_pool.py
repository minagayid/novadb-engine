"""Bounded page cache for the append-oriented storage boundary."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .page_store import PageStore


@dataclass(frozen=True)
class BufferPoolStats:
    capacity: int
    cached_pages: int
    hits: int
    misses: int

    def to_dict(self) -> dict[str, int]:
        return {
            "capacity": self.capacity,
            "cached_pages": self.cached_pages,
            "hits": self.hits,
            "misses": self.misses,
        }


class PageBufferPool:
    """An LRU cache of decoded pages; writes remain owned by ``PageStore``."""

    def __init__(self, page_store: PageStore, capacity: int = 64) -> None:
        if capacity < 1:
            raise ValueError("buffer pool capacity must be positive")
        self.page_store = page_store
        self.capacity = capacity
        self._pages: OrderedDict[int, tuple[dict[str, Any], ...]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def read_page(self, page_id: int) -> tuple[dict[str, Any], ...]:
        if page_id < 0:
            raise ValueError("page_id must be non-negative")
        cached = self._pages.get(page_id)
        if cached is not None:
            self.hits += 1
            self._pages.move_to_end(page_id)
            return cached
        self.misses += 1
        records = tuple(self.page_store.read_page(page_id))
        self._pages[page_id] = records
        self._pages.move_to_end(page_id)
        while len(self._pages) > self.capacity:
            self._pages.popitem(last=False)
        return records

    def scan_records(self) -> list[dict[str, Any]]:
        return [record for page_id in range(self.page_store.page_count) for record in self.read_page(page_id)]

    def invalidate(self) -> None:
        self._pages.clear()

    def stats(self) -> BufferPoolStats:
        return BufferPoolStats(self.capacity, len(self._pages), self.hits, self.misses)
