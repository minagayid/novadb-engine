from __future__ import annotations
import tempfile
from pathlib import Path
import subprocess
import sys
from novadb import Engine, TransactionConflict, vector_distance
from novadb.catalog import read as read_catalog
from novadb.prompting import PromptPolicy, PromptSQLService
from novadb.replication import apply_records, stream_wal
from novadb.memory import MemoryStore
from tests.test_memory_api import test_http_memory_requires_auth_and_records_audit


def test_sql_json_vector_and_analytics():
    db = Engine()
    db.execute("CREATE TABLE docs (id INT PRIMARY KEY, name TEXT NOT NULL, meta JSON, embedding VECTOR)")
    db.execute("INSERT INTO docs VALUES (1, 'alpha', '{\"team\":\"red\"}', '[1,0]'), (2, 'beta', '{\"team\":\"blue\"}', '[0,1]'), (3, 'gamma', '{\"team\":\"red\"}', '[0.8,0.2]')")
    rows = db.execute("SELECT name, JSON_EXTRACT(meta, '$.team') AS team FROM docs WHERE id >= 2 ORDER BY id")
    assert rows == [{"name": "beta", "team": "blue"}, {"name": "gamma", "team": "red"}], rows
    stats = db.execute("SELECT JSON_EXTRACT(meta, '$.team') AS team, COUNT(*) AS n FROM docs GROUP BY team ORDER BY team")
    assert stats == [{"team": "blue", "n": 1}, {"team": "red", "n": 2}], stats
    nearest = db.execute("SELECT name, VECTOR_DISTANCE(embedding, '[1,0]') AS distance FROM docs ORDER BY distance LIMIT 1")[0]
    assert nearest["name"] == "alpha"
    assert abs(nearest["distance"]) < 1e-9
    assert abs(vector_distance([1, 0], [0, 1]) - 1.0) < 1e-9


def test_structured_and_unstructured_vectors_are_validated():
    db = Engine()
    db.execute("CREATE TABLE memories (id INT PRIMARY KEY, embedding VECTOR, document VECTOR_DOCUMENT)")
    db.execute("INSERT INTO memories VALUES (1, '[1,0]', '{\"text\":\"alpha note\",\"embedding\":[1,0],\"metadata\":{\"kind\":\"note\"}}')")
    row = db.execute("SELECT JSON_EXTRACT(document, '$.text') AS text, VECTOR_DISTANCE(document, '[1,0]') AS distance FROM memories LIMIT 1")[0]
    assert row["text"] == "alpha note", row
    assert abs(row["distance"]) < 1e-9, row
    try:
        db.execute("INSERT INTO memories VALUES (2, '[1,0,0]', '{\"text\":\"bad dimension\",\"embedding\":[1,0],\"metadata\":{}}')")
    except Exception as exc:
        assert "dimension mismatch" in str(exc).lower(), exc
    else:
        raise AssertionError("mixed structured vector dimensions must be rejected")
    try:
        db.execute("INSERT INTO memories VALUES (3, '[NaN,0]', '{\"text\":\"bad\",\"embedding\":[1,0],\"metadata\":{}}')")
    except Exception as exc:
        assert "finite" in str(exc).lower(), exc
    else:
        raise AssertionError("non-finite vector components must be rejected")


def test_durability_and_recovery():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "db"
        db = Engine(path)
        db.execute("CREATE TABLE users (id INT PRIMARY KEY, name TEXT)")
        db.execute("INSERT INTO users VALUES (1, 'Ada')")
        db.close()
        recovered = Engine(path)
        assert recovered.execute("SELECT * FROM users") == [{"id": 1, "name": "Ada"}]


def test_optimistic_conflict():
    db = Engine()
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, value INT)")
    tx1 = db.begin(); tx2 = db.begin()
    tx1.insert("t", {"id": 1, "value": 10}); tx1.commit()
    tx2.insert("t", {"id": 2, "value": 20})
    try:
        tx2.commit()
    except TransactionConflict:
        return
    raise AssertionError("expected TransactionConflict")


