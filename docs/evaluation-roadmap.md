# Agent memory evidence gates

Memory summaries can retain source IDs, versions and hashes in metadata. Namespace/session filters bound visibility; upserts now reject reuse of an ID across scopes. `tests/test_memory_isolation.py` checks the original summary survives an attempted overwrite, another session sees no result, TTL expiry removes visibility, and records survive graceful durable reopen.

This is a local storage contract, not credential-bound tenant isolation. The HTTP identity service must bind allowed memory scopes to identities before multi-tenant use. Current search considers at most 50 visible candidate rows. A true crash/recovery benchmark and comparable SQLite/PostgreSQL/NovaDB semantic benchmark remain pending. No performance superiority is claimed.

Reproduce: `python -m pytest tests/test_memory_isolation.py tests/test_memory_api.py tests/test_http_security.py -q`. The HTTP test fixture restores shared handler authentication so suite order does not change results.
