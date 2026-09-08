from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .engine import Engine, NovaDBError
from .prompting import OpenAICompatiblePlanner, PromptSQLService


class NovaHandler(BaseHTTPRequestHandler):
    engine: Engine
    prompt_service: PromptSQLService | None = None
    token: str | None = None
    max_body_bytes = 1_048_576
    requests_per_minute = 60
    _rate_lock = threading.Lock()
    _request_windows: dict[str, deque[float]] = defaultdict(deque)

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
                "tables": sorted(self.engine.tables),
                "authentication": self.token is not None,
                "request_limit_per_minute": self.requests_per_minute,
        "prompt_to_sql": self.prompt_service is not None,
        "llm_planner": bool(self.prompt_service and self.prompt_service.planner),
        "prompt_guardrails": bool(self.prompt_service),
        "prompt_undo_redo": bool(self.prompt_service),
            })
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path not in {"/query", "/prompt", "/prompt/approve", "/prompt/undo", "/prompt/redo"}:
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Bearer")
            self.end_headers()
            return
        if not self._rate_allowed():
            self.send_response(429)
            self.send_header("Retry-After", "60")
            self.end_headers()
            return
        try:
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
            if self.path == "/query":
                sql = payload["sql"]
                result = self.engine.execute(sql)
                self._send(200, {"ok": True, "result": result})
                return
            if self.prompt_service is None:
                raise NovaDBError("Prompt-to-SQL is not configured")
            if self.path == "/prompt":
                self._send(200, {"ok": True, "plan": self.prompt_service.preview(payload["prompt"])})
                return
            if self.path == "/prompt/undo":
                self._send(200, self.prompt_service.undo(payload.get("execution_id")))
                return
            if self.path == "/prompt/redo":
                self._send(200, self.prompt_service.redo(payload.get("execution_id")))
                return
            self._send(200, self.prompt_service.approve(payload["plan_id"], payload.get("approved"), payload.get("sql_sha256")))
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._send(400, {"ok": False, "error": str(exc)})
        except NovaDBError as exc:
            self._send(422, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._send(500, {"ok": False, "error": str(exc)})

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
) -> None:
    if requests_per_minute < 1:
        raise ValueError("requests_per_minute must be positive")
    if max_body_bytes < 256:
        raise ValueError("max_body_bytes is too small")
    if host not in {"127.0.0.1", "localhost", "::1"} and not token:
        raise ValueError("a bearer token is required when binding NovaDB beyond loopback")
    engine = Engine(path)
    NovaHandler.engine = engine
    planner = OpenAICompatiblePlanner(llm_url, model=llm_model, token=llm_token) if llm_url else None
    NovaHandler.prompt_service = PromptSQLService(engine, planner=planner)
    NovaHandler.token = token
    NovaHandler.max_body_bytes = max_body_bytes
    NovaHandler.requests_per_minute = requests_per_minute
    NovaHandler._request_windows.clear()
    server = ThreadingHTTPServer((host, port), NovaHandler)
    server.daemon_threads = True
    print(f"NovaDB listening on http://{host}:{port}")
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
    args = parser.parse_args()
    serve(args.path, args.host, args.port, args.token, args.max_body_bytes, args.requests_per_minute, args.llm_url, args.llm_token, args.llm_model)


if __name__ == "__main__":
    main()
