from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from novadb import Engine, TransactionConflict, vector_distance
from novadb.replication import apply_records, stream_wal


def test_sql_json_vector_and_analytics() -> None:
    db = Engine()
    db.execute("CREATE TABLE docs (id INT PRIMARY KEY, name TEXT NOT NULL, meta JSON, embedding VECTOR)")
    db.execute("INSERT INTO docs VALUES (1, 'alpha', '{\"team\":\"red\"}', '[1,0]'), (2, 'beta', '{\"team\":\"blue\"}', '[0,1]'), (3, 'gamma', '{\"team\":\"red\"}', '[0.8,0.2]')")
    rows = db.execute("SELECT name, JSON_EXTRACT(meta, '$.team') AS team FROM docs WHERE id >= 2 ORDER BY id")
    assert rows == [{"name": "beta", "team": "blue"}, {"name": "gamma", "team": "red"}]
    stats = db.execute("SELECT JSON_EXTRACT(meta, '$.team') AS team, COUNT(*) AS n FROM docs GROUP BY team ORDER BY team")
    assert stats == [{"team": "blue", "n": 1}, {"team": "red", "n": 2}]
    assert db.execute("SELECT name FROM docs ORDER BY VECTOR_DISTANCE(embedding, '[1,0]') LIMIT 1")[0]["name"] == "alpha"
    assert vector_distance([1, 0], [0, 1]) == pytest.approx(1.0)


def test_durability_and_recovery() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "db"
        db = Engine(path)
        db.execute("CREATE TABLE users (id INT PRIMARY KEY, name TEXT)")
        db.execute("INSERT INTO users VALUES (1, 'Ada')")
        db.close()
        recovered = Engine(path)
        assert recovered.execute("SELECT * FROM users") == [{"id": 1, "name": "Ada"}]


def test_optimistic_conflict() -> None:
    db = Engine()
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, value INT)")
    tx1 = db.begin()
    tx2 = db.begin()
    tx1.insert("t", {"id": 1, "value": 10})
    tx1.commit()
    tx2.insert("t", {"id": 2, "value": 20})
    with pytest.raises(TransactionConflict):
        tx2.commit()


def test_replication_records() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        leader_path = Path(tmp) / "leader"
        follower_path = Path(tmp) / "follower"
        leader = Engine(leader_path)
        leader.execute("CREATE TABLE t (id INT PRIMARY KEY, value TEXT)")
        leader.execute("INSERT INTO t VALUES (1, 'replicated')")
        follower = Engine(follower_path)
        records = list(stream_wal(leader_path))
        assert apply_records(follower, iter(records)) == len(records)
        assert follower.execute("SELECT * FROM t") == [{"id": 1, "value": "replicated"}]
