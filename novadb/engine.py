from __future__ import annotations

import copy
import json
import math
import os
import re
import threading
import time
import uuid
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .page_store import PageStore

try:
    import numpy as np
except ImportError:
    np = None


class NovaDBError(Exception):
    """Base exception for NovaDB."""


class ParseError(NovaDBError):
    pass


class TransactionConflict(NovaDBError):
    pass


class ConstraintError(NovaDBError):
    pass


_MISSING = object()


def _value_key(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return (type(value).__name__, value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _split_csv(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for i, ch in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _split_keyword(text: str, keyword: str) -> list[str]:
    pattern = re.compile(rf"\s+{keyword}\s+", re.IGNORECASE)
    out: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            match = pattern.match(text, i)
            if match:
                out.append(text[start:i].strip())
                start = match.end()
                i = match.end()
                continue
        i += 1
    out.append(text[start:].strip())
    return out


def _find_keyword(text: str, keyword: str) -> int:
    pattern = re.compile(rf"\b{keyword}\b", re.IGNORECASE)
    depth = 0
    quote: str | None = None
    escaped = False
    for match in pattern.finditer(text):
        for ch in text[: match.start()]:
            pass
        # Re-scan only the segment from the previous candidate is unnecessary for
        # our small SQL grammar; this stateful scan handles quoted parentheses.
    depth = 0
    quote = None
    escaped = False
    for i, ch in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            end = i + len(keyword)
            if text[i:end].upper() == keyword.upper() and (end == len(text) or not (text[end].isalnum() or text[end] == "_")):
                return i
    return -1


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        if value[0] == "'":
            return value[1:-1].replace("''", "'")
        return bytes(value[1:-1], "utf-8").decode("unicode_escape")
    return value


def parse_value(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    if text.upper() in {"NULL", "NONE"}:
        return None
    if text.upper() in {"TRUE", "FALSE"}:
        return text.upper() == "TRUE"
    if len(text) >= 2 and text[0] in "'\"" and text[-1] == text[0]:
        return _unquote(text)
    if text.startswith("[") or text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return _unquote(text)
    try:
        if re.fullmatch(r"[-+]?\d+", text):
            return int(text)
        if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?", text) or re.fullmatch(r"[-+]?\d+[eE][-+]?\d+", text):
            return float(text)
    except ValueError:
        pass
    return text


@lru_cache(maxsize=256)
def _compile_json_path(path: str) -> tuple[str, ...] | None:
    if not path.startswith("$"):
        return None
    return tuple(part[0] or part[1] for part in re.findall(r"(?:\.([A-Za-z_][\w]*)|\[([0-9]+)\])", path[1:]))


def _json_path(value: Any, path: str) -> Any:
    tokens = _compile_json_path(path)
    if tokens is None:
        return None
    current = value
    for token in tokens:
        try:
            current = current[int(token)] if isinstance(current, list) else current.get(token)
        except (AttributeError, IndexError, KeyError, TypeError):
            return None
    return current


def vector_distance(left: Any, right: Any, metric: str = "cosine") -> float | None:
    try:
        a = [float(x) for x in left]
        b = [float(x) for x in right]
    except (TypeError, ValueError):
        return None
    if len(a) != len(b) or not a:
        return None
    if metric.lower() in {"l2", "euclidean"}:
        return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 1.0
    return 1.0 - dot / (na * nb)


def _split_boolean(expr: str, keyword: str) -> list[str]:
    pieces = []
    depth = 0
    quote: str | None = None
    start = 0
    i = 0
    upper = expr.upper()
    needle = f" {keyword.upper()} "
    while i < len(expr):
        ch = expr[i]
        if quote:
            if ch == quote and (i == 0 or expr[i - 1] != "\\"):
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and upper.startswith(needle, i):
            pieces.append(expr[start:i].strip())
            start = i + len(needle)
            i = start
            continue
        i += 1
    pieces.append(expr[start:].strip())
    return pieces


def _strip_outer_parens(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        balanced = True
        quote: str | None = None
        for i, ch in enumerate(expr):
            if quote:
                if ch == quote and expr[i - 1] != "\\":
                    quote = None
                continue
            if ch in "'\"":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(expr) - 1:
                    balanced = False
                    break
        if balanced:
            expr = expr[1:-1].strip()
        else:
            break
    return expr


_COMPARISON = re.compile(r"^(.*?)\s*(<=|>=|<>|!=|=|<|>)\s*(.*?)$", re.DOTALL)


def _resolve_identifier(token: str, row: dict[str, Any]) -> Any:
    token = token.strip()
    if token in row:
        return row[token]
    if "." in token and token.split(".", 1)[1] in row:
        return row[token.split(".", 1)[1]]
    return _MISSING


@lru_cache(maxsize=512)
def compile_expr(expr: str):
    """Compile a safe expression once; the returned callable only reads a row."""
    expr = _strip_outer_parens(expr.strip())
    arithmetic = re.match(r"^(.*?)\s*([+\-*/])\s*(.*?)$", expr)
    if arithmetic and not re.match(r"^[+-]?\d+(?:\.\d+)?$", expr):
        left_fn = compile_expr(arithmetic.group(1))
        right_fn = compile_expr(arithmetic.group(3))
        operator = arithmetic.group(2)
        def arithmetic_value(row):
            left, right = left_fn(row), right_fn(row)
            if left is _MISSING or right is _MISSING or left is None or right is None:
                return None
            try:
                return {"+": lambda: left + right, "-": lambda: left - right, "*": lambda: left * right, "/": lambda: left / right}[operator]()
            except (TypeError, ZeroDivisionError):
                return None
        return arithmetic_value
    func = re.match(r"^(\w+)\s*\((.*)\)$", expr, re.DOTALL)
    if func:
        name = func.group(1).upper()
        arg_fns = [compile_expr(arg) for arg in _split_csv(func.group(2))]
        if name == "JSON_EXTRACT":
            if len(arg_fns) != 2:
                raise ParseError("JSON_EXTRACT requires a value and a path")
            return lambda row: _json_path(arg_fns[0](row), str(arg_fns[1](row)))
        if name in {"VECTOR_DISTANCE", "COSINE_DISTANCE", "L2_DISTANCE"}:
            metric = "l2" if name == "L2_DISTANCE" else "cosine"
            return lambda row: vector_distance(arg_fns[0](row), arg_fns[1](row), metric)
        if name == "LENGTH":
            return lambda row: len(arg_fns[0](row)) if arg_fns[0](row) is not None else None
        if name == "UPPER":
            return lambda row: str(arg_fns[0](row)).upper() if arg_fns[0](row) is not None else None
        if name == "LOWER":
            return lambda row: str(arg_fns[0](row)).lower() if arg_fns[0](row) is not None else None
        if name == "COALESCE":
            return lambda row: next((fn(row) for fn in arg_fns if fn(row) is not None and fn(row) is not _MISSING), None)
        if name == "ABS":
            return lambda row: abs(arg_fns[0](row)) if arg_fns and arg_fns[0](row) is not None else None
        raise ParseError(f"Unsupported function: {name}")
    literal = parse_value(expr)
    if expr == "*":
        return lambda row: 1
    identifier = re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?", expr)
    if identifier:
        return lambda row: row[expr] if expr in row else (row.get(expr.split(".", 1)[1], _MISSING) if "." in expr else _MISSING)
    return lambda row: literal


def eval_expr(expr: str, row: dict[str, Any] | None = None) -> Any:
    return compile_expr(expr)(row or {})


def _sort_key(fn, row: dict[str, Any]):
    value = fn(row)
    return (value is None, value)


def eval_predicate(expr: str | None, row: dict[str, Any]) -> bool:
    if not expr:
        return True
    expr = _strip_outer_parens(expr)
    ors = _split_boolean(expr, "OR")
    if len(ors) > 1:
        return any(eval_predicate(part, row) for part in ors)
    ands = _split_boolean(expr, "AND")
    if len(ands) > 1:
        return all(eval_predicate(part, row) for part in ands)
    is_null = re.match(r"^(.*?)\s+IS\s+(NOT\s+)?NULL$", expr, re.IGNORECASE)
    if is_null:
        result = eval_expr(is_null.group(1), row) is None
        return not result if is_null.group(2) else result
    match = _COMPARISON.match(expr)
    if not match:
        value = eval_expr(expr, row)
        return bool(value)
    left = eval_expr(match.group(1), row)
    right = eval_expr(match.group(3), row)
    op = match.group(2)
    if left is _MISSING or right is _MISSING or left is None or right is None:
        return op == "=" and left is None and right is None
    try:
        if op == "=":
            return left == right
        if op in {"!=", "<>"}:
            return left != right
        if op == "<":
            return left < right
        if op == ">":
            return left > right
        if op == "<=":
            return left <= right
        if op == ">=":
            return left >= right
    except TypeError:
        return False
    return False


@dataclass
class Column:
    name: str
    type: str = "TEXT"
    primary_key: bool = False
    not_null: bool = False


@dataclass
class Table:
    name: str
    columns: list[Column]
    rows: list[dict[str, Any]] = field(default_factory=list)
    indexes: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    primary_keys: dict[str, set[str]] = field(default_factory=dict)
    column_map: dict[str, Column] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.column_map = {column.name: column for column in self.columns}
        self.rebuild_indexes()

    def clone(self) -> "Table":
        return copy.deepcopy(self)

    def normalize_row(self, values: dict[str, Any]) -> dict[str, Any]:
        unknown = set(values) - self.column_map.keys()
        if unknown:
            raise ConstraintError(f"Unknown column(s): {', '.join(sorted(unknown))}")
        row: dict[str, Any] = {}
        for col in self.columns:
            value = values.get(col.name)
            if col.not_null and value is None:
                raise ConstraintError(f"Column {col.name} cannot be NULL")
            if value is not None:
                value = self._coerce(col, value)
            row[col.name] = value
        return row

    @staticmethod
    def _coerce(col: Column, value: Any) -> Any:
        typ = col.type
        if typ in {"INT", "INTEGER", "BIGINT"}:
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            try:
                return int(value)
            except (ValueError, TypeError):
                raise ConstraintError(f"Invalid integer for {col.name}: {value!r}")
        if typ in {"REAL", "FLOAT", "DOUBLE", "DECIMAL"}:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            try:
                return float(value)
            except (ValueError, TypeError):
                raise ConstraintError(f"Invalid number for {col.name}: {value!r}")
        if typ in {"JSON", "JSONB"} and isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                raise ConstraintError(f"Invalid JSON for {col.name}")
        if typ.startswith("VECTOR") and isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                raise ConstraintError(f"Invalid vector for {col.name}")
        return value

    def rebuild_indexes(self) -> None:
        self.primary_keys = {column.name: set() for column in self.columns if column.primary_key}
        for row in self.rows:
            for name in self.primary_keys:
                self.primary_keys[name].add(_value_key(row.get(name)))
        for name, index in self.indexes.items():
            index.clear()
            for pos, row in enumerate(self.rows):
                key = _value_key(row.get(name))
                index.setdefault(key, []).append(pos)

    def append_index_entries(self, pos: int, row: dict[str, Any]) -> None:
        for name, index in self.indexes.items():
            index.setdefault(_value_key(row.get(name)), []).append(pos)


def _prepare_many_rows(table: Table, column_names: list[str], values_groups: list[list[Any]]) -> tuple[list[dict[str, Any]], dict[str, set[Any]]]:
    if len(set(column_names)) != len(column_names) or set(column_names) - table.column_map.keys():
        raise ConstraintError("Invalid INSERT column list")
    new_rows: list[dict[str, Any]] = []
    pending_keys = {name: set(keys) for name, keys in table.primary_keys.items()}
    positions = {name: index for index, name in enumerate(column_names)}
    for values in values_groups:
        if len(values) != len(column_names):
            raise ParseError("INSERT column/value count mismatch")
        row: dict[str, Any] = {}
        for col in table.columns:
            value = values[positions[col.name]] if col.name in positions else None
            if col.not_null and value is None:
                raise ConstraintError(f"Column {col.name} cannot be NULL")
            row[col.name] = table._coerce(col, value) if value is not None else None
        for col in table.columns:
            if col.primary_key:
                key = _value_key(row.get(col.name))
                if key in pending_keys[col.name]:
                    raise ConstraintError(f"Duplicate primary key: {col.name}={row[col.name]!r}")
                pending_keys[col.name].add(key)
        new_rows.append(row)
    return new_rows, pending_keys


class Transaction:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.base_version = engine.version
        self.tables = {name: table.clone() for name, table in engine.tables.items()}
        self.operations: list[dict[str, Any]] = []
        self.closed = False

    def _table(self, name: str) -> Table:
        if name not in self.tables:
            raise NovaDBError(f"Table does not exist: {name}")
        return self.tables[name]

    def create_table(self, name: str, columns: list[Column]) -> None:
        if name in self.tables:
            raise ConstraintError(f"Table already exists: {name}")
        self.tables[name] = Table(name, columns)
        self.operations.append({"op": "create_table", "name": name, "columns": [c.__dict__ for c in columns]})

    def create_index(self, table_name: str, column: str) -> None:
        table = self._table(table_name)
        if column not in {c.name for c in table.columns}:
            raise ConstraintError(f"Unknown column: {column}")
        table.indexes[column] = {}
        table.rebuild_indexes()
        self.operations.append({"op": "create_index", "table": table_name, "column": column})

    def insert(self, table_name: str, values: dict[str, Any]) -> dict[str, Any]:
        table = self._table(table_name)
        row = table.normalize_row(values)
        for col in table.columns:
            if col.primary_key and _value_key(row.get(col.name)) in table.primary_keys[col.name]:
                raise ConstraintError(f"Duplicate primary key: {col.name}={row[col.name]!r}")
        table.rows.append(row)
        for col in table.columns:
            if col.primary_key:
                table.primary_keys[col.name].add(_value_key(row.get(col.name)))
        table.append_index_entries(len(table.rows) - 1, row)
        self.operations.append({"op": "insert", "table": table_name, "row": row})
        return row

    def insert_many(self, table_name: str, column_names: list[str], values_groups: list[list[Any]]) -> list[dict[str, Any]]:
        table = self._table(table_name)
        new_rows, pending_keys = _prepare_many_rows(table, column_names, values_groups)
        table.rows.extend(new_rows)
        table.primary_keys = pending_keys
        if table.indexes:
            table.rebuild_indexes()
        self.operations.append({"op": "insert_many", "table": table_name, "rows": new_rows})
        return new_rows

    def update(self, table_name: str, assignments: dict[str, str], where: str | None = None) -> int:
        table = self._table(table_name)
        names = {c.name for c in table.columns}
        if set(assignments) - names:
            raise ConstraintError(f"Unknown column(s): {', '.join(sorted(set(assignments) - names))}")
        count = 0
        for row in table.rows:
            if eval_predicate(where, row):
                old = row.copy()
                for col, expr in assignments.items():
                    row[col] = table._coerce(next(c for c in table.columns if c.name == col), eval_expr(expr, row))
                for col in table.columns:
                    if col.not_null and row[col.name] is None:
                        raise ConstraintError(f"Column {col.name} cannot be NULL")
                count += 1
                self.operations.append({"op": "update", "table": table_name, "before": old, "after": row.copy()})
        table.rebuild_indexes()
        return count

    def delete(self, table_name: str, where: str | None = None) -> int:
        table = self._table(table_name)
        kept = []
        deleted = []
        for row in table.rows:
            if eval_predicate(where, row):
                deleted.append(row)
            else:
                kept.append(row)
        table.rows = kept
        table.rebuild_indexes()
        for row in deleted:
            self.operations.append({"op": "delete", "table": table_name, "row": row})
        return len(deleted)

    def _fast_group_aggregate(self, rows: list[dict[str, Any]], expressions: list[str], group_by: list[str], alias_fns: dict[str, Any], order_by: tuple[str, bool] | None, limit: int | None) -> list[dict[str, Any]]:
        specs = []
        for expression in expressions:
            alias_match = re.match(r"^(.*?)\s+AS\s+(\w+)$", expression, re.IGNORECASE | re.DOTALL)
            raw = alias_match.group(1).strip() if alias_match else expression.strip()
            alias = alias_match.group(2) if alias_match else raw
            aggregate_match = re.match(r"^(COUNT|SUM|AVG|MIN|MAX)\s*\((.*?)\)$", raw, re.IGNORECASE | re.DOTALL)
            if aggregate_match:
                arg = aggregate_match.group(2).strip()
                specs.append((alias, aggregate_match.group(1).upper(), arg, compile_expr(arg)))
            else:
                specs.append((alias, "VALUE", raw, compile_expr(raw)))
        group_fns = []
        for col in group_by:
            group_fns.append(alias_fns.get(col) or compile_expr(col))
        if np is not None and len(rows) >= 256 and len(group_fns) == 1:
            try:
                keys = [group_fns[0](row) for row in rows]
                if all(isinstance(key, (str, int, float, bool)) or key is None for key in keys):
                    unique_keys, inverse = np.unique(np.asarray(keys, dtype=object), return_inverse=True)
                    vector_result = []
                    for group_index, key in enumerate(unique_keys):
                        mask = inverse == group_index
                        representative = rows[int(np.flatnonzero(mask)[0])]
                        output = {}
                        for alias, fn_name, arg, arg_fn in specs:
                            if fn_name == "VALUE":
                                output[alias] = arg_fn(representative)
                                continue
                            if arg == "*":
                                values = None
                            else:
                                raw_values = [arg_fn(row) for row in rows]
                                numeric = np.asarray([value if value is not None else np.nan for value in raw_values], dtype=float)
                                values = numeric[mask]
                            if fn_name == "COUNT":
                                output[alias] = int(mask.sum()) if arg == "*" else int(np.count_nonzero(~np.isnan(values)))
                            elif values is None:
                                output[alias] = None
                            elif np.all(np.isnan(values)):
                                output[alias] = None
                            elif fn_name == "SUM":
                                output[alias] = float(np.nansum(values))
                            elif fn_name == "AVG":
                                output[alias] = float(np.nanmean(values))
                            elif fn_name == "MIN":
                                output[alias] = float(np.nanmin(values))
                            elif fn_name == "MAX":
                                output[alias] = float(np.nanmax(values))
                        vector_result.append(output)
                    if order_by and vector_result and order_by[0] in vector_result[0]:
                        col, descending = order_by
                        vector_result.sort(key=lambda item: _sort_key(lambda value: value.get(col), item), reverse=descending)
                    return vector_result[:limit] if limit is not None else vector_result
            except (TypeError, ValueError):
                pass
        state: dict[tuple[Any, ...], dict[str, Any]] = {}
        representatives: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            key = tuple(fn(row) for fn in group_fns)
            current = state.setdefault(key, {})
            representatives.setdefault(key, row)
            for alias, fn_name, arg, arg_fn in specs:
                if fn_name == "VALUE":
                    continue
                value = 1 if arg == "*" else arg_fn(row)
                if fn_name == "COUNT":
                    if arg == "*" or value is not None:
                        current[alias] = current.get(alias, 0) + 1
                elif value is not None:
                    if fn_name == "SUM":
                        current[alias] = current.get(alias, 0) + value
                    elif fn_name == "AVG":
                        total, count = current.get(alias, (0, 0))
                        current[alias] = (total + value, count + 1)
                    elif fn_name == "MIN":
                        current[alias] = value if alias not in current else min(current[alias], value)
                    elif fn_name == "MAX":
                        current[alias] = value if alias not in current else max(current[alias], value)
        result = []
        for key, current in state.items():
            row = representatives[key]
            output = {}
            for alias, fn_name, arg, arg_fn in specs:
                if fn_name == "VALUE":
                    output[alias] = arg_fn(row)
                elif fn_name == "AVG":
                    total, count = current.get(alias, (0, 0))
                    output[alias] = total / count if count else None
                else:
                    output[alias] = current.get(alias, 0 if fn_name == "COUNT" else None)
            result.append(output)
        if order_by and result and order_by[0] in result[0]:
            col, descending = order_by
            result.sort(key=lambda item: _sort_key(lambda value: value.get(col), item), reverse=descending)
        return result[:limit] if limit is not None else result

    def select(self, table_name: str, projection: str = "*", where: str | None = None, group_by: list[str] | None = None, order_by: tuple[str, bool] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        table = self._table(table_name)
        rows = table.rows if not where else [row for row in table.rows if eval_predicate(where, row)]
        expressions = _split_csv(projection)
        alias_map: dict[str, str] = {}
        for expression in expressions:
            alias_match = re.match(r"^(.*?)\s+AS\s+(\w+)$", expression, re.IGNORECASE | re.DOTALL)
            if alias_match:
                alias_map[alias_match.group(2)] = alias_match.group(1).strip()
        alias_fns = {
            name: compile_expr(raw)
            for name, raw in alias_map.items()
            if not re.match(r"^(COUNT|SUM|AVG|MIN|MAX)\s*\(", raw, re.IGNORECASE)
        }
        if order_by and rows and order_by[0] not in rows[0]:
            order_expr, descending = order_by
            rows.sort(key=lambda item: _sort_key(compile_expr(order_expr), item), reverse=descending)
        aggregate = any(re.match(r"^(COUNT|SUM|AVG|MIN|MAX)\s*\(", expr.strip(), re.IGNORECASE) for expr in expressions)
        if aggregate and group_by:
            return self._fast_group_aggregate(rows, expressions, group_by, alias_fns, order_by, limit)
        if projection.strip() == "*":
            result = rows
        elif aggregate or group_by:
            groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
            if group_by:
                for row in rows:
                    key_values = []
                    for col in group_by:
                        key_values.append(row.get(col) if col in row else alias_fns.get(col, compile_expr(col))(row))
                    groups.setdefault(tuple(key_values), []).append(row)
            else:
                groups[()] = rows
            result = []
            for key, group in groups.items():
                base = group[0] if group else {}
                output: dict[str, Any] = {}
                for expr in expressions:
                    alias_match = re.match(r"^(.*?)\s+AS\s+(\w+)$", expr, re.IGNORECASE | re.DOTALL)
                    raw = alias_match.group(1).strip() if alias_match else expr.strip()
                    alias = alias_match.group(2) if alias_match else raw
                    agg = re.match(r"^(COUNT|SUM|AVG|MIN|MAX)\s*\((.*?)\)$", raw, re.IGNORECASE | re.DOTALL)
                    if agg:
                        fn, arg = agg.group(1).upper(), agg.group(2).strip()
                        values = [row.get(arg) for row in group] if arg != "*" else [1 for _ in group]
                        values = [v for v in values if v is not None]
                        if fn == "COUNT":
                            output[alias] = len(values)
                        elif not values:
                            output[alias] = None
                        elif fn == "SUM":
                            output[alias] = sum(values)
                        elif fn == "AVG":
                            output[alias] = sum(values) / len(values)
                        elif fn == "MIN":
                            output[alias] = min(values)
                        elif fn == "MAX":
                            output[alias] = max(values)
                    else:
                        output[alias] = eval_expr(raw, base)
                result.append(output)
        else:
            result = []
            for row in rows:
                output: dict[str, Any] = {}
                for expr in expressions:
                    alias_match = re.match(r"^(.*?)\s+AS\s+(\w+)$", expr, re.IGNORECASE | re.DOTALL)
                    raw = alias_match.group(1).strip() if alias_match else expr.strip()
                    alias = alias_match.group(2) if alias_match else raw
                    output[alias] = eval_expr(raw, row)
                result.append(output)
        if order_by and result and order_by[0] in result[0]:
            col, descending = order_by
            result.sort(key=lambda row: _sort_key(lambda item: item.get(col), row), reverse=descending)
        if limit is not None:
            result = result[:limit]
        return result

    def commit(self) -> None:
        if self.closed:
            raise NovaDBError("Transaction is closed")
        self.engine._commit(self)
        self.closed = True

    def rollback(self) -> None:
        self.closed = True


class Engine:
    """A compact embedded database with durable WAL and snapshot transactions."""

    def __init__(self, path: str | os.PathLike[str] = ":memory:"):
        self.path = Path(path)
        self.memory = str(path) == ":memory:"
        if not self.memory:
            self.path.mkdir(parents=True, exist_ok=True)
        self.state_file = None if self.memory else self.path / "state.json"
        self.wal_file = None if self.memory else self.path / "wal.log"
        self.page_store = None if self.memory else PageStore(self.path / "pages.dat")
        self.tables: dict[str, Table] = {}
        self.version = 0
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if self.memory:
            return
        if self.state_file and self.state_file.exists():
            data = json.loads(self.state_file.read_text())
            self.version = data.get("version", 0)
            self.tables = {name: _table_from_dict(table) for name, table in data.get("tables", {}).items()}
        if self.page_store is not None and self.page_store.path.exists():
            for record in self.page_store.iter_records():
                if record.get("version", 0) <= self.version:
                    continue
                self._apply_operations(record["operations"])
                self.version = max(self.version, record.get("version", self.version))
        elif self.wal_file and self.wal_file.exists():
            for line in self.wal_file.read_text().splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("version", 0) <= self.version:
                    continue
                self._apply_operations(record["operations"])
                self.version = max(self.version, record.get("version", self.version))

    def _apply_operations(self, operations: list[dict[str, Any]]) -> None:
        for item in operations:
            op = item["op"]
            if op == "create_table":
                self.tables[item["name"]] = Table(item["name"], [Column(**c) for c in item["columns"]])
            elif op == "create_index":
                table = self.tables[item["table"]]
                table.indexes[item["column"]] = {}
                table.rebuild_indexes()
            elif op == "insert":
                table = self.tables[item["table"]]
                table.rows.append(item["row"])
            elif op == "insert_many":
                self.tables[item["table"]].rows.extend(item["rows"])
            elif op == "update":
                table = self.tables[item["table"]]
                for row in table.rows:
                    if row == item["before"]:
                        row.clear()
                        row.update(item["after"])
                        break
            elif op == "delete":
                table = self.tables[item["table"]]
                try:
                    table.rows.remove(item["row"])
                except ValueError:
                    pass
        for table in self.tables.values():
            table.rebuild_indexes()

    def begin(self) -> Transaction:
        with self._lock:
            return Transaction(self)

    def _append_page_records(self, operations: list[dict[str, Any]], version: int) -> None:
        if self.page_store is not None and operations:
            self.page_store.append_records([{"version": version, "operations": operations}], sync=True)

    def prepare(self, sql: str):
        from .prepared import PreparedStatement
        return PreparedStatement(self, sql)

    def bulk_insert(self, table_name: str, column_names: list[str], values_groups: list[list[Any]]) -> dict[str, Any]:
        """Atomically append a validated batch without cloning unrelated tables."""
        with self._lock:
            if table_name not in self.tables:
                raise NovaDBError(f"Table does not exist: {table_name}")
            table = self.tables[table_name]
            new_rows, pending_keys = _prepare_many_rows(table, column_names, values_groups)
            operations = [{"op": "insert_many", "table": table_name, "rows": new_rows}]
            record = {"txid": uuid.uuid4().hex, "version": self.version + 1, "operations": operations, "ts": time.time()}
            if not self.memory:
                self._append_page_records(operations, record["version"])
            table.rows.extend(new_rows)
            table.primary_keys = pending_keys
            if table.indexes:
                table.rebuild_indexes()
            self.version += 1
            if not self.memory and self.version % 10 == 0:
                self.checkpoint()
            return {"status": "inserted", "count": len(new_rows), "rows": new_rows}

    def _commit(self, tx: Transaction) -> None:
        with self._lock:
            if tx.base_version != self.version:
                raise TransactionConflict("Concurrent commit detected; retry the transaction")
            record = {"txid": uuid.uuid4().hex, "version": self.version + 1, "operations": tx.operations, "ts": time.time()}
            if not self.memory and tx.operations:
                self._append_page_records(tx.operations, record["version"])
            self.tables = tx.tables
            self.version += 1
            if not self.memory and self.version % 10 == 0:
                self.checkpoint()

    def checkpoint(self) -> None:
        if self.memory:
            return
        assert self.state_file is not None and self.wal_file is not None
        payload = {"version": self.version, "tables": {name: _table_to_dict(table) for name, table in self.tables.items()}}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        tmp.replace(self.state_file)
        self.wal_file.write_text("")

    def close(self) -> None:
        self.checkpoint()

    def execute(self, sql: str) -> list[dict[str, Any]] | dict[str, Any]:
        sql = sql.strip().rstrip(";").strip()
        if not sql:
            return []
        upper = sql.upper()
        if upper.startswith("INSERT INTO"):
            table_name, column_names, values_groups = _parse_insert(sql)
            table = self.tables.get(table_name)
            if table is None:
                raise NovaDBError(f"Table does not exist: {table_name}")
            return self.bulk_insert(table_name, column_names or [column.name for column in table.columns], values_groups)
        if upper.startswith(("SELECT", "SHOW TABLES", "EXPLAIN")):
            tx = Transaction.__new__(Transaction)
            tx.engine = self
            tx.base_version = self.version
            tx.tables = self.tables
            tx.operations = []
            tx.closed = True
            return execute_in_transaction(tx, sql)
        tx = self.begin()
        try:
            result = execute_in_transaction(tx, sql)
            tx.commit()
            return result
        except Exception:
            tx.rollback()
            raise

    def explain(self, sql: str) -> dict[str, Any]:
        sql = sql.strip().rstrip(";")
        if not sql.upper().startswith("SELECT"):
            return {"operation": "write", "engine": "NovaDB optimistic WAL transaction"}
        return {"operation": "scan", "engine": "NovaDB vector-friendly row scan", "sql": sql, "features": ["snapshot visibility", "predicate pushdown", "aggregate pipeline"]}


def _table_to_dict(table: Table) -> dict[str, Any]:
    return {"name": table.name, "columns": [c.__dict__ for c in table.columns], "rows": table.rows, "indexes": list(table.indexes)}


def _table_from_dict(data: dict[str, Any]) -> Table:
    table = Table(data["name"], [Column(**c) for c in data["columns"]], data.get("rows", []))
    for column in data.get("indexes", []):
        table.indexes[column] = {}
    table.rebuild_indexes()
    return table


def _parse_create_table(sql: str) -> tuple[str, list[Column]]:
    match = re.match(r"^CREATE\s+TABLE\s+(\w+)\s*\((.*)\)$", sql, re.IGNORECASE | re.DOTALL)
    if not match:
        raise ParseError("Expected CREATE TABLE name (column TYPE, ...)")
    name = match.group(1)
    columns = []
    for definition in _split_csv(match.group(2)):
        parts = definition.split()
        if len(parts) < 2:
            raise ParseError(f"Invalid column definition: {definition}")
        col = Column(parts[0], parts[1].upper(), "PRIMARY" in [p.upper() for p in parts[2:]], "NOT" in [p.upper() for p in parts[2:]] and "NULL" in [p.upper() for p in parts[2:]])
        columns.append(col)
    return name, columns


def _parse_where_tail(tail: str) -> tuple[str | None, tuple[str, bool] | None, int | None, list[str] | None]:
    where = None
    order_by = None
    limit = None
    group_by = None
    tail = tail.strip()
    if not tail:
        return where, order_by, limit, group_by
    positions = []
    for keyword in ("WHERE", "GROUP BY", "ORDER BY", "LIMIT"):
        pos = _find_keyword(tail, keyword)
        if pos >= 0:
            positions.append((pos, keyword))
    positions.sort()
    for idx, (pos, keyword) in enumerate(positions):
        end = positions[idx + 1][0] if idx + 1 < len(positions) else len(tail)
        value = tail[pos + len(keyword):end].strip()
        if keyword == "WHERE":
            where = value
        elif keyword == "GROUP BY":
            group_by = [x.strip() for x in _split_csv(value)]
        elif keyword == "ORDER BY":
            order_match = re.match(r"^(.*?)(?:\s+(ASC|DESC))?$", value, re.IGNORECASE | re.DOTALL)
            order_expr = order_match.group(1).strip() if order_match else value
            order_by = (order_expr, bool(order_match and order_match.group(2) and order_match.group(2).upper() == "DESC"))
        elif keyword == "LIMIT":
            limit = int(value)
    return where, order_by, limit, group_by


def _parse_insert(sql: str) -> tuple[str, list[str] | None, list[list[Any]]]:
    match = re.match(r"^INSERT\s+INTO\s+(\w+)(?:\s*\((.*?)\))?\s+VALUES\s*(.*)$", sql, re.IGNORECASE | re.DOTALL)
    if not match:
        raise ParseError("Expected INSERT INTO table [(columns)] VALUES (...)")
    table = match.group(1)
    columns = [x.strip() for x in _split_csv(match.group(2))] if match.group(2) else None
    values_text = match.group(3).strip()
    groups = []
    depth = 0
    start = None
    quote: str | None = None
    for i, ch in enumerate(values_text):
        if quote:
            if ch == quote and values_text[i - 1] != "\\":
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "(":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and start is not None:
                groups.append([parse_value(x) for x in _split_csv(values_text[start:i])])
    if not groups:
        raise ParseError("INSERT requires at least one VALUES group")
    return table, columns, groups


def execute_in_transaction(tx: Transaction, sql: str) -> list[dict[str, Any]] | dict[str, Any]:
    upper = sql.upper()
    if upper.startswith("CREATE TABLE"):
        name, columns = _parse_create_table(sql)
        tx.create_table(name, columns)
        return {"status": "created", "table": name}
    if upper.startswith("CREATE INDEX"):
        match = re.match(r"^CREATE\s+INDEX\s+(\w+)\s+ON\s+(\w+)\s*\((\w+)\)$", sql, re.IGNORECASE)
        if not match:
            raise ParseError("Expected CREATE INDEX index ON table (column)")
        tx.create_index(match.group(2), match.group(3))
        return {"status": "indexed", "table": match.group(2), "column": match.group(3)}
    if upper.startswith("INSERT INTO"):
        table_name, columns, groups = _parse_insert(sql)
        table = tx._table(table_name)
        names = columns or [c.name for c in table.columns]
        inserted = []
        inserted = tx.insert_many(table_name, names, groups)
        return {"status": "inserted", "count": len(inserted), "rows": inserted}
    if upper.startswith("UPDATE"):
        match = re.match(r"^UPDATE\s+(\w+)\s+SET\s+(.*)$", sql, re.IGNORECASE | re.DOTALL)
        if not match:
            raise ParseError("Expected UPDATE table SET column = expression [WHERE predicate]")
        table_name = match.group(1)
        body = match.group(2)
        where_pos = _find_keyword(body, "WHERE")
        where = body[where_pos + 5:].strip() if where_pos >= 0 else None
        assignment_text = body[:where_pos].strip() if where_pos >= 0 else body
        assignments = {}
        for assignment in _split_csv(assignment_text):
            if "=" not in assignment:
                raise ParseError(f"Invalid assignment: {assignment}")
            col, expr = assignment.split("=", 1)
            assignments[col.strip()] = expr.strip()
        return {"status": "updated", "count": tx.update(table_name, assignments, where)}
    if upper.startswith("DELETE FROM"):
        match = re.match(r"^DELETE\s+FROM\s+(\w+)(.*)$", sql, re.IGNORECASE | re.DOTALL)
        if not match:
            raise ParseError("Expected DELETE FROM table [WHERE predicate]")
        tail = match.group(2).strip()
        where = tail[5:].strip() if tail.upper().startswith("WHERE") else None
        return {"status": "deleted", "count": tx.delete(match.group(1), where)}
    if upper.startswith("SELECT"):
        match = re.match(r"^SELECT\s+(.*?)\s+FROM\s+(\w+)(.*)$", sql, re.IGNORECASE | re.DOTALL)
        if not match:
            raise ParseError("Expected SELECT projection FROM table")
        projection, table_name, tail = match.group(1).strip(), match.group(2), match.group(3)
        where, order_by, limit, group_by = _parse_where_tail(tail)
        return tx.select(table_name, projection, where, group_by, order_by, limit)
    if upper == "SHOW TABLES":
        return [{"table": name} for name in sorted(tx.tables)]
    if upper.startswith("EXPLAIN "):
        return [{"plan": {"operation": "scan", "sql": sql[8:].strip(), "engine": "NovaDB"}}]
    raise ParseError(f"Unsupported SQL: {sql}")


def format_rows(rows: Iterable[dict[str, Any]]) -> str:
    rows = list(rows)
    if not rows:
        return "(0 rows)"
    columns = list(rows[0])
    widths = {col: max(len(col), *(len(json.dumps(row.get(col), ensure_ascii=False)) for row in rows)) for col in columns}
    line = "+" + "+".join("-" * (widths[col] + 2) for col in columns) + "+"
    out = [line, "|" + "|".join(f" {col.ljust(widths[col])} " for col in columns) + "|", line]
    for row in rows:
        out.append("|" + "|".join(f" {json.dumps(row.get(col), ensure_ascii=False).ljust(widths[col])} " for col in columns) + "|")
    out.append(line)
    out.append(f"({len(rows)} rows)")
    return "\n".join(out)
