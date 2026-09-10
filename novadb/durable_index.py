"""Atomic, checksummed slotted-page persistence for B+ tree index images."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .btree import BPlusTree
from .slotted_page import SlottedPage, SlottedPageError


class DurableIndexError(RuntimeError):
    pass


class DurableBPlusTree:
    """Persist B+ tree leaves as a sequence of checksummed slotted pages."""

    def __init__(self, path: str | os.PathLike[str], *, page_size: int = 4096, max_keys: int = 32) -> None:
        self.path = Path(path)
        self.page_size = page_size
        self.max_keys = max_keys
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, tree: BPlusTree) -> None:
        pages: list[SlottedPage] = []
        page = SlottedPage(0, self.page_size)
        for key, positions in tree.items():
            record = json.dumps({"key": key, "positions": positions}, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            try:
                page.insert(record)
            except SlottedPageError:
                if not page.records:
                    raise DurableIndexError("index record exceeds slotted page capacity")
                pages.append(page)
                page = SlottedPage(len(pages), self.page_size)
                page.insert(record)
        if page.records or not pages:
            pages.append(page)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        if temporary.exists():
            temporary.unlink()
        with temporary.open("wb") as handle:
            for item in pages:
                handle.write(item.to_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    def load(self) -> BPlusTree:
        tree = BPlusTree(max_keys=self.max_keys)
        if not self.path.exists():
            return tree
        size = self.path.stat().st_size
        if size == 0 or size % self.page_size:
            raise DurableIndexError("index file ends with a partial page")
        with self.path.open("rb") as handle:
            for page_id in range(size // self.page_size):
                data = handle.read(self.page_size)
                try:
                    page = SlottedPage.from_bytes(data, self.page_size)
                except (ValueError, SlottedPageError) as exc:
                    raise DurableIndexError(f"invalid index page {page_id}") from exc
                if page.page_id != page_id:
                    raise DurableIndexError("index page sequence is invalid")
                for raw in page.records:
                    if raw is None:
                        continue
                    try:
                        record = json.loads(raw.decode("utf-8"))
                        key = record["key"]
                        positions = [int(value) for value in record["positions"]]
                    except (UnicodeDecodeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                        raise DurableIndexError("invalid index record") from exc
                    for position in positions:
                        tree.insert(key, position)
        return tree

    def verify(self, tree: BPlusTree) -> bool:
        try:
            loaded = self.load()
        except DurableIndexError:
            return False
        return list(loaded.items()) == list(tree.items())
