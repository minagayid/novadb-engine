"""Deterministic vector index with an exact-recall safety fallback."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any

from .vector import dense_vector


@dataclass(frozen=True)
class VectorMatch:
    key: Any
    distance: float


class VectorANNIndex:
    """Random-hyperplane candidate index with deterministic exact fallback.

    This is a dependency-free ANN prototype.  Search always evaluates every
    stored vector when the bucket does not contain enough candidates, so the
    result is useful for correctness tests even before a production ANN
    library or on-disk index is selected.
    """

    def __init__(self, dimensions: int, *, planes: int = 8, seed: int = 17) -> None:
        if dimensions < 1 or planes < 1:
            raise ValueError("dimensions and planes must be positive")
        self.dimensions = dimensions
        self.planes = planes
        self._vectors: dict[Any, tuple[float, ...]] = {}
        self._buckets: dict[int, set[Any]] = {}
        self._hyperplanes = tuple(
            tuple(self._coefficient(seed, plane, dimension) for dimension in range(dimensions))
            for plane in range(planes)
        )

    @staticmethod
    def _coefficient(seed: int, plane: int, dimension: int) -> float:
        digest = hashlib.sha256(f"{seed}:{plane}:{dimension}".encode("ascii")).digest()
        return 1.0 if digest[0] & 1 else -1.0

    def _normalize(self, vector: Any) -> tuple[float, ...]:
        normalized = tuple(dense_vector(vector, "vector"))
        if len(normalized) != self.dimensions:
            raise ValueError(f"vector dimension mismatch: expected {self.dimensions}")
        return normalized

    def _signature(self, vector: tuple[float, ...]) -> int:
        signature = 0
        for index, plane in enumerate(self._hyperplanes):
            if sum(a * b for a, b in zip(plane, vector)) >= 0:
                signature |= 1 << index
        return signature

    @staticmethod
    def _distance(left: tuple[float, ...], right: tuple[float, ...]) -> float:
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)))

    def add(self, key: Any, vector: Any) -> None:
        if key in self._vectors:
            self.remove(key)
        normalized = self._normalize(vector)
        self._vectors[key] = normalized
        self._buckets.setdefault(self._signature(normalized), set()).add(key)

    def remove(self, key: Any) -> bool:
        vector = self._vectors.pop(key, None)
        if vector is None:
            return False
        bucket = self._buckets.get(self._signature(vector))
        if bucket is not None:
            bucket.discard(key)
            if not bucket:
                self._buckets.pop(self._signature(vector), None)
        return True

    def search(self, vector: Any, *, limit: int = 10, probes: int = 2, exact: bool = True) -> list[VectorMatch]:
        if limit < 1:
            return []
        query = self._normalize(vector)
        signature = self._signature(query)
        candidates: set[Any] = set(self._buckets.get(signature, set()))
        if len(candidates) < limit:
            for distance in range(1, min(probes, self.planes) + 1):
                for bit in range(self.planes):
                    candidates.update(self._buckets.get(signature ^ (1 << bit), set()))
                    if len(candidates) >= limit:
                        break
                if len(candidates) >= limit:
                    break
        if len(candidates) < limit:
            candidates.update(self._vectors)
        if exact:
            # A bucket collision is not a proof of nearest-neighbor recall.
            # The default therefore widens to the full set before ranking.
            candidates.update(self._vectors)
        return [
            VectorMatch(key, self._distance(query, self._vectors[key]))
            for key in sorted(candidates, key=str)
        ][:limit]

    def __len__(self) -> int:
        return len(self._vectors)
