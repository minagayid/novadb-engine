from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .engine import Engine, NovaDBError


class NovaHandler(BaseHTTPRequestHandler):
    engine: Engine

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok", "version": self.engine.version, "tables": sorted(self.engine.tables)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/query":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            sql = payload["sql"]
            result = self.engine.execute(sql)
            self._send(200, {"ok": True, "result": result})
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._send(400, {"ok": False, "error": str(exc)})
        except NovaDBError as exc:
            self._send(422, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._send(500, {"ok": False, "error": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(path: str, host: str = "127.0.0.1", port: int = 8765) -> None:
    engine = Engine(path)
    NovaHandler.engine = engine
    server = ThreadingHTTPServer((host, port), NovaHandler)
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
    args = parser.parse_args()
    serve(args.path, args.host, args.port)


if __name__ == "__main__":
    main()
