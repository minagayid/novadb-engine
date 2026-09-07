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
    service = PromptSQLService(db, planner=lambda prompt, schema: {"sql": "SELECT * FROM information_schema.tables", "explanation": "bad"})
    try:
        service.preview("show database metadata")
    except PromptPlanningError as exc:
        assert "unknown table" in str(exc)
    else:
        raise AssertionError("unknown tables must be rejected before approval")
