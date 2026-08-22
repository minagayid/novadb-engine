from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from novadb import Engine  # noqa: E402


N = 10_000
ROWS = [(i, f"user-{i}", {"segment": "a" if i % 2 == 0 else "b"}, [float(i % 10), float((i * 3) % 10)]) for i in range(N)]


def measure(fn):
    start = time.perf_counter()
    result = fn()
    return {"seconds": time.perf_counter() - start, "result": result}


def bench_novadb():
    db = Engine()
    db.execute("CREATE TABLE events (id INT PRIMARY KEY, name TEXT, meta JSON, embedding VECTOR)")
    values = ",".join("(%d, '%s', '%s', '%s')" % (i, name, json.dumps(meta).replace("'", "''"), json.dumps(vector)) for i, name, meta, vector in ROWS)
    def do_insert():
        db.execute("INSERT INTO events VALUES " + values)
        return "ok"
    insert = measure(do_insert)
    query = measure(lambda: db.execute("SELECT JSON_EXTRACT(meta, '$.segment') AS segment, COUNT(*) AS n, AVG(id) AS avg_id FROM events GROUP BY segment ORDER BY segment"))
    prepared_db = Engine()
    prepared_db.execute("CREATE TABLE events (id INT PRIMARY KEY, name TEXT, meta JSON, embedding VECTOR)")
    prepared_insert_raw = measure(lambda: prepared_db.prepare("INSERT INTO events VALUES (?, ?, ?, ?)").executemany(ROWS))
    prepared_insert = {"seconds": prepared_insert_raw["seconds"], "count": prepared_insert_raw["result"]["count"]}
    prepared_query = measure(lambda: prepared_db.prepare("SELECT name, id + ? AS boosted FROM events WHERE id >= ? ORDER BY boosted LIMIT 100").execute((5, 9900)))
    return {"insert": insert, "aggregate": query, "prepared_insert": prepared_insert, "prepared_query": {"seconds": prepared_query["seconds"], "rows": len(prepared_query["result"]), "first": prepared_query["result"][0]}}


def bench_sqlite():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, name TEXT, meta TEXT, embedding TEXT)")
    def do_insert():
        db.executemany("INSERT INTO events VALUES (?, ?, ?, ?)", [(i, name, json.dumps(meta), json.dumps(vector)) for i, name, meta, vector in ROWS])
        return "ok"
    insert = measure(do_insert)
    query = measure(lambda: db.execute("SELECT json_extract(meta, '$.segment') AS segment, COUNT(*) AS n, AVG(id) AS avg_id FROM events GROUP BY segment ORDER BY segment").fetchall())
    prepared_query = measure(lambda: db.execute("SELECT name, id + ? AS boosted FROM events WHERE id >= ? ORDER BY boosted LIMIT 100", (5, 9900)).fetchall())
    return {"insert": insert, "aggregate": query, "prepared_query": {"seconds": prepared_query["seconds"], "rows": len(prepared_query["result"]), "first": prepared_query["result"][0]}}


if __name__ == "__main__":
    print(json.dumps({"rows": N, "novadb": bench_novadb(), "sqlite": bench_sqlite()}, indent=2, default=str))
