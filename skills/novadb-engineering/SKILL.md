---
name: novadb-engineering
description: Build, extend, optimize, validate, and publish database-engine prototypes such as NovaDB. Use for page-oriented storage, WAL/MVCC, Raft-style consensus, query planners, cost models, join algorithms, bytecode execution, prepared statements, benchmark-driven optimization, and GitHub release workflows.
---

# NovaDB Engineering

## Overview

Use this skill to turn a database-engine request into a bounded, measurable implementation. Prefer explicit storage, transaction, execution, consensus, optimizer, and validation boundaries over broad feature claims. Treat “better than SQLite/Oracle” as a workload-specific hypothesis that must be tested, not as an architectural conclusion.

## Workflow decision tree

1. Inspect the repository, runtime, tests, benchmark, current persistence format, and Git status before changing code.
2. Classify the requested work:
   - **Storage or durability:** follow the page/WAL workflow.
   - **Distributed fault tolerance:** follow the Raft-style consensus workflow.
   - **Complex SQL:** follow the optimizer and joins workflow.
   - **Throughput:** profile first, then change one hot path at a time.
   - **Publication:** follow the validation and GitHub workflow only after tests pass.
3. Keep compatibility fallbacks when changing on-disk formats. Add migration/recovery tests before deleting the old path.
4. Distinguish prototype behavior from production guarantees in code comments, reports, and repository documentation.

## Storage and durability workflow

1. Define the page size, header fields, record framing, checksum, page identifiers, and recovery rules before implementation.
2. Implement append, scan, corruption detection, and atomic checkpoint replacement in a standalone page-store module.
3. Add a versioned operation record around each committed batch. Flush the durable record before publishing visible state.
4. Make recovery load the checkpoint and replay only records newer than its version. Retain a legacy WAL fallback during migration.
5. Test truncation, checksum failure, duplicate replay, checkpoint interruption, empty files, large records, and restart recovery.
6. Only then add a buffer pool, free-page management, B+ trees, compaction, and page-level MVCC.

A minimal durable commit sequence is:

```text
validate batch -> append versioned record -> flush/fsync -> publish state -> advance commit version
```

Never call a page log “replication-safe” until record ordering, checksums, duplicate handling, crash recovery, and follower catch-up are tested.

## Raft-style consensus workflow

Use this workflow when the user asks for fault-tolerant distributed writes. Implement a narrow, testable replicated state machine before adding sharding or distributed SQL.

### Define the protocol

Specify the node roles (`follower`, `candidate`, `leader`), persistent state (`current_term`, `voted_for`, log entries), volatile state (`commit_index`, `last_applied`), leader state (`next_index`, `match_index`), election timeout range, heartbeat interval, message schema, and client behavior when a node is not leader.

### Implement in this order

1. Add deterministic logical clocks or injectable timers so election tests do not depend on wall-clock sleeps.
2. Implement term comparison and step-down rules. A node observing a higher term must update its term, clear its vote when required by the protocol, and become a follower.
3. Implement `RequestVote` with the up-to-date-log rule and one-vote-per-term rule.
4. Implement `AppendEntries` heartbeats and log replication, including previous-log-term matching, conflict truncation, and leader commit advancement only for entries from the current term.
5. Apply committed log entries to a deterministic NovaDB state-machine adapter in log order.
6. Add client proposals that succeed only on the leader and return the log index/term needed for acknowledgement.
7. Persist protocol state before sending messages that rely on it. Do not acknowledge a write as committed merely because the leader appended it locally.

### Consensus quality gates

| Gate | Required evidence |
|---|---|
| Election | One leader emerges in a healthy odd-sized cluster |
| Safety | At most one leader is accepted per term; committed entries are never overwritten |
| Failover | A majority can elect a replacement after leader loss |
| Catch-up | A restarted or lagging follower converges through backtracking |
| Partition | A minority cannot commit new writes; the majority can continue |
| Durability | Restarted nodes recover term, vote, log, and applied state |
| Client semantics | Redirect or reject writes sent to followers; duplicate client requests are idempotent |

Call the result “Raft-style” or “reference consensus” until it has durable message transport, quorum tests, clock-fault tests, crash injection, membership-change rules, and operational observability. Do not claim production-grade consensus from an in-memory simulation.