def test_replication_records():
    with tempfile.TemporaryDirectory() as tmp:
        leader_path = Path(tmp) / "leader"; follower_path = Path(tmp) / "follower"
        leader = Engine(leader_path)
        leader.execute("CREATE TABLE t (id INT PRIMARY KEY, value TEXT)")
        leader.execute("INSERT INTO t VALUES (1, 'replicated')")
        follower = Engine(follower_path)
        records = list(stream_wal(leader_path))
        assert apply_records(follower, iter(records)) == len(records)
        assert follower.execute("SELECT * FROM t") == [{"id": 1, "value": "replicated"}]


def test_page_store():
    from novadb.page_store import PageCorruptionError, PageStore
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "pages.dat"
        store = PageStore(path, page_size=512)
        records = [{"id": i, "value": "x" * 20} for i in range(50)]
        page_ids = store.append_records(records)
        assert page_ids
        assert list(store.iter_records()) == records
        raw = bytearray(path.read_bytes())
        raw[24] ^= 1
        path.write_bytes(raw)
        try:
            list(PageStore(path, page_size=512).iter_records())
        except PageCorruptionError:
            return
        raise AssertionError("expected checksum failure")


def test_page_store_rejects_torn_tail():
    from novadb.page_store import PageCorruptionError, PageStore
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "pages.dat"
        path.write_bytes(b"torn page")
        try:
            PageStore(path, page_size=512)
        except PageCorruptionError:
            return
        raise AssertionError("partial pages must fail closed")


def test_buffer_pool_and_durable_catalog():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "db"
        db = Engine(path)
        db.execute("CREATE TABLE notes (id INT PRIMARY KEY, body TEXT)")
        db.execute("CREATE INDEX notes_id ON notes (id)")
        db.execute("INSERT INTO notes VALUES (1, 'catalogued')")
        db.close()
        catalog = read_catalog(path / "catalog.json")
        assert catalog["engine_version"] == 3, catalog
        assert catalog["tables"]["notes"]["indexes"] == ["id"], catalog
        recovered = Engine(path)
        assert recovered.storage_status()["catalog_present"] is True
        assert recovered.storage_status()["page_count"] >= 1
        assert recovered.execute("SELECT * FROM notes LIMIT 1") == [{"id": 1, "body": "catalogued"}]
        assert recovered.buffer_pool is not None
        recovered.buffer_pool.read_page(0)
        recovered.buffer_pool.read_page(0)
        assert recovered.buffer_pool.stats().hits >= 1


def test_prompt_governance_exposes_limits_without_bypassing_approval():
    db = Engine()
    service = PromptSQLService(
        db,
        planner=lambda prompt, schema: {"sql": "SHOW TABLES", "explanation": "read-only"},
        policy=PromptPolicy(ttl_seconds=30, max_plans=4),
    )
    plan = service.preview("list tables")
    assert plan["policy_version"] == "prompt-sql/v2"
    assert plan["guardrails"]["max_plans"] == 4
    governance = service.governance()
    assert governance["planner_is_untrusted"] is True
    assert "exact hash approval" in governance["approval_boundary"]
    assert service.history() == {"undoable": [], "redoable": []}


def test_prompt_undo_redo_round_trip_is_version_guarded():
    db = Engine()
    db.execute("CREATE TABLE notes (id INT PRIMARY KEY, body TEXT)")
    service = PromptSQLService(
        db,
        planner=lambda prompt, schema: {"sql": f"INSERT INTO notes VALUES ({prompt}, 'note')"},
    )
    plan = service.preview("1")
    execution = service.approve(plan["plan_id"], True, plan["sql_sha256"])
    execution_id = execution["execution_id"]
    assert service.history()["undoable"][0]["execution_id"] == execution_id
    assert service.undo(execution_id)["status"] == "UNDONE"
    assert service.history()["redoable"][0]["execution_id"] == execution_id
    assert service.redo(execution_id)["status"] == "REDONE"
    db.execute("INSERT INTO notes VALUES (2, 'outside change')")
    try:
        service.undo(execution_id)
    except Exception as exc:
        assert "concurrent commit" in str(exc).lower(), exc
    else:
        raise AssertionError("undo must refuse to restore across an intervening commit")


