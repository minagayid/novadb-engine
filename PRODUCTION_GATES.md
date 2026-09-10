# NovaDB production gates

The repository now contains a fail-closed evidence evaluator:

```powershell
python -m novadb --production-gate production-gate-manifest.example.json --json
```

The evaluator is a release-readiness aid, not an approval authority. It binds
the result to a manifest and digest, reports each missing gate, and always
leaves `production_approved` false. A passing unit test is not treated as a
substitute for an independent operational drill or security review.

## Gate plan

| Gate | Evidence required | Current repository boundary |
|---|---|---|
| Regression | Reproducible tests at a pinned revision | 25 dependency-free checks pass; broader optional suites remain separate |
| Durable storage | Slotted-page round trips, durable index images, checksummed frames | Implemented and covered by corruption tests |
| Crash recovery | Fault matrix across write, flush, fsync, and replacement boundaries; reopen and no-loss proof | Torn-tail recovery and checkpoint interruption are covered; the full matrix remains open |
| Backup/restore | Checksummed archive, clean restore, reopen, and scheduled drill | Local archive/restore and checksum validation are implemented; scheduled drills remain operational work |
| SQL MVCC | SQL-integrated row versions, snapshot reads, serializable validation, and recovery rules | A tested MVCC primitive exists; SQL integration remains open |
| Identity security | Least-privilege roles, rotation/revocation, external identity review | File-backed PBKDF2 identities and role checks are implemented; external identity and rotation review remain open |
| Operations | Encryption/key management, retained audit, real consensus transport, observability | Not closed by this repository |
| Independent review | Separate reliability, security, and operations sign-off | Not closed by this repository |

Run the evidence suite:

```powershell
python -m compileall -q novadb tests
python -m tests.test_runner
```

The remaining work is intentionally explicit. The engine must not be called
production-ready until the open gates have evidence at the intended workload,
filesystem, deployment topology, and recovery objectives.
