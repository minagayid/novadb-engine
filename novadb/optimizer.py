from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .engine import Engine, Table, _find_keyword, _sort_key, eval_expr, eval_predicate


@dataclass
class Relation:
    table: str
    alias: str
    rows: list[dict[str, Any]]

    @property
    def cardinality(self) -> int:
        return len(self.rows)


@dataclass
class JoinSpec:
    table: str
    alias: str
    left_key: str
    right_key: str


@dataclass
class PlanNode:
    operation: str
    cost: float
    estimated_rows: float
    details: dict[str, Any] = field(default_factory=dict)
    children: list["PlanNode"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "cost": round(self.cost, 3),
            "estimated_rows": round(self.estimated_rows, 3),
            "details": self.details,
            "children": [child.to_dict() for child in self.children],
        }


class QuerySyntaxError(ValueError):
    pass


def _qualify_rows(table_name: str, alias: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    qualified = []
    for row in rows:
        output = dict(row)
        for column, value in row.items():
            output[f"{table_name}.{column}"] = value
            output[f"{alias}.{column}"] = value
        qualified.append(output)
    return qualified


def _identifier_parts(identifier: str) -> tuple[str | None, str]:
    identifier = identifier.strip()
    if "." in identifier:
        owner, column = identifier.split(".", 1)
        return owner, column
    return None, identifier


def _column_values(relation: Relation, identifier: str) -> list[Any]:
    owner, column = _identifier_parts(identifier)
    values = []
    for row in relation.rows:
        if owner:
            values.append(row.get(f"{owner}.{column}"))
        else:
            values.append(row.get(column))
    return values


def _distinct_count(relation: Relation, identifier: str) -> int:
    return max(1, len({repr(value) for value in _column_values(relation, identifier)}))


def hash_join(left: list[dict[str, Any]], right: list[dict[str, Any]], left_key: str, right_key: str) -> list[dict[str, Any]]:
    """Hash join with the smaller input as the build side."""
    if len(left) <= len(right):
        build, probe, build_key, probe_key, build_is_left = left, right, left_key, right_key, True
    else:
        build, probe, build_key, probe_key, build_is_left = right, left, right_key, left_key, False
    index: dict[Any, list[dict[str, Any]]] = {}
    for row in build:
        index.setdefault(row.get(build_key), []).append(row)
    output = []
    for row in probe:
        matches = index.get(row.get(probe_key), [])
        for match in matches:
            merged = dict(match)
            merged.update(row)
            output.append(merged)
    return output


def nested_loop_join(left: list[dict[str, Any]], right: list[dict[str, Any]], left_key: str, right_key: str) -> list[dict[str, Any]]:
    output = []
    for left_row in left:
        left_value = left_row.get(left_key)
        for right_row in right:
            if left_value == right_row.get(right_key):
                merged = dict(left_row)
                merged.update(right_row)
                output.append(merged)
    return output


class QueryPlanner:
    """Small cost-based planner for inner equi-join SELECT statements."""

    def __init__(self, tables: dict[str, Table]):
        self.tables = tables

    def _parse(self, sql: str) -> tuple[str, str, list[JoinSpec], str]:
        sql = sql.strip().rstrip(";")
        match = re.match(r"^SELECT\s+(.*?)\s+FROM\s+(.*)$", sql, re.IGNORECASE | re.DOTALL)
        if not match:
            raise QuerySyntaxError("Expected SELECT ... FROM ...")
        projection = match.group(1).strip()
        from_tail = match.group(2).strip()
        cut_positions = [(pos, keyword) for keyword in ("WHERE", "ORDER BY", "LIMIT", "GROUP BY") if (pos := _find_keyword(from_tail, keyword)) >= 0]
        cut_positions.sort()
        from_part = from_tail[:cut_positions[0][0]].strip() if cut_positions else from_tail
        tail = from_tail[cut_positions[0][0]:].strip() if cut_positions else ""
        tokens = from_part.split()
        if not tokens:
            raise QuerySyntaxError("Invalid FROM clause")
        base_table = tokens[0]
        alias_index = 1
        if len(tokens) > 2 and tokens[1].upper() == "AS":
            base_alias = tokens[2]
            alias_index = 3
        elif len(tokens) > 1 and tokens[1].upper() not in {"JOIN", "INNER"}:
            base_alias = tokens[1]
            alias_index = 2
        else:
            base_alias = base_table
        remainder = " ".join(tokens[alias_index:]).strip()
        if base_table not in self.tables:
            raise QuerySyntaxError(f"Unknown table: {base_table}")
        joins: list[JoinSpec] = []
        join_pattern = re.compile(r"^(?:(?:INNER)\s+)?JOIN\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?\s+ON\s+(.*)$", re.IGNORECASE | re.DOTALL)
        while remainder:
            join_match = join_pattern.match(remainder)
            if not join_match:
                raise QuerySyntaxError(f"Invalid JOIN clause: {remainder}")
            join_table = join_match.group(1)
            join_alias = join_match.group(2) or join_table
            on_and_rest = join_match.group(3).strip()
            next_join = re.search(r"\s+(?:(?:INNER)\s+)?JOIN\s+", on_and_rest, re.IGNORECASE)
            on_text = on_and_rest[:next_join.start()].strip() if next_join else on_and_rest
            remainder = on_and_rest[next_join.start():].strip() if next_join else ""
            comparison = re.match(r"^(\w+(?:\.\w+)?)\s*=\s*(\w+(?:\.\w+)?)$", on_text)
            if not comparison:
                raise QuerySyntaxError("Only equi-join predicates are supported")
            if join_table not in self.tables:
                raise QuerySyntaxError(f"Unknown table: {join_table}")
            joins.append(JoinSpec(join_table, join_alias, comparison.group(1), comparison.group(2)))
        return projection, base_table, joins, tail

    def _relation(self, table: str, alias: str) -> Relation:
        return Relation(table, alias, _qualify_rows(table, alias, self.tables[table].rows))

    def plan(self, sql: str) -> PlanNode:
        _, base_table, joins, _ = self._parse(sql)
        base_alias_match = re.search(r"\bFROM\s+\w+(?:\s+(?:AS\s+)?(\w+))?", sql, re.IGNORECASE)
        base_alias = base_alias_match.group(1) if base_alias_match and base_alias_match.group(1) else base_table
        current = self._relation(base_table, base_alias)
        root = PlanNode("SEQ_SCAN", float(max(current.cardinality, 1)), float(current.cardinality), {"table": base_table, "alias": base_alias})
        remaining = joins[:]
        known_aliases = {base_alias, base_table}
        while remaining:
            candidates = []
            for join in remaining:
                if not ({join.left_key.split(".")[0], join.right_key.split(".")[0]} & known_aliases):
                    continue
                relation = self._relation(join.table, join.alias)
                if join.left_key.split(".")[0] in {join.alias, join.table}:
                    left_key, right_key = join.right_key, join.left_key
                else:
                    left_key, right_key = join.left_key, join.right_key
                left_distinct = max(1, len({repr(row.get(left_key)) for row in _qualify_rows("current", "current", current.rows)}))
                right_distinct = _distinct_count(relation, right_key)
                estimated_rows = current.cardinality * relation.cardinality / max(left_distinct, right_distinct, 1)
                hash_cost = current.cardinality + relation.cardinality
                nested_cost = current.cardinality * relation.cardinality
                candidates.append((hash_cost, nested_cost, estimated_rows, join, relation, left_key, right_key))
            if not candidates:
                raise QuerySyntaxError("JOIN predicates must connect to an already joined relation")
            hash_cost, nested_cost, estimated_rows, join, relation, left_key, right_key = min(candidates, key=lambda item: item[0])
            operation = "HASH_JOIN" if hash_cost <= nested_cost else "NESTED_LOOP_JOIN"
            details = {"left_key": left_key, "right_key": right_key, "strategy": operation, "build_rows": min(current.cardinality, relation.cardinality)}
            root = PlanNode(operation, root.cost + hash_cost if operation == "HASH_JOIN" else root.cost + nested_cost, estimated_rows, details, [root, PlanNode("SEQ_SCAN", float(max(relation.cardinality, 1)), float(relation.cardinality), {"table": join.table, "alias": join.alias})])
            current = Relation(f"({current.table} JOIN {join.table})", f"{current.alias}+{join.alias}", [])
            known_aliases.update({join.alias, join.table})
            remaining.remove(join)
            # Keep only estimates at planning time; execution rebuilds the actual row relation.
            current.rows = [{}] * max(0, int(round(estimated_rows)))
        return root

    def execute(self, sql: str) -> list[dict[str, Any]]:
        projection, base_table, joins, tail = self._parse(sql)
        base_alias_match = re.search(r"\bFROM\s+\w+(?:\s+(?:AS\s+)?(\w+))?", sql, re.IGNORECASE)
        base_alias = base_alias_match.group(1) if base_alias_match and base_alias_match.group(1) else base_table
        rows = _qualify_rows(base_table, base_alias, self.tables[base_table].rows)
        known_aliases = {base_alias, base_table}
        remaining = joins[:]
        while remaining:
            chosen = None
            for join in remaining:
                owners = {join.left_key.split(".")[0], join.right_key.split(".")[0]}
                if owners & known_aliases:
                    chosen = join
                    break
            if chosen is None:
                raise QuerySyntaxError("JOIN predicates must connect to an already joined relation")
            relation = _qualify_rows(chosen.table, chosen.alias, self.tables[chosen.table].rows)
            if chosen.left_key.split(".")[0] in {chosen.alias, chosen.table}:
                left_key, right_key = chosen.right_key, chosen.left_key
            else:
                left_key, right_key = chosen.left_key, chosen.right_key
            left_distinct = max(1, len({repr(row.get(left_key)) for row in rows}))
            right_distinct = max(1, len({repr(row.get(right_key)) for row in relation}))
            hash_cost = len(rows) + len(relation)
            nested_cost = len(rows) * len(relation)
            rows = hash_join(rows, relation, left_key, right_key) if hash_cost <= nested_cost else nested_loop_join(rows, relation, left_key, right_key)
            known_aliases.update({chosen.alias, chosen.table})
            remaining.remove(chosen)
        where = None
        order_by = None
        limit = None
        group_by = None
        positions = []
        for keyword in ("WHERE", "GROUP BY", "ORDER BY", "LIMIT"):
            pos = _find_keyword(tail, keyword)
            if pos >= 0:
                positions.append((pos, keyword))
        positions.sort()
        for index, (pos, keyword) in enumerate(positions):
            end = positions[index + 1][0] if index + 1 < len(positions) else len(tail)
            value = tail[pos + len(keyword):end].strip()
            if keyword == "WHERE":
                where = value
            elif keyword == "GROUP BY":
                group_by = value
            elif keyword == "ORDER BY":
                order_match = re.match(r"^(.*?)(?:\s+(ASC|DESC))?$", value, re.IGNORECASE | re.DOTALL)
                order_by = (order_match.group(1).strip(), bool(order_match.group(2) and order_match.group(2).upper() == "DESC"))
            elif keyword == "LIMIT":
                limit = int(value)
        rows = [row for row in rows if eval_predicate(where, row)]
        if group_by:
            raise QuerySyntaxError("GROUP BY with JOIN is reserved for the next optimizer stage")
        if order_by:
            expression, descending = order_by
            rows.sort(key=lambda row: _sort_key(lambda item: eval_expr(expression, item), row), reverse=descending)
        if limit is not None:
            rows = rows[:limit]
        if projection == "*":
            return rows
        result = []
        for row in rows:
            output = {}
            for expression in projection.split(","):
                expression = expression.strip()
                alias_match = re.match(r"^(.*?)\s+AS\s+(\w+)$", expression, re.IGNORECASE | re.DOTALL)
                raw = alias_match.group(1).strip() if alias_match else expression
                alias = alias_match.group(2) if alias_match else raw
                output[alias] = eval_expr(raw, row)
            result.append(output)
        return result

    def explain(self, sql: str) -> dict[str, Any]:
        return self.plan(sql).to_dict()


class QueryExecutor:
    def __init__(self, tables: dict[str, Table]):
        self.planner = QueryPlanner(tables)

    def execute(self, sql: str) -> list[dict[str, Any]]:
        return self.planner.execute(sql)