def test_prepared_statements_and_bytecode():
    db = Engine()
    db.execute("CREATE TABLE items (id INT PRIMARY KEY, name TEXT, score INT)")
    statement = db.prepare("INSERT INTO items VALUES (?, ?, ?)")
    result = statement.executemany([(1, "a", 10), (2, "b", 20), (3, "c", 30)])
    assert result["count"] == 3
    query = db.prepare("SELECT name, score + ? AS boosted FROM items WHERE score >= ? ORDER BY boosted")
    assert query.execute((5, 15)) == [{"name": "b", "boosted": 25}, {"name": "c", "boosted": 35}]
    assert query.explain()["predicate_bytecode"]


def test_cost_based_joins():
    db = Engine()
    db.execute("CREATE TABLE users (id INT PRIMARY KEY, name TEXT)")
    db.execute("CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, amount INT)")
    db.prepare("INSERT INTO users VALUES (?, ?)").executemany([(1, "Ada"), (2, "Grace"), (3, "Linus")])
    db.prepare("INSERT INTO orders VALUES (?, ?, ?)").executemany([(10, 1, 100), (11, 1, 50), (12, 3, 75)])
    rows = db.execute("SELECT u.name, o.amount FROM users u JOIN orders o ON u.id = o.user_id WHERE o.amount >= 75 ORDER BY o.amount DESC")
    assert rows == [{"u.name": "Ada", "o.amount": 100}, {"u.name": "Linus", "o.amount": 75}], rows
    plan = db.explain("SELECT u.name, o.amount FROM users u JOIN orders o ON u.id = o.user_id")
    assert plan["operation"] == "HASH_JOIN"
    assert plan["details"]["strategy"] == "HASH_JOIN"


def test_explain_sql_uses_the_same_optimizer_as_engine_explain():
    db = Engine()
    db.execute("CREATE TABLE users (id INT PRIMARY KEY, name TEXT)")
    db.execute("CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, amount INT)")
    db.execute("INSERT INTO users VALUES (1, 'Ada'), (2, 'Grace')")
    db.execute("INSERT INTO orders VALUES (10, 1, 100), (11, 2, 50)")
    sql = "SELECT u.name, o.amount FROM users u JOIN orders o ON u.id = o.user_id"
    direct_plan = db.explain(sql)
    sql_result = db.execute("EXPLAIN " + sql)
    assert sql_result == [{"plan": direct_plan}], sql_result
    assert sql_result[0]["plan"]["operation"] == "HASH_JOIN"


def test_cli_explain_exposes_the_cost_based_plan():
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "explain.sql"
        script.write_text(
            "CREATE TABLE users (id INT PRIMARY KEY, name TEXT);"
            "CREATE TABLE orders (id INT PRIMARY KEY, user_id INT);"
            "EXPLAIN SELECT u.name, o.id FROM users u JOIN orders o ON u.id = o.user_id;",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, "-m", "novadb", ":memory:", "--file", str(script)],
            check=True,
            capture_output=True,
            text=True,
        )
        assert '"operation": "HASH_JOIN"' in result.stdout, result.stdout
        assert '"estimated_rows"' in result.stdout, result.stdout


