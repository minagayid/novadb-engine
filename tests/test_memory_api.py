import json
import threading
import http.client

from novadb import Engine
from novadb.memory import MemoryStore
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
    return response.status, json.loads(data)


def test_memory_store_recent_and_semantic_retrieval():
    store = MemoryStore(Engine(), embedder=lambda text: [1.0, 0.0] if "appointment" in text else [0.0, 1.0])
    stored = store.upsert({
        "namespace": "hospital",
        "session_id": "session-1",
        "kind": "care_summary",
        "content": "Patient requested an appointment with primary care.",
        "metadata": {"department": "access"},
    })
    assert stored["embedding_stored"] is True
    result = store.search({"namespace": "hospital", "session_id": "session-1", "query_embedding": [1, 0], "limit": 5})
    assert result["mode"] == "semantic"
    assert result["memories"][0]["memory_id"] == stored["memory_id"]
    recent = store.recent({"namespace": "hospital", "session_id": "session-1"})
    assert recent["count"] == 1


def test_http_memory_requires_auth_and_records_audit():
    token = "memory-token-123"
    NovaHandler.engine = Engine()
    NovaHandler.token = token
    NovaHandler.memory_store = MemoryStore(NovaHandler.engine)
    NovaHandler.prompt_service = None
    NovaHandler.max_body_bytes = 1_048_576
    NovaHandler.requests_per_minute = 20
    NovaHandler._request_windows.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), NovaHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        status, _ = _request(port, "POST", "/memory/upsert", {"session_id": "s", "content": "redacted summary"})
        assert status == 401
        status, body = _request(port, "POST", "/memory/upsert", {"session_id": "s", "content": "redacted summary"}, token)
        assert status == 200
        assert body["result"]["embedding_stored"] is False
        status, body = _request(port, "POST", "/audit", {"session_id": "s", "event_type": "ROUTED", "decision": "review", "payload": {"department": "records"}}, token)
        assert status == 200
        assert body["result"]["event_type"] == "ROUTED"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
