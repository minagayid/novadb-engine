"""Prompt-to-SQL planning with an explicit approval boundary.

The model may propose one bounded NovaDB statement, but this module never
executes a generated statement during planning.  Callers must approve the
returned plan id and SQL hash before execution.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Callable

from .engine import Engine, NovaDBError


class PromptPlanningError(NovaDBError):
    """Raised when a natural-language request cannot become safe SQL."""


_ALLOWED_PREFIX = re.compile(r"^(?:SELECT\b|SHOW\s+TABLES\b|EXPLAIN\b|CREATE\s+TABLE\b|CREATE\s+INDEX\b|INSERT\s+INTO\b|UPDATE\b|DELETE\s+FROM\b)", re.IGNORECASE)
_BLOCKED_WORDS = re.compile(r"\b(PRAGMA|ATTACH|DETACH|VACUUM|LOAD|COPY|CALL|DROP|ALTER|TRUNCATE)\b", re.IGNORECASE)


def _single_statement(sql: str) -> str:
    """Normalize one SQL statement and reject comments or hidden statements."""
    value = sql.strip()
    if value.startswith("```") and value.endswith("```"):
        value = re.sub(r"^```(?:sql)?\s*|\s*```$", "", value, flags=re.IGNORECASE | re.DOTALL).strip()
    if not value:
        raise PromptPlanningError("The planner returned empty SQL")
    quote: str | None = None
    escaped = False
    semicolons: list[int] = []
    for index, char in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == ";":
            semicolons.append(index)
    if semicolons and any(value[index + 1 :].strip() for index in semicolons):
        raise PromptPlanningError("Only one SQL statement may be proposed")
    value = value.rstrip(";").strip()
    if "--" in value or "/*" in value or "*/" in value:
        raise PromptPlanningError("SQL comments are not allowed in generated plans")
    upper = value.upper()
    if _BLOCKED_WORDS.search(upper):
        raise PromptPlanningError("The generated SQL uses a blocked operation")
    if not _ALLOWED_PREFIX.match(value):
        raise PromptPlanningError("The generated SQL is outside NovaDB's supported safe grammar")
    return value


def schema_snapshot(engine: Engine) -> dict[str, Any]:
    """Return only the structural schema needed for planning."""
    return {
        "tables": {
            name: {
                "columns": [
                    {
                        "name": column.name,
                        "type": column.type,
                        "primary_key": column.primary_key,
                        "not_null": column.not_null,
                    }
                    for column in table.columns
                ],
                "row_count": len(table.rows),
            }
            for name, table in sorted(engine.tables.items())
        }
    }


def sql_risk(sql: str) -> tuple[str, bool]:
    upper = sql.upper()
    if upper.startswith(("SELECT", "SHOW TABLES", "EXPLAIN")):
        return "read_only", True
    if upper.startswith(("UPDATE", "DELETE FROM")):
        return "destructive_or_mutating", False
    return "mutating", False


def _validate_table_references(sql: str, schema: dict[str, Any]) -> None:
    """Reject common LLM hallucinations before a plan reaches approval."""
    upper = sql.upper()
    if upper.startswith("CREATE TABLE"):
        return
    known = set(schema.get("tables", {}))
    candidates = re.findall(r"\b(?:FROM|JOIN|INTO|UPDATE|ON)\s+([A-Za-z_]\w*)", sql, re.IGNORECASE)
    unknown = sorted({name for name in candidates if name not in known})
    if unknown:
        raise PromptPlanningError(f"The plan references unknown table(s): {', '.join(unknown)}")


@dataclass
class PromptPlan:
    plan_id: str
    prompt: str
    sql: str
    explanation: str
    risk: str
    read_only: bool
    sql_sha256: str
    schema: dict[str, Any]
    status: str
    created_at: float
    expires_at: float
    execution_id: str | None = None
    undo_available: bool = False

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionRecord:
    execution_id: str
    plan_id: str
    sql: str
    before: dict[str, Any]
    after: dict[str, Any]
    applied_version: int
    undone_version: int | None = None


class OpenAICompatiblePlanner:
    """Small standard-library client for Ollama or the local gateway."""

    def __init__(self, base_url: str, model: str = "qwen3:1.7b", token: str | None = None, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.token = token
        self.timeout = timeout

    def __call__(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        system = (
            "You are NovaDB's SQL planner. Return JSON only with keys sql, explanation. "
            "Propose exactly one statement supported by NovaDB. Never use DROP, ALTER, "
            "TRUNCATE, PRAGMA, ATTACH, comments, multiple statements, or external tables. "
            "Do not invent tables or columns. The caller will ask for approval before execution."
        )
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps({"request": prompt, "schema": schema}, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_tokens": 220,
            "stream": False,
            "reasoning_effort": "low",
            "think": False,
        }
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise PromptPlanningError(f"LLM planner unavailable: {exc}") from exc
        try:
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
            text = str(content).strip()
            fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.IGNORECASE | re.DOTALL)
            if fenced:
                text = fenced.group(1)
            else:
                start, end = text.find("{"), text.rfind("}")
                text = text[start : end + 1] if start >= 0 and end > start else text
            parsed = json.loads(text)
            return {"sql": parsed["sql"], "explanation": parsed.get("explanation", "")}
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise PromptPlanningError("LLM planner returned invalid JSON") from exc


class PromptSQLService:
    """Create expiring plans and execute them only after explicit approval."""

    def __init__(
        self,
        engine: Engine,
        planner: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        ttl_seconds: int = 900,
        max_sql_chars: int = 4_000,
        max_select_rows: int = 1_000,
        max_insert_rows: int = 100,
        history_limit: int = 10,
    ):
        self.engine = engine
        self.planner = planner
        self.ttl_seconds = ttl_seconds
        self.max_sql_chars = max_sql_chars
        self.max_select_rows = max_select_rows
        self.max_insert_rows = max_insert_rows
        self._plans: dict[str, PromptPlan] = {}
        self._history: deque[ExecutionRecord] = deque(maxlen=history_limit)
        self._redo: deque[ExecutionRecord] = deque(maxlen=history_limit)

    def _fallback(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        normalized = prompt.strip().lower()
        if re.search(r"\b(show|list|what)\b.*\b(table|tables)\b", normalized):
            return {"sql": "SHOW TABLES", "explanation": "Lists the tables without changing data."}
        match = re.search(r"\b(?:show|list|display)\b.*\bfrom\s+([a-z_]\w*)\b", normalized)
        if match and match.group(1) in schema["tables"]:
            return {"sql": f"SELECT * FROM {match.group(1)} LIMIT 25", "explanation": "Previews up to 25 rows from the requested table."}
        raise PromptPlanningError("No LLM planner is configured for this request")

    def _validate_task_budget(self, sql: str) -> None:
        if len(sql.encode("utf-8")) > self.max_sql_chars:
            raise PromptPlanningError(f"The SQL plan exceeds the {self.max_sql_chars}-byte safety limit")
        upper = sql.upper()
        if upper.startswith("SELECT"):
            match = re.search(r"\bLIMIT\s+(\d+)\s*$", sql, re.IGNORECASE)
            if not match:
                raise PromptPlanningError("SELECT plans must include an explicit LIMIT")
            if int(match.group(1)) > self.max_select_rows:
                raise PromptPlanningError(f"SELECT LIMIT cannot exceed {self.max_select_rows} rows")
        elif upper.startswith("INSERT INTO"):
            values = re.split(r"\bVALUES\b", sql, maxsplit=1, flags=re.IGNORECASE)
            groups = re.findall(r"\([^()]*\)", values[1]) if len(values) == 2 else []
            if not groups or len(groups) > self.max_insert_rows:
                raise PromptPlanningError(f"INSERT plans are limited to {self.max_insert_rows} rows")
        elif upper.startswith(("UPDATE", "DELETE FROM")) and not re.search(r"\bWHERE\b", sql, re.IGNORECASE):
            raise PromptPlanningError("UPDATE and DELETE plans require a WHERE clause")

    def preview(self, prompt: str) -> dict[str, Any]:
        prompt = str(prompt).strip()
        if not prompt or len(prompt) > 2_000:
            raise PromptPlanningError("Prompt must contain 1–2,000 characters")
        schema = schema_snapshot(self.engine)
        simple_table_request = re.search(r"\b(show|list|what)\b.*\b(table|tables)\b", prompt, re.IGNORECASE)
        proposed = self._fallback(prompt, schema) if simple_table_request else (self.planner(prompt, schema) if self.planner else self._fallback(prompt, schema))
        sql = _single_statement(str(proposed.get("sql", "")))
        self._validate_task_budget(sql)
        _validate_table_references(sql, schema)
        risk, read_only = sql_risk(sql)
        now = time.time()
        plan = PromptPlan(
            plan_id=uuid.uuid4().hex,
            prompt=prompt,
            sql=sql,
            explanation=str(proposed.get("explanation", "Review this statement before approval."))[:1_000],
            risk=risk,
            read_only=read_only,
            sql_sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            schema=schema,
            status="PENDING_APPROVAL",
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )
        self._plans[plan.plan_id] = plan
        return plan.public()

    def approve(self, plan_id: str, approved: bool, sql_sha256: str | None = None) -> dict[str, Any]:
        plan = self._plans.get(str(plan_id))
        if plan is None:
            raise PromptPlanningError("Unknown or expired plan")
        if time.time() > plan.expires_at:
            plan.status = "EXPIRED"
            raise PromptPlanningError("Plan has expired; create a new preview")
        if plan.status != "PENDING_APPROVAL":
            raise PromptPlanningError(f"Plan is already {plan.status.lower()}")
        if not approved:
            plan.status = "REJECTED"
            return {"ok": True, "status": plan.status, "plan": plan.public()}
        if sql_sha256 != plan.sql_sha256:
            raise PromptPlanningError("Approval must include the exact SQL hash shown in the preview")
        before = self.engine.snapshot_state() if not plan.read_only else None
        result = self.engine.execute(plan.sql)
        plan.status = "EXECUTED"
        response: dict[str, Any] = {"ok": True, "status": plan.status, "plan": plan.public(), "result": result}
        if before is not None:
            execution_id = uuid.uuid4().hex
            after = self.engine.snapshot_state()
            record = ExecutionRecord(execution_id, plan.plan_id, plan.sql, before, after, self.engine.version)
            self._history.append(record)
            self._redo.clear()
            plan.execution_id = execution_id
            plan.undo_available = True
            response["execution_id"] = execution_id
            response["undo_available"] = True
            response["plan"] = plan.public()
        return response

    def undo(self, execution_id: str | None = None) -> dict[str, Any]:
        candidates = [item for item in self._history if item.undone_version is None]
        if execution_id:
            candidates = [item for item in candidates if item.execution_id == execution_id]
        if not candidates:
            raise PromptPlanningError("No undoable approved mutation was found")
        record = candidates[-1]
        version = self.engine.restore_state(record.before, expected_version=record.applied_version)
        record.undone_version = version
        self._redo.append(record)
        plan = self._plans.get(record.plan_id)
        if plan:
            plan.status = "UNDONE"
            plan.undo_available = False
        return {"ok": True, "status": "UNDONE", "execution_id": record.execution_id, "plan_id": record.plan_id}

    def redo(self, execution_id: str | None = None) -> dict[str, Any]:
        if not self._redo:
            raise PromptPlanningError("No undone mutation is available to redo")
        record = self._redo[-1]
        if execution_id and record.execution_id != execution_id:
            raise PromptPlanningError("The requested execution is not the latest undone mutation")
        if record.undone_version is None:
            raise PromptPlanningError("The mutation is not currently undone")
        version = self.engine.restore_state(record.after, expected_version=record.undone_version)
        record.undone_version = None
        self._redo.pop()
        self._history.append(record)
        plan = self._plans.get(record.plan_id)
        if plan:
            plan.status = "EXECUTED"
            plan.undo_available = True
        return {"ok": True, "status": "REDONE", "execution_id": record.execution_id, "plan_id": record.plan_id, "version": version}