def test_raft_consensus():
    from novadb.raft import NotLeaderError, RaftCluster
    applied = {node_id: [] for node_id in ["n1", "n2", "n3"]}
    cluster = RaftCluster(["n1", "n2", "n3"], {node_id: applied[node_id].append for node_id in applied}, election_timeout=3)
    cluster.tick(8)
    leader = cluster.leader()
    assert leader is not None, cluster.status()
    index = leader.propose({"op": "set", "key": "x", "value": 1})
    assert index == 0
    assert all(applied[node_id] == [{"op": "set", "key": "x", "value": 1}] for node_id in applied), applied
    followers = [node_id for node_id in cluster.node_ids if node_id != leader.node_id]
    cluster.partition([leader.node_id], followers)
    try:
        leader.propose({"op": "set", "key": "minority", "value": 1})
    except Exception:
        pass
    else:
        raise AssertionError("minority leader committed without a quorum")
    cluster.heal()
    assert cluster.leader() is not None


def test_replicated_engine():
    from novadb.raft import ReplicatedEngine
    engines = {node_id: Engine() for node_id in ["n1", "n2", "n3"]}
    replicated = ReplicatedEngine(engines, election_timeout=3)
    replicated.tick(8)
    replicated.execute("CREATE TABLE t (id INT PRIMARY KEY, value TEXT)")
    replicated.execute("INSERT INTO t VALUES (1, 'consensus')")
    for engine in engines.values():
        assert engine.execute("SELECT * FROM t") == [{"id": 1, "value": "consensus"}]


def test_btree_compaction_and_vector_index():
    from novadb import BPlusTree, VectorANNIndex

    tree = BPlusTree(max_keys=3)
    for value in range(40):
        tree.insert(value, value)
    assert tree.find(27) == [27]
    assert [key for key, _ in tree.items()] == list(range(40))

    index = VectorANNIndex(2)
    index.add("origin", [0, 0])
    index.add("x", [1, 0])
    assert index.search([0.1, 0], limit=1)[0].key == "origin"

    with tempfile.TemporaryDirectory() as tmp:
        db = Engine(tmp)
        db.execute("CREATE TABLE events (id INT PRIMARY KEY, value TEXT)")
        db.execute("CREATE INDEX events_id ON events (id)")
        db.execute("INSERT INTO events VALUES (1, 'one'), (2, 'two')")
        result = db.compact()
        assert result["status"] == "compacted"
        db.close()
        recovered = Engine(tmp)
        assert recovered.execute("SELECT * FROM events WHERE id = 2") == [{"id": 2, "value": "two"}]
        recovered.close()


def test_agent_memory_contract():
    store = MemoryStore(Engine(), embedder=lambda text: [1.0, 0.0] if "appointment" in text else [0.0, 1.0])
    stored = store.upsert({
        "namespace": "hospital",
        "session_id": "session-1",
        "kind": "care_summary",
        "content": "Patient requested an appointment with primary care.",
        "metadata": {"department": "access"},
    })
    result = store.search({"namespace": "hospital", "session_id": "session-1", "query_embedding": [1, 0], "limit": 5})
    assert stored["embedding_stored"] is True
    assert result["mode"] == "semantic"
    assert result["memories"][0]["memory_id"] == stored["memory_id"]
    assert store.recent({"namespace": "hospital", "session_id": "session-1"})["count"] == 1


if __name__ == "__main__":
    tests = [test_sql_json_vector_and_analytics, test_structured_and_unstructured_vectors_are_validated, test_durability_and_recovery, test_optimistic_conflict, test_replication_records, test_page_store, test_page_store_rejects_torn_tail, test_buffer_pool_and_durable_catalog, test_prompt_governance_exposes_limits_without_bypassing_approval, test_prompt_undo_redo_round_trip_is_version_guarded, test_prepared_statements_and_bytecode, test_cost_based_joins, test_explain_sql_uses_the_same_optimizer_as_engine_explain, test_cli_explain_exposes_the_cost_based_plan, test_raft_consensus, test_replicated_engine, test_btree_compaction_and_vector_index, test_agent_memory_contract, test_http_memory_requires_auth_and_records_audit]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} tests passed")
