from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from .engine import ParseError, _json_path, _split_boolean, _split_csv, _strip_outer_parens, parse_value, vector_distance


@dataclass(frozen=True)
class Instruction:
    opcode: str
    operand: Any = None


class BytecodeProgram:
    """Small stack VM program compiled once and reusable for many rows."""

    def __init__(self, instructions: list[Instruction], parameter_count: int = 0):
        self.instructions = tuple(instructions)
        self.parameter_count = parameter_count

    def run(self, row: dict[str, Any], params: tuple[Any, ...] = ()) -> Any:
        if len(params) < self.parameter_count:
            raise ValueError(f"expected {self.parameter_count} parameters, got {len(params)}")
        stack: list[Any] = []
        for instruction in self.instructions:
            op, operand = instruction.opcode, instruction.operand
            if op == "LOAD_COL":
                stack.append(row.get(operand))
            elif op == "CONST":
                stack.append(operand)
            elif op == "LOAD_PARAM":
                stack.append(params[operand])
            elif op == "CALL":
                name, arity = operand
                args = stack[-arity:] if arity else []
                if arity:
                    del stack[-arity:]
                stack.append(_call(name, args))
            elif op == "ADD":
                right, left = stack.pop(), stack.pop()
                stack.append(None if left is None or right is None else left + right)
            elif op == "SUB":
                right, left = stack.pop(), stack.pop()
                stack.append(None if left is None or right is None else left - right)
            elif op == "MUL":
                right, left = stack.pop(), stack.pop()
                stack.append(None if left is None or right is None else left * right)
            elif op == "DIV":
                right, left = stack.pop(), stack.pop()
                try:
                    stack.append(None if left is None or right is None else left / right)
                except ZeroDivisionError:
                    stack.append(None)
            elif op == "CMP":
                right, left = stack.pop(), stack.pop()
                stack.append(_compare(left, right, operand))
            elif op == "IS_NULL":
                stack.append(stack.pop() is None)
            elif op == "IS_NOT_NULL":
                stack.append(stack.pop() is not None)
            elif op == "NOT":
                stack.append(not bool(stack.pop()))
            elif op == "AND":
                right, left = stack.pop(), stack.pop()
                stack.append(bool(left) and bool(right))
            elif op == "OR":
                right, left = stack.pop(), stack.pop()
                stack.append(bool(left) or bool(right))
            else:
                raise RuntimeError(f"unknown bytecode opcode: {op}")
        return stack[-1] if stack else None

    def explain(self) -> list[dict[str, Any]]:
        return [{"opcode": instruction.opcode, "operand": instruction.operand} for instruction in self.instructions]


def _call(name: str, args: list[Any]) -> Any:
    name = name.upper()
    if name == "JSON_EXTRACT":
        if len(args) != 2:
            raise ParseError("JSON_EXTRACT requires a value and a path")
        return _json_path(args[0], str(args[1]))
    if name in {"VECTOR_DISTANCE", "COSINE_DISTANCE", "L2_DISTANCE"}:
        return vector_distance(args[0], args[1], "l2" if name == "L2_DISTANCE" else "cosine")
    if name == "LENGTH":
        return len(args[0]) if args and args[0] is not None else None
    if name == "UPPER":
        return str(args[0]).upper() if args and args[0] is not None else None
    if name == "LOWER":
        return str(args[0]).lower() if args and args[0] is not None else None
    if name == "COALESCE":
        return next((value for value in args if value is not None), None)
    if name == "ABS":
        return abs(args[0]) if args and args[0] is not None else None
    raise ParseError(f"Unsupported function: {name}")


def _compare(left: Any, right: Any, operator: str) -> bool:
    if left is None or right is None:
        return operator == "=" and left is None and right is None
    try:
        return {"=": left == right, "!=": left != right, "<>": left != right, "<": left < right, ">": left > right, "<=": left <= right, ">=": left >= right}[operator]
    except TypeError:
        return False


class _Compiler:
    def __init__(self):
        self.instructions: list[Instruction] = []
        self.parameter_count = 0

    def compile_value(self, expr: str) -> None:
        expr = _strip_outer_parens(expr.strip())
        if expr == "?":
            self.instructions.append(Instruction("LOAD_PARAM", self.parameter_count))
            self.parameter_count += 1
            return
        function = re.match(r"^(\w+)\s*\((.*)\)$", expr, re.DOTALL)
        if function:
            name = function.group(1).upper()
            args = _split_csv(function.group(2))
            for arg in args:
                self.compile_value(arg)
            self.instructions.append(Instruction("CALL", (name, len(args))))
            return
        arithmetic = re.match(r"^(.*?)\s*([+\-*/])\s*(.*?)$", expr)
        if arithmetic and not (expr.startswith(("'", '"')) and expr.endswith(("'", '"'))):
            self.compile_value(arithmetic.group(1))
            self.compile_value(arithmetic.group(3))
            self.instructions.append(Instruction({"+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV"}[arithmetic.group(2)]))
            return
        identifier = re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?", expr)
        if identifier:
            self.instructions.append(Instruction("LOAD_COL", expr.split(".", 1)[-1]))
            return
        self.instructions.append(Instruction("CONST", parse_value(expr)))

    def compile_predicate(self, expr: str) -> None:
        expr = _strip_outer_parens(expr)
        ors = _split_boolean(expr, "OR")
        if len(ors) > 1:
            self.compile_predicate(ors[0])
            for part in ors[1:]:
                self.compile_predicate(part)
                self.instructions.append(Instruction("OR"))
            return
        ands = _split_boolean(expr, "AND")
        if len(ands) > 1:
            self.compile_predicate(ands[0])
            for part in ands[1:]:
                self.compile_predicate(part)
                self.instructions.append(Instruction("AND"))
            return
        null_match = re.match(r"^(.*?)\s+IS\s+(NOT\s+)?NULL$", expr, re.IGNORECASE)
        if null_match:
            self.compile_value(null_match.group(1))
            self.instructions.append(Instruction("IS_NOT_NULL" if null_match.group(2) else "IS_NULL"))
            return
        comparison = re.match(r"^(.*?)\s*(<=|>=|<>|!=|=|<|>)\s*(.*?)$", expr, re.DOTALL)
        if comparison:
            self.compile_value(comparison.group(1))
            self.compile_value(comparison.group(3))
            self.instructions.append(Instruction("CMP", comparison.group(2)))
            return
        self.compile_value(expr)


def compile_expression(expr: str) -> BytecodeProgram:
    compiler = _Compiler()
    compiler.compile_value(expr)
    return BytecodeProgram(compiler.instructions, compiler.parameter_count)


def compile_predicate(expr: str | None) -> BytecodeProgram | None:
    if not expr:
        return None
    compiler = _Compiler()
    compiler.compile_predicate(expr)
    return BytecodeProgram(compiler.instructions, compiler.parameter_count)
