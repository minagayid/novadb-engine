from __future__ import annotations
import tempfile
from pathlib import Path
from novadb import Engine, TransactionConflict, vector_distance
from novadb.replication import apply_records, stream_wal


def test_sql_json_vector_and_analytics():
    db = Engine()
    db.execute("CREATE TABLE docs (id INT PRIMARY KEY, name TEXT NOT NULL, meta JSON, embedding VECTOR)")
    db.execute("INSERT INTO docs VALUES (1, 'alpha', '{\"team\":\"red\"}', '[1,0]'), (2, 'beta', '{\"team\":\"blue\"}', '[0,1]'), (3, 'gamma', '{\"team\":\"red\"}', '[0.8,0.2]')")
    rows = db.execute("SELECT name, JSON_EXTRACT(meta, '$.team') AS team FROM docs WHERE id >= 2 ORDER BY id")
    assert rows == [{"name": "beta", "team": "blue"}, {"name": "gamma", "team": "red"}], rows
    stats = db.execute("SELECT JSON_EXTRACT(meta, '$.team') AS team, COUNT(*) AS n FROM docs GROUP BY team ORDER BY team")
    assert stats == [{"team": "blue", "n": 1}, {"team": "red", "n": 2}], stats
    assert db.execute("SELECT name FROM docs ORDER BY VECTOR_DISTANCE(embedding, '[1,0]') LIMIT 1")[0]["name"] == "alpha"
    assert abs(vector_distance([1, 0], [0, 1]) - 1.0) < 1e-9


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


def test_prepared_statements_and_bytecode():
    db = Engine()
    db.execute("CREATE TABLE items (id INT PRIMARY KEY, name TEXT, score INT)")
    statement = db.prepare("INSERT INTO items VALUES (?, ?, ?)")
    result = statement.executemany([(1, "a", 10), (2, "b", 20), (3, "c", 30)])
    assert result["count"] == 3
    query = db.prepare("SELECT name, score + ? AS boosted FROM items WHERE score >= ? ORDER BY boosted")
    assert query.execute((5, 15)) == [{"name": "b", "boosted": 25}, {"name": "c", "boosted": 35}]
    assert query.explain()["predicate_bytecode"]


if __name__ == "__main__":
    tests = [test_sql_json_vector_and_analytics, test_durability_and_recovery, test_optimistic_conflict, test_replication_records, test_page_store, test_prepared_statements_and_bytecode]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} tests passed")
