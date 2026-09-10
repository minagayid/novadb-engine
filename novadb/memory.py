"""Bounded agent-memory and audit primitives built on NovaDB tables.

The memory API deliberately stores caller-provided summaries rather than raw
chat transcripts.  Embeddings are optional: when an OpenAI-compatible
embedding endpoint is configured, NovaDB can create/query vectors locally on
the same private host as the model.
"""

from __future__ import annotations

import copy
import json
import re
import time
import uuid
from dataclasses import dataclass
from math import isfinite
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from .engine import Engine, NovaDBError, vector_distance
from .vector import dense_vector


MEMORY_TABLE = "agent_memory"
AUDIT_TABLE = "agent_audit_events"
MAX_CONTENT_CHARS = 8_000
MAX_METADATA_CHARS = 8_192
MAX_NAMESPACE_CHARS = 96
MAX_SESSION_CHARS = 160
MAX_KIND_CHARS = 96
MAX_SOURCE_CHARS = 96
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_TTL_SECONDS = 31 * 24 * 60 * 60
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]+$")


class MemoryValidationError(NovaDBError):
    """Raised when an agent-memory request exceeds its contract."""


def ensure_agent_tables(engine: Engine) -> None:
    """Create the stable tables used by the HTTP memory contract."""

    with engine._lock:
        tables = set(engine.tables)
    if MEMORY_TABLE not in tables:
        engine.execute(
            "CREATE TABLE agent_memory ("
            "memory_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, "
            "session_id TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL, "
            "metadata JSON, embedding VECTOR, created_at BIGINT NOT NULL, "
            "expires_at BIGINT NOT NULL, source TEXT NOT NULL)"
        )
        engine.execute("CREATE INDEX agent_memory_namespace ON agent_memory (namespace)")
        engine.execute("CREATE INDEX agent_memory_session ON agent_memory (session_id)")
    if AUDIT_TABLE not in tables:
        engine.execute(
            "CREATE TABLE agent_audit_events ("
            "event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, "
            "session_id TEXT, request_id TEXT, decision TEXT NOT NULL, "
            "payload JSON, created_at BIGINT NOT NULL)"
        )


