"""Small row-versioned MVCC primitive for serializable validation experiments."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Iterator


class MVCCConflict(RuntimeError):
    pass


_MISSING = object()


@dataclass
class _Version:
    created: int
    value: Any
    deleted: int | None = None


@dataclass
class MVCCTransaction:
    store: "MVCCStore"
    start_version: int
    pending: dict[Any, Any] = field(default_factory=dict)
    read_keys: set[Any] = field(default_factory=set)
    read_prefixes: set[str] = field(default_factory=set)
    closed: bool = False

    def get(self, key: Any, default: Any = None) -> Any:
        value = self.store._read(self, key)
        return default if value is _MISSING else copy.deepcopy(value)

    def put(self, key: Any, value: Any) -> None:
        self._ensure_open()
        self.pending[copy.deepcopy(key)] = copy.deepcopy(value)

    def delete(self, key: Any) -> None:
        self._ensure_open()
        self.pending[copy.deepcopy(key)] = _MISSING

    def scan(self, prefix: str = "") -> Iterator[tuple[Any, Any]]:
        self._ensure_open()
        self.read_prefixes.add(prefix)
        keys = set(self.store._versions)
        keys.update(self.pending)
        for key in sorted(keys, key=str):
            if prefix and not str(key).startswith(prefix):
                continue
            value = self.store._read(self, key)
            if value is not _MISSING:
                yield copy.deepcopy(key), copy.deepcopy(value)

    def commit(self) -> int:
        self._ensure_open()
        version = self.store._commit(self)
        self.closed = True
        return version

    def rollback(self) -> None:
        self.closed = True
        self.pending.clear()

    def _ensure_open(self) -> None:
        if self.closed:
            raise MVCCConflict("MVCC transaction is closed")


class MVCCStore:
    """Serializable snapshot store keyed by caller-defined row identifiers."""

    def __init__(self) -> None:
        self._version = 0
        self._versions: dict[Any, list[_Version]] = {}

    @property
    def version(self) -> int:
        return self._version

    def begin(self) -> MVCCTransaction:
        return MVCCTransaction(self, self._version)

    def _visible(self, key: Any, version: int) -> Any:
        candidates = self._versions.get(key, ())
        for item in reversed(candidates):
            if item.created <= version and (item.deleted is None or version < item.deleted):
                return item.value
        return _MISSING

    def _read(self, tx: MVCCTransaction, key: Any) -> Any:
        tx._ensure_open()
        tx.read_keys.add(key)
        if key in tx.pending:
            return tx.pending[key]
        return self._visible(key, tx.start_version)

    def _changed_after(self, key: Any, version: int) -> bool:
        return any(item.created > version or (item.deleted is not None and item.deleted > version) for item in self._versions.get(key, ()))

    def _commit(self, tx: MVCCTransaction) -> int:
        watched = set(tx.read_keys) | set(tx.pending)
        for prefix in tx.read_prefixes:
            watched.update(key for key in self._versions if str(key).startswith(prefix))
            watched.update(key for key in tx.pending if str(key).startswith(prefix))
        if any(self._changed_after(key, tx.start_version) for key in watched):
            raise MVCCConflict("serializable validation failed")
        if not tx.pending:
            return self._version
        self._version += 1
        for key, value in tx.pending.items():
            current = self._versions.setdefault(key, [])
            visible = self._visible(key, self._version)
            if visible is not _MISSING and current and current[-1].deleted is None:
                current[-1].deleted = self._version
            if value is not _MISSING:
                current.append(_Version(self._version, copy.deepcopy(value)))
        return self._version
