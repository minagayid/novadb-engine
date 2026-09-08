import json
import threading

from novadb import Engine
from novadb.prompting import PromptPlanningError, PromptSQLService


def test_prompt_plan_requires_approval_and_exact_hash():
    db = Engine()
    db.execute("CREATE TABLE notes (id INT PRIMARY KEY, body TEXT NOT NULL)")
    db.execute("INSERT INTO notes VALUES (1, 'hello')")

    def planner(prompt, schema):
        assert prompt == "show my notes"
        assert "notes" in schema["tables"]
        return {"sql": "SELECT id, body FROM notes ORDER BY id LIMIT 10", "explanation": "Read-only preview."}

    service = PromptSQLService(db, planner=planner)
    plan = service.preview("show my notes")
    assert plan["status"] == "PENDING_APPROVAL"
    assert plan["read_only"] is True
    assert plan["sql_sha256"]
    result = service.approve(plan["plan_id"], True, plan["sql_sha256"])
    assert result["status"] == "EXECUTED"
    assert result["result"] == [{"id": 1, "body": "hello"}]


def test_prompt_plan_rejects_without_mutating():
    db = Engine()
    db.execute("CREATE TABLE notes (id INT PRIMARY KEY, body TEXT)")
    service = PromptSQLService(db, planner=lambda prompt, schema: {"sql": "INSERT INTO notes VALUES (1, 'x')", "explanation": "Write."})
    plan = service.preview("add a note")
    rejected = service.approve(plan["plan_id"], False)
    assert rejected["status"] == "REJECTED"
    assert db.execute("SELECT * FROM notes") == []


def test_prompt_plan_requires_exact_hash_and_blocks_hidden_sql():
    db = Engine()
    service = PromptSQLService(db, planner=lambda prompt, schema: {"sql": "SHOW TABLES; DELETE FROM notes", "explanation": "bad"})
    try:
        service.preview("do something")
    except PromptPlanningError as exc:
        assert "one SQL statement" in str(exc)
    else:
        raise AssertionError("multiple statements must be rejected")

    service = PromptSQLService(db, planner=lambda prompt, schema: {"sql": "SHOW TABLES", "explanation": "read"})
    plan = service.preview("list tables")
    try:
        service.approve(plan["plan_id"], True, "wrong")
    except PromptPlanningError as exc:
        assert "exact SQL hash" in str(exc)
    else:
        raise AssertionError("approval must bind to the preview hash")


def test_prompt_plan_rejects_unknown_tables_before_approval():
    db = Engine()
    db.execute("CREATE TABLE notes (id INT PRIMARY KEY, body TEXT)")
    service = PromptSQLService(db, planner=lambda prompt, schema: {"sql": "SELECT * FROM information_schema.tables LIMIT 1", "explanation": "bad"})
    try:
        service.preview("show database metadata")
    except PromptPlanningError as exc:
        assert "unknown table" in str(exc)
    else:
        raise AssertionError("unknown tables must be rejected before approval")


def test_prompt_mutation_has_bounded_undo_and_redo():
    db = Engine()
    db.execute("CREATE TABLE notes (id INT PRIMARY KEY, body TEXT)")
    service = PromptSQLService(
        db,
        planner=lambda prompt, schema: {"sql": "INSERT INTO notes VALUES (1, 'x')" if prompt == "first" else "INSERT INTO notes VALUES (2, 'y')"},
    )
    first_plan = service.preview("first")
    first = service.approve(first_plan["plan_id"], True, first_plan["sql_sha256"])
    second_plan = service.preview("second")
    second = service.approve(second_plan["plan_id"], True, second_plan["sql_sha256"])
    assert db.execute("SELECT * FROM notes LIMIT 10") == [{"id": 1, "body": "x"}, {"id": 2, "body": "y"}]
    assert service.undo(second["execution_id"])["status"] == "UNDONE"
    assert service.undo(first["execution_id"])["status"] == "UNDONE"
    assert db.execute("SELECT * FROM notes LIMIT 10") == []
    assert service.redo(first["execution_id"])["status"] == "REDONE"
    assert service.redo(second["execution_id"])["status"] == "REDONE"
    assert service.undo(second["execution_id"])["status"] == "UNDONE"
    assert service.undo(first["execution_id"])["status"] == "UNDONE"
    assert db.execute("SELECT * FROM notes LIMIT 10") == []
    assert len(service._history) == 2


