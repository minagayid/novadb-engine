from __future__ import annotations

import json
import os
import struct
import zlib
from pathlib import Path
from typing import Any, Iterator


PAGE_SIZE = 16 * 1024
MAGIC = b"NVP1"
VERSION = 1
HEADER = struct.Struct(">4sB3xQII")  # magic, version, page_id, payload_length, crc32
HEADER_SIZE = HEADER.size


class PageCorruptionError(RuntimeError):
    pass


class PageStore:
    """Append-oriented fixed-size page file used by the bulk write path."""

    def __init__(self, path: str | os.PathLike[str], page_size: int = PAGE_SIZE):
        if page_size <= HEADER_SIZE + 16:
            raise ValueError("page_size is too small")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.page_size = page_size
        self._next_page_id = self._discover_next_page_id()

    def _discover_next_page_id(self) -> int:
        if not self.path.exists():
            return 0
        return self.path.stat().st_size // self.page_size

    @property
    def next_page_id(self) -> int:
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
        with self.path.open("ab") as handle:
            for current_id, page_payload in pages:
                header = HEADER.pack(MAGIC, VERSION, current_id, len(page_payload), zlib.crc32(page_payload) & 0xFFFFFFFF)
                handle.write(header + page_payload + bytes(self.page_size - HEADER_SIZE - len(page_payload)))
            handle.flush()
            if sync:
                os.fsync(handle.fileno())
        self._next_page_id = page_id + 1
        return [current_id for current_id, _ in pages]

    def iter_records(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("rb") as handle:
            page_id = 0
            while True:
                block = handle.read(self.page_size)
                if not block:
                    break
                if len(block) != self.page_size:
                    raise PageCorruptionError(f"truncated page {page_id}")
                magic, version, stored_id, payload_length, checksum = HEADER.unpack(block[:HEADER_SIZE])
                if magic != MAGIC or version != VERSION or stored_id != page_id:
                    raise PageCorruptionError(f"invalid page header at {page_id}")
                if payload_length > self.page_size - HEADER_SIZE:
                    raise PageCorruptionError(f"invalid payload length at page {page_id}")
                payload = block[HEADER_SIZE:HEADER_SIZE + payload_length]
                if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
                    raise PageCorruptionError(f"checksum mismatch at page {page_id}")
                offset = 0
                while offset < len(payload):
                    if offset + 4 > len(payload):
                        raise PageCorruptionError(f"truncated record frame at page {page_id}")
                    length = struct.unpack(">I", payload[offset:offset + 4])[0]
                    offset += 4
                    if offset + length > len(payload):
                        raise PageCorruptionError(f"truncated record at page {page_id}")
                    yield json.loads(payload[offset:offset + length])
                    offset += length
                page_id += 1

    def checkpoint(self, records: list[dict[str, Any]]) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        if temporary.exists():
            temporary.unlink()
        replacement = PageStore(temporary, self.page_size)
        replacement.append_records(records)
        temporary.replace(self.path)
        self._next_page_id = replacement.next_page_id
