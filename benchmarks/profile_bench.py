from __future__ import annotations

import cProfile
import io
import pstats
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.bench import ROWS, Engine  # noqa: E402
import json  # noqa: E402


def workload() -> None:
    db = Engine()
    db.execute("CREATE TABLE events (id INT PRIMARY KEY, name TEXT, meta JSON, embedding VECTOR)")
    values = ",".join("(%d, '%s', '%s', '%s')" % (i, name, json.dumps(meta).replace("'", "''"), json.dumps(vector)) for i, name, meta, vector in ROWS)
    db.execute("INSERT INTO events VALUES " + values)
    db.execute("SELECT JSON_EXTRACT(meta, '$.segment') AS segment, COUNT(*) AS n, AVG(id) AS avg_id FROM events GROUP BY segment ORDER BY segment")


if __name__ == "__main__":
    profiler = cProfile.Profile()
    profiler.enable()
    workload()
    profiler.disable()
    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(30)
    print(stream.getvalue())