def test_prompt_guardrails_bound_reads_and_writes():
    db = Engine()
    db.execute("CREATE TABLE notes (id INT PRIMARY KEY, body TEXT)")
    service = PromptSQLService(db, planner=lambda prompt, schema: {"sql": "SELECT * FROM notes", "explanation": "unbounded"})
    try:
        service.preview("read everything")
    except PromptPlanningError as exc:
        assert "explicit LIMIT" in str(exc)
    else:
        raise AssertionError("unbounded reads must be rejected")

    service = PromptSQLService(db, planner=lambda prompt, schema: {"sql": "DELETE FROM notes", "explanation": "unbounded"})
    try:
        service.preview("delete everything")
    except PromptPlanningError as exc:
        assert "WHERE clause" in str(exc)
    else:
        raise AssertionError("unbounded deletes must be rejected")


def test_prompt_rejects_non_boolean_approval_and_stale_plans():
    db = Engine()
    db.execute("CREATE TABLE notes (id INT PRIMARY KEY, body TEXT)")
    service = PromptSQLService(db, planner=lambda prompt, schema: {"sql": "INSERT INTO notes VALUES (1, 'x')"})
    plan = service.preview("add a note")
    for value in ("false", 1, None):
        try:
            service.approve(plan["plan_id"], value, plan["sql_sha256"])
        except PromptPlanningError as exc:
            assert "JSON boolean" in str(exc)
        else:
            raise AssertionError("approval must be a real boolean")
    db.execute("INSERT INTO notes VALUES (2, 'outside change')")
    try:
        service.approve(plan["plan_id"], True, plan["sql_sha256"])
    except PromptPlanningError as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("changed databases require a new preview")


def test_prompt_caps_affected_rows_and_undo_snapshot_size():
    db = Engine()
    db.execute("CREATE TABLE notes (id INT PRIMARY KEY, body TEXT)")
    db.execute("INSERT INTO notes VALUES (1, 'a'), (2, 'b')")
    service = PromptSQLService(db, planner=lambda prompt, schema: {"sql": "DELETE FROM notes WHERE id >= 1"}, max_mutation_rows=1)
    plan = service.preview("remove notes")
    try:
        service.approve(plan["plan_id"], True, plan["sql_sha256"])
    except PromptPlanningError as exc:
        assert "limited to 1 rows" in str(exc)
    else:
        raise AssertionError("mutations must be bounded before execution")
    assert len(db.execute("SELECT * FROM notes LIMIT 10")) == 2

    current_snapshot_bytes = len(json.dumps(db.snapshot_state(), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    service = PromptSQLService(db, planner=lambda prompt, schema: {"sql": "INSERT INTO notes VALUES (3, 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx')"}, max_undo_snapshot_bytes=current_snapshot_bytes + 10)
    plan = service.preview("add a note")
    try:
        service.approve(plan["plan_id"], True, plan["sql_sha256"])
    except PromptPlanningError as exc:
        assert "Undo snapshots" in str(exc)
    else:
        raise AssertionError("oversized undo history must be rejected before execution")
    assert len(db.execute("SELECT * FROM notes LIMIT 10")) == 2


def test_prompt_cache_is_bounded_and_expired_plans_are_purged():
    service = PromptSQLService(Engine(), planner=lambda prompt, schema: {"sql": "SHOW TABLES"}, max_plans=2)
    first = service.preview("first")
    second = service.preview("second")
    service.preview("third")
    assert len(service._plans) == 2
    assert first["plan_id"] not in service._plans
    service._plans[second["plan_id"]].expires_at = 0
    service.preview("fourth")
    assert second["plan_id"] not in service._plans


def test_prompt_approval_is_single_use_under_concurrency():
    db = Engine()
    db.execute("CREATE TABLE notes (id INT PRIMARY KEY, body TEXT)")
    service = PromptSQLService(db, planner=lambda prompt, schema: {"sql": "INSERT INTO notes VALUES (1, 'x')"})
    plan = service.preview("add a note")
    barrier = threading.Barrier(2)
    outcomes = []

    def approve():
        barrier.wait()
        try:
            outcomes.append(service.approve(plan["plan_id"], True, plan["sql_sha256"])["status"])
        except PromptPlanningError as exc:
            outcomes.append(str(exc))

    threads = [threading.Thread(target=approve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("EXECUTED") == 1
    assert any("already executed" in outcome for outcome in outcomes)
    assert db.execute("SELECT * FROM notes LIMIT 10") == [{"id": 1, "body": "x"}]
