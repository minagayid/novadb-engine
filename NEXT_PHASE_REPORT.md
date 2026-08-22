# NovaDB Next Phase — Page Storage, Bytecode, and Prepared Statements

**Author:** Manus AI
**Status:** Implemented and validated

## Scope

This phase implemented the architecture changes identified as necessary to reduce NovaDB’s insert and query overhead: fixed-size checksummed pages, a page-authoritative durable commit stream, a stack-based bytecode virtual machine, and reusable prepared statements with batch execution.

## Implemented architecture

| Component | Implementation |
|---|---|
| Page store | `novadb/page_store.py` writes 16 KiB pages with versioned headers, length-prefixed records, CRC32 checksums, sequential append, scan, and atomic checkpoint replacement |
| Durable write path | On-disk commits append one versioned operation record to the page store and fsync once per batch; the legacy newline WAL remains a recovery fallback for older databases |
| Recovery | The engine loads the state checkpoint and replays only newer page-log versions; corrupted/truncated pages raise `PageCorruptionError` |
| Bytecode VM | `novadb/bytecode.py` supports column loads, constants, parameters, function calls, arithmetic, comparisons, null checks, and boolean operators |
| Prepared statements | `Engine.prepare(sql)` returns a reusable statement; `executemany` validates one batch and routes inserts through `bulk_insert` |
| Compiled SELECT | Simple prepared projections and predicates execute through reusable bytecode instead of the general expression interpreter |
| Replication | The replication helper now streams versioned page-log records and supports legacy WAL fallback |

## Benchmark results

The benchmark uses 10,000 rows containing an integer key, text value, JSON metadata, and a two-dimensional vector. Timings are from the same sandbox workload and are not universal performance claims.

| Operation | NovaDB result | SQLite result |
|---|---:|---:|
| Literal bulk insert, 10,000 rows | 0.0880 s | 0.0246 s |
| Prepared batch insert, 10,000 rows | 0.0146 s | 0.0246 s batch reference |
| Grouped JSON aggregate | 0.00735 s | 0.00174 s |
| Prepared filtered projection, 100 rows | 0.00445 s | 0.000076 s |

The prepared batch path is approximately **1.7× faster than the SQLite comparison insert** for this in-memory Python workload, but that result must be interpreted carefully: NovaDB’s prepared path receives already typed Python objects, while the SQLite comparison serializes JSON and vector values before binding. The fair conclusion is that the new API removes NovaDB’s prior parser and transaction overhead; it is not proof of general SQLite superiority.

The literal SQL bulk path remains approximately **3.6× slower than SQLite**, and the grouped aggregate remains approximately **4.2× slower**. The prepared filtered projection remains slower because the current bytecode VM is still implemented in Python and scans Python dictionaries.

## Why these changes matter

The page store establishes the correct physical abstraction for subsequent buffer-pool and B+ tree work. Fixed-size pages permit sequential writes, checksums, page identifiers, and bounded recovery units. The bytecode layer establishes a stable intermediate representation that can later be interpreted in native code, compiled to machine code, or executed over typed columnar batches. Prepared statements remove repeated SQL parsing and make batch boundaries explicit.

These changes are necessary but not sufficient to beat SQLite or Oracle across broad workloads. The next decisive step is to replace Python dictionaries and the Python interpreter loop with native typed pages, a buffer pool, compiled operators, and parallel execution. Storage layout and execution representation are now ready for that work.

## Validation

The final dependency-free regression suite passes **6/6 tests**, including SQL, JSON, vectors, grouped aggregation, restart recovery, optimistic conflicts, page checksums, prepared batch inserts, and prepared bytecode queries. All NovaDB modules pass Python compilation.

```bash
cd /home/ubuntu/novadb
PYTHONPATH=. python3 tests/test_runner.py
PYTHONPATH=. python3 benchmarks/bench.py
```