## Cost-based optimizer and joins workflow

1. Parse the query into a logical representation containing relations, filters, projections, join predicates, grouping, ordering, and limits.
2. Collect or maintain table statistics: row count, distinct values, null fraction, min/max, and index availability. Mark estimates as unknown when statistics are missing.
3. Generate candidate physical plans. At minimum compare sequential scan, indexed lookup, nested-loop join, and hash join. Include build-side memory and materialization costs.
4. Estimate selectivity with explicit formulas and guardrails. A simple equi-join estimate is:

```text
estimated_join_rows = left_rows * right_rows / max(left_distinct, right_distinct, 1)
```

Clamp estimates to non-negative values and preserve a confidence/unknown flag rather than manufacturing precision.

5. Choose join order using dynamic programming for a small relation count or a greedy strategy for larger queries. Prefer selective filters early and build the smaller hash side when memory permits.
6. Implement hash join with a keyed build table and nested-loop join as a correctness fallback for small inputs or non-hashable keys.
7. Preserve qualified column names (`alias.column`) to avoid collisions. Add tests for duplicate column names, NULL join keys, empty inputs, repeated keys, aliases, filters, ordering, and limits.
8. Expose `EXPLAIN` with operation, estimated rows, cost, chosen strategy, and child plans. Never hide a heuristic choice behind a claim of optimality.

### Join correctness rule

For an inner equi-join, a row matches only when both key values are equal and non-missing according to the engine’s NULL semantics. Test the physical join algorithms directly and test planner output separately from result correctness.

## Bytecode and prepared statements workflow

1. Compile reusable expressions into a small stack VM with explicit instructions for column loads, constants, parameters, function calls, arithmetic, comparisons, NULL checks, and Boolean operations.
2. Give each prepared statement a stable parameter count and global parameter numbering across projections and predicates.
3. Implement `prepare(sql)`, `execute(params)`, and `executemany(rows)`. Route one-VALUES-group inserts through one batch validation, one page/WAL commit, and one durability boundary.
4. Keep a safe fallback for unsupported SQL constructs. Never substitute user parameters through unsafe string concatenation when a typed path is available.
5. Add `EXPLAIN` output for bytecode and tests for wrong arity, quoted literals, arithmetic, JSON paths, vectors, predicates, and repeated execution.

## Benchmark-driven optimization workflow

1. Freeze a reproducible workload and record machine/runtime, durability mode, row count, data types, and query shape.
2. Profile before changing code. Record cumulative time and allocation-heavy functions.
3. Implement one optimization family at a time: batch writes, metadata/index maintenance, read-only scans, expression compilation, JSON path caching, vectorized aggregation, page appends, then prepared statements.
4. Run correctness tests after every change. Reject any speedup that changes query results, recovery behavior, or transaction semantics.
5. Report baseline, optimized result, comparison result, workload caveats, and the remaining bottleneck. Benchmark numbers from one machine are evidence for that workload only.

## Validation and release workflow

1. Run syntax/compile checks, unit tests, recovery tests, consensus fault tests, optimizer plan tests, and the benchmark.
2. Inspect `git diff --check`, ensure generated caches and database files are ignored, and update the README plus a focused implementation report.
3. Commit with a message describing the architectural change. Keep the working tree clean.
4. When the user explicitly requests publication, use GitHub CLI to create or update the repository with the requested visibility. Verify repository URL, visibility, branch, commit, and remote tracking after pushing.
5. Deliver the repository URL and the key reports. State clearly which guarantees are implemented, which are reference-only, and which remain future work.

## Reusable output structure

Use this structure for future database-engine deliverables:

1. **Scope and competitive hypothesis.** Define the workload and what “better” means.
2. **Architecture.** Describe storage, transactions, execution, optimizer, consensus, and interfaces.
3. **Implementation.** Name the modules and compatibility paths changed.
4. **Validation.** Give tests, failure cases, benchmark inputs, and measured results.
5. **Limitations and roadmap.** Identify unimplemented production obligations.
6. **Publication.** Provide the repository URL, commit, branch, and reproducibility commands.

Do not include README files, changelogs, or user-facing project artifacts inside this skill. Keep this skill focused on agent instructions and decision gates.