def _bounded_token(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MemoryValidationError(f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > maximum or not TOKEN_PATTERN.fullmatch(value):
        raise MemoryValidationError(f"{field} contains unsupported characters or is too long")
    return value


def _bounded_content(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MemoryValidationError("content must be a non-empty summary string")
    value = value.strip()
    if len(value) > MAX_CONTENT_CHARS:
        raise MemoryValidationError(f"content exceeds {MAX_CONTENT_CHARS} characters")
    return value


def _metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MemoryValidationError("metadata must be an object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise MemoryValidationError("metadata must be JSON serializable") from exc
    if len(encoded) > MAX_METADATA_CHARS:
        raise MemoryValidationError(f"metadata exceeds {MAX_METADATA_CHARS} characters")
    return copy.deepcopy(value)


def _limit(value: Any, default: int = 8) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise MemoryValidationError("limit must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise MemoryValidationError("limit must be an integer") from exc
    if result < 1 or result > 50:
        raise MemoryValidationError("limit must be between 1 and 50")
    return result


@dataclass(frozen=True)
class OpenAICompatibleEmbedder:
    """Small dependency-free client for a private OpenAI-compatible endpoint."""

    base_url: str
    model: str = "bge-m3"
    token: str | None = None
    timeout_seconds: float = 30.0

    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/embeddings") else f"{base}/embeddings"

    def embed(self, text: str) -> list[float]:
        payload = json.dumps({"model": self.model, "input": text}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(self.endpoint(), data=payload, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                if response.status < 200 or response.status >= 300:
                    raise MemoryValidationError("embedding service returned an error")
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise MemoryValidationError("embedding service is unavailable") from exc
        try:
            vector = data["data"][0]["embedding"]
            result = dense_vector(vector, "embedding")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise MemoryValidationError("embedding service returned an invalid vector") from exc
        if not all(isfinite(item) for item in result):
            raise MemoryValidationError("embedding service returned non-finite values")
        return result


class MemoryStore:
    """Durable, redaction-by-contract memory for n8n and other agents."""

    def __init__(
        self,
        engine: Engine,
        embedder: OpenAICompatibleEmbedder | Callable[[str], list[float]] | None = None,
        default_ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        if default_ttl_seconds < 60 or default_ttl_seconds > MAX_TTL_SECONDS:
            raise ValueError("default_ttl_seconds is outside the allowed range")
        self.engine = engine
        self.embedder = embedder
        self.default_ttl_seconds = default_ttl_seconds
        ensure_agent_tables(engine)

    def _embed(self, text: str, supplied: Any) -> list[float] | None:
        if supplied is not None:
            try:
                return dense_vector(supplied, "embedding")
            except Exception as exc:
                raise MemoryValidationError("embedding must be a finite numeric vector") from exc
        if self.embedder is None:
            return None
        try:
            return self.embedder.embed(text) if hasattr(self.embedder, "embed") else self.embedder(text)
        except MemoryValidationError:
            raise
        except Exception as exc:
            raise MemoryValidationError("embedding service is unavailable") from exc

    def upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise MemoryValidationError("memory request must be an object")
        memory_id = payload.get("memory_id") or uuid.uuid4().hex
        memory_id = _bounded_token(memory_id, "memory_id", 128)
        namespace = _bounded_token(payload.get("namespace", "hospital"), "namespace", MAX_NAMESPACE_CHARS)
        session_id = _bounded_token(payload.get("session_id"), "session_id", MAX_SESSION_CHARS)
        kind = _bounded_token(payload.get("kind", "conversation_summary"), "kind", MAX_KIND_CHARS)
        source = _bounded_token(payload.get("source", "n8n"), "source", MAX_SOURCE_CHARS)
        content = _bounded_content(payload.get("content"))
        metadata = _metadata(payload.get("metadata"))
        ttl = payload.get("ttl_seconds", self.default_ttl_seconds)
        if isinstance(ttl, bool):
            raise MemoryValidationError("ttl_seconds must be an integer")
        try:
            ttl = int(ttl)
        except (TypeError, ValueError) as exc:
            raise MemoryValidationError("ttl_seconds must be an integer") from exc
        if ttl < 60 or ttl > MAX_TTL_SECONDS:
            raise MemoryValidationError(f"ttl_seconds must be between 60 and {MAX_TTL_SECONDS}")
        embedding = self._embed(content, payload.get("embedding"))
        now = int(time.time())
        row = {
            "memory_id": memory_id,
            "namespace": namespace,
            "session_id": session_id,
            "kind": kind,
            "content": content,
            "metadata": metadata,
            "embedding": embedding,
            "created_at": now,
            "expires_at": now + ttl,
            "source": source,
        }
        tx = self.engine.begin()
        table = tx._table(MEMORY_TABLE)
        for existing in [item for item in table.rows if item.get("memory_id") == memory_id]:
            tx.delete(MEMORY_TABLE, f"memory_id = '{memory_id}'")
            break
        tx.delete(MEMORY_TABLE, f"expires_at <= {now}")
        tx.insert(MEMORY_TABLE, row)
        tx.commit()
        return {
            "memory_id": memory_id,
            "namespace": namespace,
            "session_id": session_id,
            "kind": kind,
            "created_at": now,
            "expires_at": now + ttl,
            "embedding_stored": embedding is not None,
        }

    def _visible(self, payload: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        namespace = _bounded_token(payload.get("namespace", "hospital"), "namespace", MAX_NAMESPACE_CHARS)
        session_id = _bounded_token(payload.get("session_id"), "session_id", MAX_SESSION_CHARS)
        kinds = payload.get("kinds")
        if kinds is not None:
            if not isinstance(kinds, list) or not all(isinstance(item, str) for item in kinds):
                raise MemoryValidationError("kinds must be an array of strings")
            kinds = {_bounded_token(item, "kind", MAX_KIND_CHARS) for item in kinds}
        now = int(time.time())
        with self.engine._lock:
            rows = copy.deepcopy(self.engine.tables[MEMORY_TABLE].rows)
        return [
            row for row in rows
            if row.get("namespace") == namespace
            and row.get("session_id") == session_id
            and row.get("expires_at") is not None
            and row.get("expires_at") > now
            and (kinds is None or row.get("kind") in kinds)
        ][:limit]

    def recent(self, payload: dict[str, Any]) -> dict[str, Any]:
        limit = _limit(payload.get("limit"))
        rows = self._visible(payload, 50)
        rows.sort(key=lambda row: (row.get("created_at", 0), row.get("memory_id", "")), reverse=True)
        return {"memories": rows[:limit], "count": min(len(rows), limit), "mode": "recent"}

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise MemoryValidationError("memory search request must be an object")
        limit = _limit(payload.get("limit"))
        rows = self._visible(payload, 50)
        query_embedding = payload.get("query_embedding")
        if query_embedding is None and payload.get("query_text") and self.embedder is not None:
            query_embedding = self._embed(_bounded_content(payload["query_text"]), None)
        if query_embedding is not None:
            try:
                query_embedding = dense_vector(query_embedding, "query_embedding")
            except Exception as exc:
                raise MemoryValidationError("query_embedding must be a finite numeric vector") from exc
            scored = []
            for row in rows:
                if row.get("embedding") is None:
                    continue
                distance = vector_distance(row["embedding"], query_embedding)
                if distance is not None:
                    item = copy.deepcopy(row)
                    item["distance"] = distance
                    scored.append(item)
            scored.sort(key=lambda row: (row["distance"], -row.get("created_at", 0)))
            return {"memories": scored[:limit], "count": min(len(scored), limit), "mode": "semantic"}
        query_text = str(payload.get("query_text", "")).strip().lower()
        if query_text:
            rows = [row for row in rows if query_text in str(row.get("content", "")).lower()]
        rows.sort(key=lambda row: (row.get("created_at", 0), row.get("memory_id", "")), reverse=True)
        return {"memories": rows[:limit], "count": min(len(rows), limit), "mode": "lexical" if query_text else "recent"}

    def audit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise MemoryValidationError("audit request must be an object")
        event_type = _bounded_token(payload.get("event_type", "AGENT_EVENT"), "event_type", 128)
        decision = _bounded_token(payload.get("decision", "recorded"), "decision", 64)
        session_id = payload.get("session_id")
        if session_id is not None:
            session_id = _bounded_token(session_id, "session_id", MAX_SESSION_CHARS)
        request_id = payload.get("request_id")
        if request_id is not None:
            request_id = _bounded_token(request_id, "request_id", 128)
        event_id = uuid.uuid4().hex
        row = {
            "event_id": event_id,
            "event_type": event_type,
            "session_id": session_id,
            "request_id": request_id,
            "decision": decision,
            "payload": _metadata(payload.get("payload")),
            "created_at": int(time.time()),
        }
        tx = self.engine.begin()
        tx.insert(AUDIT_TABLE, row)
        tx.commit()
        return {"event_id": event_id, "event_type": event_type, "decision": decision, "created_at": row["created_at"]}
