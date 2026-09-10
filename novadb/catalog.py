"""Durable schema catalog sidecar for the single-node prototype."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


CATALOG_VERSION = 1


def snapshot(version: int, tables: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "catalog_version": CATALOG_VERSION,
        "engine_version": version,
        "tables": {
            name: {
                "columns": [column.__dict__ for column in table.columns],
                "indexes": sorted(table.indexes),
                "row_count": len(table.rows),
            }
            for name, table in sorted(tables.items())
        },
    }


def write(path: str | os.PathLike[str], version: int, tables: Mapping[str, Any]) -> None:
    """Atomically publish schema metadata after the state image is durable."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(snapshot(version, tables), ensure_ascii=False, separators=(",", ":")))
    with temporary.open("rb+") as handle:
        os.fsync(handle.fileno())
    temporary.replace(target)


def read(path: str | os.PathLike[str]) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text())
    if payload.get("catalog_version") != CATALOG_VERSION:
        raise ValueError("unsupported NovaDB catalog version")
    return payload
