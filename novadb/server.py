from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import ssl
import threading
import time
import uuid
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .engine import Engine, NovaDBError
from .memory import MemoryStore, MemoryValidationError, OpenAICompatibleEmbedder
from .prompting import OpenAICompatiblePlanner, PromptSQLService


class NovaHandler(BaseHTTPRequestHandler):
    engine: Engine
    prompt_service: PromptSQLService | None = None
    token: str | None = None
    max_body_bytes = 1_048_576
    requests_per_minute = 60
    audit_path: str | None = None
    memory_store: MemoryStore | None = None
    _rate_lock = threading.Lock()
    _request_windows: dict[str, deque[float]] = defaultdict(deque)

    def _request_id(self) -> str:
        supplied = self.headers.get("X-Request-ID", "")
        if re.fullmatch(r"[A-Za-z0-9._-]{1,80}", supplied):
            return supplied
        return uuid.uuid4().hex

    def _send(
        self,
        status: int,
        payload: Any,
        request_id: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        request_id = request_id or self._request_id()
        self._audit(status, request_id)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", request_id)
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _audit(self, status: int, request_id: str) -> None:
        if self.audit_path is None:
            return
        entry = {
            "timestamp": time.time(),
            "request_id": request_id,
            "method": self.command,
            "path": self.path,
            "status": status,
            "client": self.client_address[0],
        }
        try:
            with open(self.audit_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            # Audit persistence must not turn a safe request rejection into a
            # server crash, but the failure remains visible through local logs.
            return

    def _authorized(self) -> bool:
        if self.token is None:
            return True
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.token}"
        return hmac.compare_digest(supplied, expected)

    def _rate_allowed(self) -> bool:
        now = time.monotonic()
        key = self.client_address[0]
        with self._rate_lock:
            window = self._request_windows[key]
            while window and window[0] <= now - 60:
                window.popleft()
            if len(window) >= self.requests_per_minute:
                return False
            window.append(now)
            return True

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {
                "status": "ok",
                "version": self.engine.version,
                "table_count": len(self.engine.tables),
                "authentication": self.token is not None,
                "request_limit_per_minute": self.requests_per_minute,
                "prompt_to_sql": self.prompt_service is not None,
                "llm_planner": bool(self.prompt_service and self.prompt_service.planner),
                "prompt_guardrails": bool(self.prompt_service),
                "prompt_undo_redo": bool(self.prompt_service),
                "agent_memory": self.memory_store is not None,
                "memory_embeddings": bool(self.memory_store and self.memory_store.embedder),
            })
            return
        if self.path == "/memory/health":
            request_id = self._request_id()
            if not self._rate_allowed():
                self._send(429, {"ok": False, "error": "rate limit exceeded"}, request_id, {"Retry-After": "60"})
                return
            if not self._authorized():
                self._send(401, {"ok": False, "error": "authentication required"}, request_id)
                return
            self._send(200, {
                "ok": True,
                "service": "novadb-agent-memory",
                "storage": "durable-novadb",
                "embeddings": bool(self.memory_store and self.memory_store.embedder),
            }, request_id)
            return
        if self.path in {"/prompt/governance", "/prompt/history"}:
            request_id = self._request_id()
            if not self._rate_allowed():
                self._send(429, {"ok": False, "error": "rate limit exceeded"}, request_id, {"Retry-After": "60"})
                return
            if not self._authorized():
                self._send(401, {"ok": False, "error": "authentication required"}, request_id)
                return
            if self.prompt_service is None:
                self._send(422, {"ok": False, "error": "Prompt-to-SQL is not configured"}, request_id)
                return
            payload = self.prompt_service.governance() if self.path.endswith("governance") else self.prompt_service.history()
            self._send(200, {"ok": True, **payload}, request_id)
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path not in {"/query", "/prompt", "/prompt/approve", "/prompt/undo", "/prompt/redo", "/memory/upsert", "/memory/search", "/memory/recent", "/audit"}:
            self._send(404, {"error": "not found"})
            return
        request_id = self._request_id()
        if not self._rate_allowed():
            self._send(429, {"ok": False, "error": "rate limit exceeded"}, request_id, {"Retry-After": "60"})
            return
        if not self._authorized():
            self._send(401, {"ok": False, "error": "authentication required"}, request_id, {"WWW-Authenticate": "Bearer"})
            return
        try:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise ValueError("Content-Type must be application/json")
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise ValueError("Content-Length is required")
            length = int(raw_length)
            if length <= 0 or length > self.max_body_bytes:
                raise ValueError(f"request body exceeds {self.max_body_bytes} bytes")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("incomplete request body")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("request JSON must be an object")
            if self.path == "/query":
                sql = payload["sql"]
                if not isinstance(sql, str):
                    raise ValueError("sql must be a string")
                result = self.engine.execute(sql)
                self._send(200, {"ok": True, "result": result}, request_id)
                return
            if self.memory_store is None:
                raise NovaDBError("Agent memory is not configured")
            if self.path == "/memory/upsert":
                self._send(200, {"ok": True, "result": self.memory_store.upsert(payload)}, request_id)
                return
            if self.path == "/memory/search":
                self._send(200, {"ok": True, "result": self.memory_store.search(payload)}, request_id)
                return
            if self.path == "/memory/recent":
                self._send(200, {"ok": True, "result": self.memory_store.recent(payload)}, request_id)
                return
            if self.path == "/audit":
                self._send(200, {"ok": True, "result": self.memory_store.audit(payload)}, request_id)
                return
            if self.prompt_service is None:
                raise NovaDBError("Prompt-to-SQL is not configured")
            if self.path == "/prompt":
                prompt = payload["prompt"]
                if not isinstance(prompt, str):
                    raise ValueError("prompt must be a string")
                self._send(200, {"ok": True, "plan": self.prompt_service.preview(prompt)}, request_id)
                return
            if self.path == "/prompt/undo":
                self._send(200, self.prompt_service.undo(payload.get("execution_id")), request_id)
                return
            if self.path == "/prompt/redo":
                self._send(200, self.prompt_service.redo(payload.get("execution_id")), request_id)
                return
            self._send(200, self.prompt_service.approve(payload["plan_id"], payload.get("approved"), payload.get("sql_sha256")), request_id)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._send(400, {"ok": False, "error": str(exc), "request_id": request_id}, request_id)
        except NovaDBError as exc:
            self._send(422, {"ok": False, "error": str(exc), "request_id": request_id}, request_id)
        except Exception:
            # Do not turn database/parser internals into a remote information leak.
            self._send(500, {"ok": False, "error": "internal server error", "request_id": request_id}, request_id)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(
    path: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
    max_body_bytes: int = 1_048_576,
    requests_per_minute: int = 60,
    llm_url: str | None = None,
    llm_token: str | None = None,
    llm_model: str = "qwen3:1.7b",
    tls_cert: str | None = None,
    tls_key: str | None = None,
    embedding_url: str | None = None,
    embedding_token: str | None = None,
    embedding_model: str = "bge-m3",
    memory_default_ttl: int = 7 * 24 * 60 * 60,
) -> None:
    if requests_per_minute < 1:
        raise ValueError("requests_per_minute must be positive")
    if max_body_bytes < 256:
        raise ValueError("max_body_bytes is too small")
    if bool(tls_cert) != bool(tls_key):
        raise ValueError("tls_cert and tls_key must be provided together")
    if host not in {"127.0.0.1", "localhost", "::1"} and not token:
        raise ValueError("a bearer token is required when binding NovaDB beyond loopback")
    engine = Engine(path)
    NovaHandler.engine = engine
    planner = OpenAICompatiblePlanner(llm_url, model=llm_model, token=llm_token) if llm_url else None
    NovaHandler.prompt_service = PromptSQLService(engine, planner=planner)
    resolved_embedding_url = embedding_url or os.environ.get("NOVADB_EMBEDDING_URL") or llm_url
    resolved_embedding_token = embedding_token or os.environ.get("NOVADB_EMBEDDING_TOKEN") or llm_token
    resolved_embedding_model = os.environ.get("NOVADB_EMBEDDING_MODEL", embedding_model)
    embedder = OpenAICompatibleEmbedder(resolved_embedding_url, model=resolved_embedding_model, token=resolved_embedding_token) if resolved_embedding_url else None
    NovaHandler.memory_store = MemoryStore(engine, embedder=embedder, default_ttl_seconds=memory_default_ttl)
    NovaHandler.token = token
    NovaHandler.max_body_bytes = max_body_bytes
    NovaHandler.requests_per_minute = requests_per_minute
    NovaHandler.audit_path = None if path == ":memory:" else str(Path(path) / "http-audit.jsonl")
    NovaHandler._request_windows.clear()
    server = ThreadingHTTPServer((host, port), NovaHandler)
    server.daemon_threads = True
    scheme = "http"
    if tls_cert and tls_key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls_cert, tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    print(f"NovaDB listening on {scheme}://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        engine.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="NovaDB HTTP service")
    parser.add_argument("path", nargs="?", default="novadb-data")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=os.environ.get("NOVA_DB_TOKEN"), help="Bearer token; required off loopback")
    parser.add_argument("--max-body-bytes", type=int, default=1_048_576)
    parser.add_argument("--requests-per-minute", type=int, default=60)
    parser.add_argument("--llm-url", default=os.environ.get("NOVADB_LLM_URL"), help="OpenAI-compatible chat endpoint base URL")
    parser.add_argument("--llm-token", default=os.environ.get("NOVADB_LLM_TOKEN"), help="Optional planner bearer token")
    parser.add_argument("--llm-model", default=os.environ.get("NOVADB_LLM_MODEL", "qwen3:1.7b"))
    parser.add_argument("--tls-cert", help="TLS certificate PEM path")
    parser.add_argument("--tls-key", help="TLS private key PEM path")
    parser.add_argument("--embedding-url", default=os.environ.get("NOVADB_EMBEDDING_URL"), help="Optional OpenAI-compatible embeddings base URL")
    parser.add_argument("--embedding-token", default=os.environ.get("NOVADB_EMBEDDING_TOKEN"), help="Optional embedding bearer token")
    parser.add_argument("--embedding-model", default=os.environ.get("NOVADB_EMBEDDING_MODEL", "bge-m3"))
    parser.add_argument("--memory-default-ttl", type=int, default=int(os.environ.get("NOVADB_MEMORY_DEFAULT_TTL", 7 * 24 * 60 * 60)))
    args = parser.parse_args()
    serve(args.path, args.host, args.port, args.token, args.max_body_bytes, args.requests_per_minute, args.llm_url, args.llm_token, args.llm_model, args.tls_cert, args.tls_key, args.embedding_url, args.embedding_token, args.embedding_model, args.memory_default_ttl)


if __name__ == "__main__":
    main()
