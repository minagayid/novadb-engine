"""Validated vector values used by NovaDB's structured and document types."""

from __future__ import annotations

import json
import math
from typing import Any

from .engine_errors import VectorTypeError


VECTOR_TYPES = frozenset({"VECTOR", "VECTOR_DOCUMENT", "VECTOR_DOC", "UNSTRUCTURED_VECTOR"})
MAX_VECTOR_DIMENSION = 4_096
MAX_DOCUMENT_TEXT = 64_000


def _error(column_name: str, message: str) -> VectorTypeError:
    return VectorTypeError(f"Invalid vector value for {column_name}: {message}")


def dense_vector(value: Any, column_name: str = "vector") -> list[float]:
    """Normalize a finite, non-empty dense vector with a bounded dimension."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise _error(column_name, "expected a JSON array") from exc
    if not isinstance(value, (list, tuple)):
        raise _error(column_name, "expected an array of numbers")
    if not value or len(value) > MAX_VECTOR_DIMENSION:
        raise _error(column_name, f"dimension must be between 1 and {MAX_VECTOR_DIMENSION}")
    result: list[float] = []
    for item in value:
        if isinstance(item, bool):
            raise _error(column_name, "boolean components are not numeric")
        try:
            number = float(item)
        except (TypeError, ValueError) as exc:
            raise _error(column_name, "components must be numbers") from exc
        if not math.isfinite(number):
            raise _error(column_name, "components must be finite")
        result.append(number)
    return result


def document_vector(value: Any, column_name: str = "vector_document") -> dict[str, Any]:
    """Normalize an unstructured document plus its searchable embedding."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise _error(column_name, "expected a JSON object") from exc
    if not isinstance(value, dict):
        raise _error(column_name, "expected an object with text and embedding")
    text = value.get("text")
    if not isinstance(text, str) or not text.strip():
        raise _error(column_name, "text must be a non-empty string")
    if len(text) > MAX_DOCUMENT_TEXT:
        raise _error(column_name, f"text exceeds {MAX_DOCUMENT_TEXT} characters")
    if "embedding" not in value:
        raise _error(column_name, "embedding is required")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict):
        raise _error(column_name, "metadata must be an object")
    return {
        "text": text,
        "embedding": dense_vector(value["embedding"], f"{column_name}.embedding"),
        "metadata": metadata,
    }


def embedding(value: Any) -> list[float] | None:
    """Extract a dense embedding from either vector representation."""
    if isinstance(value, dict):
        value = value.get("embedding")
    try:
        return dense_vector(value)
    except VectorTypeError:
        return None
