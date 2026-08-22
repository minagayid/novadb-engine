# NovaDB

NovaDB is a compact, dependency-free database-engine prototype designed around a unified goal: combine the zero-administration experience of an embedded database with SQL analytics, JSON and vector data, durable transactions, and a clear path toward distributed operation.

It is **not** an honest claim to replace Oracle across every enterprise workload. Oracle-class systems include decades of engineering around distributed deployment, optimizer breadth, security, tooling, operations, and compatibility. NovaDB instead makes the competitive target measurable: lower setup friction, a small embeddable implementation, an integrated data model for relational/JSON/vector workloads, and a transparent architecture that can be extended without hiding trade-offs.

## Current feature set

| Capability | Prototype status | Implementation |
|---|---:|---|
| Embedded, zero-admin runtime | Working | `Engine(':memory:')` or a local database directory |
| SQL DDL/DML | Working | `CREATE TABLE`, `CREATE INDEX`, `INSERT`, `SELECT`, `UPDATE`, `DELETE` |
| Analytical SQL | Working | `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, `GROUP BY`, `ORDER BY`, `LIMIT` |
| JSON data | Working | Native JSON values and `JSON_EXTRACT(value, '$.path')` |
| Vector data | Working | Vector values and cosine/L2 distance functions |
| Durability | Working | Append-only WAL, fsync before commit, checkpointed state |
| Transaction isolation | Working prototype | Snapshot transactions with optimistic commit conflict detection |
| HTTP access | Working prototype | `GET /health` and `POST /query` |
| Replication | Reference primitive | Ordered WAL stream and follower replay helper |
| Enterprise hardening | Not complete | Authentication, authorization, encryption, quotas, auditing, and production consensus are future work |

## Quick start

The engine requires only Python 3.11 or newer and has no third-party runtime dependencies.

```bash
cd /home/ubuntu/novadb
PYTHONPATH=. python3 -m novadb /tmp/novadb-demo --sql "CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL, profile JSON)"
PYTHONPATH=. python3 -m novadb /tmp/novadb-demo --sql "INSERT INTO users VALUES (1, 'Ada', '{\"role\":\"admin\"}')"
PYTHONPATH=. python3 -m novadb /tmp/novadb-demo --sql "SELECT name, JSON_EXTRACT(profile, '$.role') AS role FROM users"
```

For an interactive shell:

```bash
PYTHONPATH=. python3 -m novadb /tmp/novadb-demo
```

For the local HTTP service:

```bash
PYTHONPATH=. python3 -m novadb.server /tmp/novadb-demo --port 8765
curl http://127.0.0.1:8765/health
curl -X POST http://127.0.0.1:8765/query \
  -H 'Content-Type: application/json' \
  -d '{"sql":"SELECT * FROM users"}'
```

## Example: relational, JSON, and vector query

```sql
CREATE TABLE documents (
    id INT PRIMARY KEY,
    title TEXT NOT NULL,
    metadata JSON,
    embedding VECTOR
);

INSERT INTO documents VALUES
    (1, 'alpha', '{"team":"red","tier":2}', '[1,0]'),
    (2, 'beta',  '{"team":"blue","tier":1}', '[0,1]'),
    (3, 'gamma', '{"team":"red","tier":1}', '[0.8,0.2]');

SELECT
    title,
    JSON_EXTRACT(metadata, '$.team') AS team,
    VECTOR_DISTANCE(embedding, '[1,0]') AS distance
FROM documents
ORDER BY distance
LIMIT 2;
```

## Architecture

NovaDB intentionally separates the engine into small layers. The SQL layer parses a bounded SQL grammar and evaluates expressions through a safe, non-`eval` expression interpreter. The transaction layer clones a snapshot of the catalog, applies changes privately, and commits only if the engine version is unchanged. The storage layer records committed operations in a newline-delimited WAL before publishing the new snapshot. Checkpoints atomically replace `state.json` and truncate the WAL after a durable state image has been written.

The design makes the distributed extension explicit. A leader can expose ordered WAL records, while a follower can apply them in version order. This is useful as a reference protocol, but it does not pretend to provide consensus, fencing, quorum durability, split-brain prevention, or online re-sharding. Those are separate engineering obligations.

| Layer | Current mechanism | Next serious step |
|---|---|---|
| SQL | Bounded parser and expression evaluator | Cost-based optimizer, joins, window functions, prepared statements |
| Execution | Row scans with aggregate pipeline | Columnar batches, late materialization, parallel operators |
| Storage | JSON state image plus append-only WAL | Slotted pages, B+ trees, buffer pool, compaction |
| Transactions | Snapshot copy plus optimistic version check | MVCC timestamps, lock manager, serializable validation |
| Indexes | In-memory equality index metadata | Durable B+ tree and vector ANN index |
| Replication | Ordered WAL replay helper | Raft-like consensus, leases, quorum commit |
| Service | Threaded HTTP JSON endpoint | Binary protocol, authentication, quotas, observability |

## Correctness and durability model

A committed write is appended to the WAL, flushed, and then made visible to the engine snapshot. A transaction that begins against version `v` cannot commit after another transaction has advanced the database to a later version; it receives `TransactionConflict` and must be retried by the caller. This is deliberately conservative and easy to reason about, but it copies the whole catalog for each transaction and is therefore not suitable for high-concurrency production workloads.

The recovery path loads the last checkpoint and replays only WAL records newer than its version. Checkpoint replacement uses a temporary file followed by an atomic rename. A production engine would additionally need checksummed WAL frames, torn-write detection, fsync policy controls, crash-injection tests, and a formally specified recovery protocol.

## Throughput optimizations

The engine now includes a direct atomic bulk-insert path, one-pass batch validation, O(1) primary-key checks, incremental index maintenance, read-only execution without catalog cloning, cached safe expression compilation, cached JSON path tokens, and a vectorized grouped-aggregation path when NumPy is available. These changes reduced the included NovaDB workload from 2.2324 seconds to 0.0886 seconds for 10,000-row insertion and from 0.1272 seconds to 0.0076 seconds for the grouped aggregate. The detailed before/after analysis is in [`OPTIMIZATION_REPORT.md`](OPTIMIZATION_REPORT.md). The optimized prototype is still slower than SQLite on this workload, so these numbers are evidence of progress rather than a claim of universal superiority.

## Page storage, bytecode, and prepared statements

The next-phase architecture adds `PageStore`, a fixed-size 16 KiB page file with versioned headers, length-prefixed records, CRC32 checksums, sequential append, and atomic checkpoint support. On-disk commits use the page log as the authoritative versioned commit stream; the legacy newline WAL remains available as a compatibility fallback. The design provides the physical boundary needed for a future buffer pool and durable B+ tree.

`Engine.prepare(sql)` returns a reusable prepared statement. `executemany` performs one validated batch insert through the direct bulk loader. Simple prepared `SELECT` projections and predicates are compiled into a small stack-machine bytecode program with column loads, constants, parameters, function calls, arithmetic, comparisons, null checks, and boolean operators.

```python
from novadb import Engine

