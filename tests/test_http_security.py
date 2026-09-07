import http.client
import json
import threading

from novadb import Engine
from novadb.prompting import PromptSQLService
from novadb.server import NovaHandler, ThreadingHTTPServer


def _request(port, method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = None if body is None else json.dumps(body).encode()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request(method, path, payload, headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data


def test_http_auth_and_limits():
    token = "test-token-123"
    NovaHandler.engine = Engine()
    NovaHandler.token = token
    NovaHandler.max_body_bytes = 256
    NovaHandler.requests_per_minute = 10
    NovaHandler._request_windows.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), NovaHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        status, _ = _request(port, "POST", "/query", {"sql": "SHOW TABLES"})
        assert status == 401
        status, health = _request(port, "GET", "/health")
        assert status == 200
        assert json.loads(health)["authentication"] is True
        status, body = _request(port, "POST", "/query", {"sql": "SHOW TABLES"}, token)
        assert status == 200
        assert json.loads(body)["ok"] is True
        status, _ = _request(port, "POST", "/query", {"sql": "x" * 400}, token)
        assert status == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_http_prompt_approval():
    token = "prompt-token-123"
    engine = Engine()
    engine.execute("CREATE TABLE notes (id INT PRIMARY KEY, body TEXT)")
    NovaHandler.engine = engine
    NovaHandler.token = token
    NovaHandler.prompt_service = PromptSQLService(engine, planner=lambda prompt, schema: {"sql": "SHOW TABLES", "explanation": "Safe read-only check."})
    NovaHandler.max_body_bytes = 1_048_576
    NovaHandler.requests_per_minute = 20
    NovaHandler._request_windows.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), NovaHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        status, raw = _request(port, "POST", "/prompt", {"prompt": "list the tables"}, token)
        assert status == 200
        plan = json.loads(raw)["plan"]
        assert plan["status"] == "PENDING_APPROVAL"
        status, raw = _request(port, "POST", "/prompt/approve", {"plan_id": plan["plan_id"], "approved": True, "sql_sha256": plan["sql_sha256"]}, token)
        assert status == 200
        assert json.loads(raw)["status"] == "EXECUTED"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
