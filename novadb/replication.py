from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from .engine import Engine
from .page_store import PageStore


def stream_wal(path: str | Path, after_version: int = 0) -> Iterator[dict]:
    """Yield committed versioned page-log records after a follower's version."""
    root = Path(path)
    pages = root / "pages.dat"
    if pages.exists():
        for record in PageStore(pages).iter_records():
            if record.get("version", 0) > after_version:
                yield record
        return
    wal = root / "wal.log"
    if not wal.exists():
        return
    for line in wal.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("version", 0) > after_version:
            yield record


def apply_records(follower: Engine, records: Iterator[dict]) -> int:
    """Apply leader records to a follower, preserving record order.

    This is intentionally a reference implementation for the replication protocol:
    production distributed consensus, fencing, and quorum acknowledgements remain
    explicit future work rather than being hidden behind a misleading API.
    """
    applied = 0
    for record in records:
        if record.get("version", 0) <= follower.version:
            continue
        follower._apply_operations(record["operations"])
        follower.version = record["version"]
        applied += 1
    follower.checkpoint()
    return applied