db = Engine(\":memory:\")
db.execute(\"CREATE TABLE items (id INT PRIMARY KEY, name TEXT, score INT)\")
db.prepare(\"INSERT INTO items VALUES (?, ?, ?)\").executemany([(1, \"a\", 10), (2, \"b\", 20)])
rows = db.prepare(\"SELECT name, score + ? AS boosted FROM items WHERE score >= ?\").execute((5, 15))
```

The next-phase benchmark reports **0.0146 seconds** for a prepared 10,000-row batch insert, **0.0880 seconds** for literal SQL bulk insert, **0.00735 seconds** for grouped aggregation, and **0.00445 seconds** for a prepared filtered projection. These are prototype measurements on one sandbox workload, not universal superiority claims.

## Distributed consensus and cost-based joins

NovaDB now includes a deterministic **Raft-style reference layer** in `novadb/raft.py`. It implements follower/candidate/leader roles, term and vote persistence, RequestVote, AppendEntries, conflict backtracking, quorum commit, ordered state-machine application, partition injection, and a `ReplicatedEngine` adapter that applies committed SQL commands to multiple NovaDB engines. Use it as a testable control-plane foundation; production deployment still requires a real network transport, durable quorum acknowledgements, membership changes, snapshots, deduplication, security, and crash-injection testing.

The new `novadb/optimizer.py` module provides cost-based plans for inner equi-joins. It estimates cardinality from relation sizes and distinct key counts, compares hash join with nested-loop cost, chooses the smaller hash build side, preserves qualified names, and exposes plan trees through `EXPLAIN`.

```sql
EXPLAIN SELECT u.name, o.amount
FROM users u JOIN orders o ON u.id = o.user_id
WHERE o.amount >= 75;
```

The reusable agent workflow for extending NovaDB is available at `/home/ubuntu/skills/novadb-engineering/SKILL.md`. The detailed implementation report is in [`DISTRIBUTED_OPTIMIZER_REPORT.md`](DISTRIBUTED_OPTIMIZER_REPORT.md).

## Validation

The repository includes a dependency-free regression runner covering SQL execution, JSON extraction, vector distance, grouped aggregation, durability and recovery, optimistic conflicts, WAL follower replay, page checksums, prepared batch inserts, bytecode queries, multi-table joins, optimizer plan selection, Raft election and quorum commit, minority partition rejection, and replicated NovaDB commands.

```bash
cd /home/ubuntu/novadb
PYTHONPATH=. python3 tests/test_runner.py
```

A reproducible comparison workload is available at `benchmarks/bench.py`. It compares the prototype with the Python standard library’s SQLite binding for a 10,000-row batch insert and grouped aggregate. The output is intentionally a measurement artifact, not a marketing claim: the workload, machine, Python build, durability mode, and data shape all affect the result.

```bash
cd /home/ubuntu/novadb
PYTHONPATH=. python3 benchmarks/bench.py
```

## Roadmap toward an enterprise-grade engine

The next milestone is a real page-oriented storage manager with checksummed pages, a buffer pool, a durable catalog, and B+ tree indexes. The following milestone is a vectorized execution engine with columnar batches, joins, statistics, and a cost-based optimizer. Only after those foundations are stable should the system add MVCC timestamps, lock management, prepared statements, security, wire-protocol compatibility, and consensus-backed replication.

The phrase “better than Oracle” should therefore be evaluated by workload and dimension. NovaDB can plausibly aim to be better for a narrow set of developer-centric embedded workloads because it is smaller and more integrated. It should not claim superiority for enterprise breadth, operational maturity, or global distributed guarantees until those properties are implemented and independently measured.

## License and status

This repository is an experimental prototype intended for evaluation and extension. It is not production-ready and should not be used as the sole store for irreplaceable data.
