# NovaDB Next Architecture

## Objective

The next optimization phase targets the current bottlenecks that prevent NovaDB from challenging native engines: Python row dictionaries, repeated SQL parsing, whole-catalog transaction snapshots, and JSON state-image persistence. The design moves writes toward fixed-size pages, moves repeated query work toward reusable bytecode, and gives callers a prepared-statement API that can keep parsing out of hot loops.

## Page-oriented storage

NovaDB will use 16 KiB pages with a compact binary header containing a magic value, format version, page identifier, record count, payload length, and CRC32. Rows are encoded as length-prefixed records inside pages. A batch insert packs many rows into a small number of pages and performs one durability flush for the batch instead of repeatedly rewriting a large state image.

The page store is append-oriented for the prototype. It exposes append, scan, and checkpoint operations. The append path is designed to evolve into a buffer pool: dirty pages can be accumulated in memory, sorted by file position, and flushed in larger sequential writes. The current state image remains available as a compatibility fallback while the page file becomes the durable fast path for bulk inserts.

## Compiled bytecode execution

A prepared query is compiled into a small stack machine. Instructions include `LOAD` for a column, `CONST` for a literal, `CALL` for supported functions, arithmetic operators, comparisons, and boolean operators. The bytecode is compiled once and run for every row, avoiding repeated regular-expression parsing and repeated expression-shape dispatch. The design is intentionally small so the instruction set can later be lowered to native code or a vectorized batch executor.

## Prepared statements

`Engine.prepare(sql)` returns a reusable statement. Positional `?` parameters are bound without string concatenation in the common batch-insert path. `executemany` validates and appends a complete batch through the direct bulk loader, keeping one parse, one validation pass, one page-pack operation, and one WAL durability boundary for the batch.

## Expected performance effect

| Change | Removes | Enables |
|---|---|---|
| Fixed-size page packing | Full JSON state-image rewrite and object-heavy persistence | Sequential writes, buffer pooling, page checksums |
| Direct bulk page append | Per-row transaction and fsync overhead | Large durable batches |
| Bytecode cache | Repeated expression parsing and dispatch | Reusable compiled query plans |
| Prepared statements | Repeated SQL parse and literal construction | High-rate point writes and parameterized reads |
| Incremental metadata | Whole-catalog copies and repeated index rebuilds | Lower write amplification |

## Explicit boundary

These changes improve NovaDB’s architecture and measured hot paths, but they do not automatically make a Python prototype faster than SQLite or Oracle. The remaining native-code gap requires a buffer pool, B+ trees, typed columnar vectors, compiled joins, parallel operators, and a mature cost-based optimizer. The benchmark will continue to report measured results rather than infer superiority from the architecture alone.
