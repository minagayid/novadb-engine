"""Checksummed variable-length slotted pages for durable index records."""

from __future__ import annotations

import struct
import zlib


MAGIC = b"NSP1"
VERSION = 1
HEADER = struct.Struct(">4sB3xQHHHI")
HEADER_SIZE = HEADER.size
SLOT = struct.Struct(">HH")
SLOT_SIZE = SLOT.size


class SlottedPageError(RuntimeError):
    pass


class SlottedPage:
    """A fixed-size page with stable slot ids and compactable tombstones."""

    def __init__(self, page_id: int, page_size: int = 4096, records: list[bytes | None] | None = None) -> None:
        if page_id < 0 or page_size <= HEADER_SIZE + SLOT_SIZE:
            raise ValueError("invalid slotted page dimensions")
        self.page_id = page_id
        self.page_size = page_size
        self.records = list(records or [])
        if self.free_space < 0:
            raise SlottedPageError("records do not fit in page")

    @property
    def free_space(self) -> int:
        used = sum(len(record) for record in self.records if record is not None)
        return self.page_size - HEADER_SIZE - len(self.records) * SLOT_SIZE - used

    def insert(self, record: bytes) -> int:
        if not isinstance(record, bytes) or not record:
            raise ValueError("record must be non-empty bytes")
        if self.free_space < len(record) + SLOT_SIZE:
            raise SlottedPageError("record does not fit in page")
        self.records.append(record)
        return len(self.records) - 1

    def delete(self, slot_id: int) -> None:
        if slot_id < 0 or slot_id >= len(self.records) or self.records[slot_id] is None:
            raise SlottedPageError("slot is not present")
        self.records[slot_id] = None

    def compact(self) -> int:
        removed = sum(record is None for record in self.records)
        self.records = [record for record in self.records if record is not None]
        return removed

    def to_bytes(self) -> bytes:
        slot_count = len(self.records)
        free_start = HEADER_SIZE + slot_count * SLOT_SIZE
        free_end = self.page_size
        body = bytearray(self.page_size - HEADER_SIZE)
        slots: list[tuple[int, int]] = []
        for record in self.records:
            if record is None:
                slots.append((0, 0))
                continue
            free_end -= len(record)
            if free_end < free_start:
                raise SlottedPageError("record does not fit in page")
            body[free_end - HEADER_SIZE:free_end - HEADER_SIZE + len(record)] = record
            slots.append((free_end, len(record)))
        for index, (offset, length) in enumerate(slots):
            SLOT.pack_into(body, index * SLOT_SIZE, offset, length)
        checksum = zlib.crc32(body) & 0xFFFFFFFF
        header = HEADER.pack(MAGIC, VERSION, self.page_id, slot_count, free_start, free_end, checksum)
        return header + bytes(body)

    @classmethod
    def from_bytes(cls, data: bytes, page_size: int = 4096) -> "SlottedPage":
        if len(data) != page_size:
            raise SlottedPageError("slotted page has an invalid size")
        magic, version, page_id, slot_count, free_start, free_end, checksum = HEADER.unpack(data[:HEADER_SIZE])
        body = data[HEADER_SIZE:]
        if magic != MAGIC or version != VERSION:
            raise SlottedPageError("invalid slotted page header")
        if zlib.crc32(body) & 0xFFFFFFFF != checksum:
            raise SlottedPageError("slotted page checksum mismatch")
        expected_start = HEADER_SIZE + slot_count * SLOT_SIZE
        if free_start != expected_start or free_end < free_start or free_end > page_size:
            raise SlottedPageError("invalid slotted page free-space bounds")
        records: list[bytes | None] = []
        occupied: set[tuple[int, int]] = set()
        for index in range(slot_count):
            offset, length = SLOT.unpack_from(body, index * SLOT_SIZE)
            if length == 0:
                records.append(None)
                continue
            if offset < free_start or offset + length > page_size or (offset, length) in occupied:
                raise SlottedPageError("invalid slotted page slot")
            occupied.add((offset, length))
            records.append(bytes(data[offset:offset + length]))
        return cls(page_id, page_size, records)
