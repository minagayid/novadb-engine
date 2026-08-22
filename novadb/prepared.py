from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .bytecode import BytecodeProgram, Instruction, compile_expression, compile_predicate
from .engine import Engine, ParseError, _parse_insert, _parse_where_tail, _split_csv


def _rebase_program(program: BytecodeProgram, offset: int) -> BytecodeProgram:
    instructions = []
    for instruction in program.instructions:
        operand = instruction.operand + offset if instruction.opcode == "LOAD_PARAM" else instruction.operand
        instructions.append(Instruction(instruction.opcode, operand))
    return BytecodeProgram(instructions, program.parameter_count + offset)


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, dict)):
        import json
        return "'" + json.dumps(value, separators=(",", ":")).replace("'", "''") + "'"
    return "'" + str(value).replace("'", "''") + "'"


def _bind_sql(sql: str, params: tuple[Any, ...]) -> str:
    parts = sql.split("?")
    if len(parts) - 1 != len(params):
        raise ValueError(f"expected {len(parts) - 1} parameters, got {len(params)}")
    return "".join(part + (_sql_literal(params[index]) if index < len(params) else "") for index, part in enumerate(parts))


@dataclass
class PreparedStatement:
    engine: Engine
    sql: str

    def __post_init__(self) -> None:
        self.parameter_count = self.sql.count("?")
        self.kind = "insert" if self.sql.lstrip().upper().startswith("INSERT INTO") else "query"
        self._projection_programs: list[tuple[str, str, BytecodeProgram]] = []
        self._predicate_program: BytecodeProgram | None = None
        if self.kind == "query" and self.sql.lstrip().upper().startswith("SELECT"):
            self._compile_select()

    def _compile_select(self) -> None:
        match = re.match(r"^SELECT\s+(.*?)\s+FROM\s+(\w+)(.*)$", self.sql.strip().rstrip(";"), re.IGNORECASE | re.DOTALL)
        if not match:
            return
        projection, _, tail = match.group(1).strip(), match.group(2), match.group(3)
        parameter_offset = 0
        for expression in _split_csv(projection):
            alias_match = re.match(r"^(.*?)\s+AS\s+(\w+)$", expression, re.IGNORECASE | re.DOTALL)
            raw = alias_match.group(1).strip() if alias_match else expression.strip()
            alias = alias_match.group(2) if alias_match else raw
            if not re.match(r"^(COUNT|SUM|AVG|MIN|MAX)\s*\(", raw, re.IGNORECASE):
                program = compile_expression(raw)
                self._projection_programs.append((raw, alias, _rebase_program(program, parameter_offset)))
                parameter_offset += program.parameter_count
        where, _, _, _ = _parse_where_tail(tail)
        predicate = compile_predicate(where)
        self._predicate_program = _rebase_program(predicate, parameter_offset) if predicate else None

    def _check_params(self, params: tuple[Any, ...]) -> None:
        if len(params) != self.parameter_count:
            raise ValueError(f"expected {self.parameter_count} parameters, got {len(params)}")

    def _execute_compiled_select(self, params: tuple[Any, ...]) -> Any:
        match = re.match(r"^SELECT\s+(.*?)\s+FROM\s+(\w+)(.*)$", self.sql.strip().rstrip(";"), re.IGNORECASE | re.DOTALL)
        if not match or not self._projection_programs:
            return None
        projection, table_name, tail = match.group(1).strip(), match.group(2), match.group(3)
        where, order_by, limit, group_by = _parse_where_tail(tail)
        if group_by or any(re.match(r"^(COUNT|SUM|AVG|MIN|MAX)\s*\(", expr.strip(), re.IGNORECASE) for expr in _split_csv(projection)):
            return None
        table = self.engine.tables[table_name]
        rows = table.rows
        if self._predicate_program:
            rows = [row for row in rows if bool(self._predicate_program.run(row, params))]
        result = [{alias: program.run(row, params) for _, alias, program in self._projection_programs} for row in rows]
        if order_by and result and order_by[0] in result[0]:
            column, descending = order_by
            result.sort(key=lambda row: (row.get(column) is None, row.get(column)), reverse=descending)
        return result[:limit] if limit is not None else result

    def execute(self, params: Iterable[Any] = ()) -> Any:
        values = tuple(params)
        self._check_params(values)
        if self.kind == "query" and self.sql.lstrip().upper().startswith("SELECT"):
            compiled_result = self._execute_compiled_select(values)
            if compiled_result is not None:
                return compiled_result
        if self.kind == "insert":
            table_name, columns, groups = _parse_insert(self.sql.replace("?", "NULL"))
            bound_groups = []
            cursor = 0
            for group in groups:
                bound = []
                for value in group:
                    if value is None and cursor < len(values):
                        bound.append(values[cursor])
                        cursor += 1
                    else:
                        bound.append(value)
                bound_groups.append(bound)
            table = self.engine.tables[table_name]
            names = columns or [column.name for column in table.columns]
            return self.engine.bulk_insert(table_name, names, bound_groups)
        return self.engine.execute(_bind_sql(self.sql, values))

    def executemany(self, parameter_rows: Iterable[Iterable[Any]]) -> Any:
        rows = [tuple(row) for row in parameter_rows]
        if self.kind != "insert":
            return [self.execute(row) for row in rows]
        if not rows:
            return {"status": "inserted", "count": 0, "rows": []}
        template = self.sql.replace("?", "NULL")
        table_name, columns, groups = _parse_insert(template)
        if len(groups) != 1:
            raise ParseError("executemany expects one VALUES group in the prepared INSERT")
        template_values = groups[0]
        bound_groups = []
        for params in rows:
            self._check_params(params)
            cursor = 0
            bound = []
            for value in template_values:
                if value is None and cursor < len(params):
                    bound.append(params[cursor])
                    cursor += 1
                else:
                    bound.append(value)
            bound_groups.append(bound)
        table = self.engine.tables[table_name]
        names = columns or [column.name for column in table.columns]
        return self.engine.bulk_insert(table_name, names, bound_groups)

    def explain(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "parameter_count": self.parameter_count,
            "projection_bytecode": [{"alias": alias, "program": program.explain()} for _, alias, program in self._projection_programs],
            "predicate_bytecode": self._predicate_program.explain() if self._predicate_program else None,
        }
