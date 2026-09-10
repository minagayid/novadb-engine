from __future__ import annotations

import json
import os
import struct
import zlib
from pathlib import Path
from typing import Any, Callable, Iterator


PAGE_SIZE = 16 * 1024
MAGIC = b"NVP1"
VERSION = 1
HEADER = struct.Struct(">4sB3xQII")  # magic, version, page_id, payload_length, crc32
HEADER_SIZE = HEADER.size


class PageCorruptionError(RuntimeError):
    pass


class CrashInjected(RuntimeError):
    """Test-only interruption raised at a named storage boundary."""


class PageStore:
    """Append-oriented fixed-size page file used by the bulk write path."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        page_size: int = PAGE_SIZE,
        *,
        recover_torn_tail: bool = False,
        fault_injector: Callable[[str], None] | None = None,
    ):
        if page_size <= HEADER_SIZE + 16:
            raise ValueError("page_size is too small")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.page_size = page_size
        self.recovered_torn_tail = False
        self.fault_injector = fault_injector
        self.recover_torn_tail = recover_torn_tail
        self._next_page_id = self._discover_next_page_id()

    def _inject(self, stage: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(stage)

    def _discover_next_page_id(self) -> int:
        if not self.path.exists():
            return 0
        size = self.path.stat().st_size
        if size % self.page_size:
            if self.recover_torn_tail:
                valid_size = size - (size % self.page_size)
                with self.path.open("rb+") as handle:
                    handle.truncate(valid_size)
                self.recovered_torn_tail = True
                return valid_size // self.page_size
            raise PageCorruptionError("page file ends with a partial page")
        return size // self.page_size

    @property
    def next_page_id(self) -> int:
        return self._next_page_id

    @property
    def page_count(self) -> int:
        return self._next_page_id

    def append_records(self, records: list[dict[str, Any]], sync: bool = True) -> list[int]:
        if not records:
            return []
        pages: list[tuple[int, bytes]] = []
        payload_capacity = self.page_size - HEADER_SIZE
        page_id = self._next_page_id
        payload = bytearray()
        for record in records:
            encoded = json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            framed = struct.pack(">I", len(encoded)) + encoded
            if len(framed) > payload_capacity:
                raise ValueError("record exceeds page capacity")
            if payload and len(payload) + len(framed) > payload_capacity:
                pages.append((page_id, bytes(payload)))
                page_id += 1
                payload = bytearray()
            payload.extend(framed)
        if payload:
            pages.append((page_id, bytes(payload)))
        self._inject("append_before_write")
        with self.path.open("ab") as handle:
            for current_id, page_payload in pages:
                header = HEADER.pack(MAGIC, VERSION, current_id, len(page_payload), zlib.crc32(page_payload) & 0xFFFFFFFF)
                handle.write(header + page_payload + bytes(self.page_size - HEADER_SIZE - len(page_payload)))
                self._inject("append_after_page_write")
            handle.flush()
            self._inject("append_after_flush")
            if sync:
                os.fsync(handle.fileno())
                self._inject("append_after_fsync")
        self._next_page_id = page_id + 1
        return [current_id for current_id, _ in pages]

    def _read_block(self, page_id: int) -> bytes:
        if page_id < 0 or page_id >= self.page_count:
            raise PageCorruptionError(f"page {page_id} is not present")
        with self.path.open("rb") as handle:
            handle.seek(page_id * self.page_size)
            block = handle.read(self.page_size)
        if len(block) != self.page_size:
            raise PageCorruptionError(f"truncated page {page_id}")
        return block

    def read_page(self, page_id: int) -> list[dict[str, Any]]:
        """Decode and verify one page for a future buffer-pool consumer."""
        block = self._read_block(page_id)
        magic, version, stored_id, payload_length, checksum = HEADER.unpack(block[:HEADER_SIZE])
        if magic != MAGIC or version != VERSION or stored_id != page_id:
            raise PageCorruptionError(f"invalid page header at {page_id}")
        if payload_length > self.page_size - HEADER_SIZE:
            raise PageCorruptionError(f"invalid payload length at page {page_id}")
        payload = block[HEADER_SIZE:HEADER_SIZE + payload_length]
        if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
            raise PageCorruptionError(f"checksum mismatch at page {page_id}")
        records: list[dict[str, Any]] = []
        offset = 0
        while offset < len(payload):
            if offset + 4 > len(payload):
                raise PageCorruptionError(f"truncated record frame at page {page_id}")
            length = struct.unpack(">I", payload[offset:offset + 4])[0]
            offset += 4
            if offset + length > len(payload):
                raise PageCorruptionError(f"truncated record at page {page_id}")
            records.append(json.loads(payload[offset:offset + length]))
            offset += length
        return records

    def iter_records(self) -> Iterator[dict[str, Any]]:
        for page_id in range(self.page_count):
            yield from self.read_page(page_id)

    def checkpoint(self, records: list[dict[str, Any]]) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        if temporary.exists():
            temporary.unlink()
        replacement = PageStore(temporary, self.page_size)
        if records:
            replacement.append_records(records)
        else:
            temporary.touch()
        self._inject("checkpoint_before_replace")
        temporary.replace(self.path)
        self._inject("checkpoint_after_replace")
        self._next_page_id = replacement.next_page_id
