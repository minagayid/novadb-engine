# NovaDB Throughput Optimization Report

**Author:** Manus AI
**Workload:** 10,000-row embedded batch insert plus grouped JSON aggregate
**Comparison:** Python standard-library SQLite binding
**Status:** Optimizations implemented and regression-tested

## What was slowing NovaDB

The original profile showed three dominant costs. Every SQL statement created a deep snapshot of the complete catalog, including all existing rows. Every inserted row scanned all previous rows to detect duplicate primary keys and rebuilt indexes repeatedly. The analytical path reparsed and reevaluated expressions row by row, while JSON path parsing repeated regular-expression work for every record.

The prototype also paid for work that was not required by the benchmark. Read-only queries cloned rows even though they never mutate them, and batch inserts created one operation object per row instead of one compact batch WAL operation.

## Implemented optimizations

| Optimization | Implementation | Main effect |
|---|---|---|
| Direct bulk insert | `Engine.execute` routes `INSERT ... VALUES (...), ...` through `bulk_insert` | Avoids cloning unrelated tables and commits one batch operation |
| Batch validation | `_prepare_many_rows` normalizes and validates all rows in one pass | Removes repeated transaction overhead |
| O(1) primary-key validation | Tables maintain `primary_keys` sets | Replaces an O(n) scan per inserted row |
| Incremental index maintenance | Secondary indexes are rebuilt only when present; primary-key state is updated incrementally | Avoids needless full-table work |
| Read-only fast path | `SELECT`, `SHOW TABLES`, and `EXPLAIN` operate on the live catalog without a write snapshot | Removes read-side deep copies |
| Cached expression compiler | `compile_expr` caches safe scalar expression functions | Removes repeated regular-expression parsing |
| Cached JSON paths | `_compile_json_path` caches parsed JSON path tokens | Removes repeated path-token parsing |
| One-pass grouped aggregation | `_fast_group_aggregate` accumulates aggregates without retaining per-group row lists | Reduces allocations and repeated scans |
| Optional NumPy aggregate path | Large single-key grouped aggregates use NumPy arrays when available | Enables vectorized numeric aggregation while retaining a pure-Python fallback |

## Measured result

The baseline values are from the initial implementation run. The optimized values are from the same benchmark script and same 10,000-row workload after the changes above.

| Measurement | Baseline | Optimized | Improvement |
|---|---:|---:|---:|
| NovaDB batch insert | 2.2324 s | 0.0886 s | 25.2× faster |
| NovaDB grouped aggregate | 0.1272 s | 0.0076 s | 16.8× faster |
| SQLite batch insert | 0.0244 s | 0.0246 s | Comparison reference |
| SQLite grouped aggregate | 0.0017 s | 0.0017 s | Comparison reference |

The optimized NovaDB run is approximately **3.6× slower than SQLite on batch insert** and **4.6× slower on the grouped aggregate** for this particular workload. It therefore does not yet beat SQLite, and no responsible conclusion about beating Oracle can be drawn from this microbenchmark.

## Why the remaining gap exists

NovaDB still uses a Python SQL parser, Python dictionaries for row storage, JSON serialization for WAL payloads, and a simple scan-oriented executor. SQLite is implemented in optimized native code with a mature page store, compiled virtual machine, highly tuned B-tree indexes, and decades of workload-specific engineering. Enterprise database throughput additionally depends on execution parallelism, buffer-pool behavior, lock management, statistics, optimizer quality, storage hardware, and workload shape.

The next throughput milestones should be implemented in this order:

1. Replace the JSON state image and dictionary rows with fixed-size pages, a buffer pool, checksummed WAL frames, and a durable B+ tree.
2. Add a compiled bytecode execution layer so predicates and projections do not dispatch through Python callables for each cell.
3. Store analytical columns in contiguous typed arrays and operate on batches rather than row dictionaries.
4. Add prepared statements and a binary protocol so the parser and JSON boundary are not in the hot path.
5. Add join algorithms, cardinality statistics, and a cost-based optimizer before comparing broader SQL workloads.
6. Add parallel scans and partitioned aggregation for multi-core analytical throughput.
7. Add durable vector indexes, concurrent MVCC, and consensus-backed replication for distributed workloads.

Those changes are foundational rather than cosmetic. They are required before attempting a fair comparison with native engines, and they matter more than adding additional SQL syntax to the current prototype.

## Validation status

The dependency-free regression suite passes all four tests covering relational SQL, JSON extraction, vector distance, grouped aggregation, restart recovery, optimistic conflict detection, and follower WAL replay. Python bytecode compilation also succeeds for every NovaDB module.
