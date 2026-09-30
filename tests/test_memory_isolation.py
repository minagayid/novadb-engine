"""Agent memory contract tests: scope, provenance, TTL and durable reopen."""
import tempfile
import unittest
from unittest.mock import patch

from novadb.engine import Engine
from novadb.memory import MemoryStore, MemoryValidationError


class MemoryIsolationTests(unittest.TestCase):
    def test_cross_session_replacement_is_rejected_without_destroying_source(self):
        engine = Engine()
        store = MemoryStore(engine)
        payload = {"memory_id": "same-id", "namespace": "eval", "session_id": "a",
                   "content": "synthetic protocol summary", "source": "fixture-001",
                   "metadata": {"source_version": "1", "confidence": "fixture-only"}}
        store.upsert(payload)
        for scope in [{"session_id": "b"}, {"namespace": "other"}]:
            with self.assertRaises(MemoryValidationError):
                store.upsert({**payload, **scope, "content": "replacement"})
        self.assertEqual(store.recent({"namespace": "eval", "session_id": "b"})["count"], 0)
        original = store.recent({"namespace": "eval", "session_id": "a"})["memories"][0]
        self.assertEqual(original["content"], payload["content"])
        self.assertEqual(original["metadata"]["source_version"], "1")
        engine.close()

    def test_ttl_and_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("novadb.memory.time.time", return_value=1000):
                engine = Engine(directory)
                store = MemoryStore(engine)
                store.upsert({"namespace": "eval", "session_id": "a", "content": "fixture",
                              "ttl_seconds": 60, "source": "test"})
                engine.close()
                reopened = Engine(directory)
                self.assertEqual(MemoryStore(reopened).recent({"namespace": "eval", "session_id": "a"})["count"], 1)
            with patch("novadb.memory.time.time", return_value=1060):
                self.assertEqual(MemoryStore(reopened).recent({"namespace": "eval", "session_id": "a"})["count"], 0)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
