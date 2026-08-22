from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import Engine, NovaDBError, format_rows


def run_script(engine: Engine, text: str) -> None:
    statements = [part.strip() for part in text.split(";") if part.strip()]
    for statement in statements:
        result = engine.execute(statement)
        if isinstance(result, list):
            print(format_rows(result))
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False))


def repl(engine: Engine) -> None:
    print("NovaDB 0.1 — embedded SQL engine; type .help or .quit")
    buffer = ""
    while True:
        try:
            line = input("novadb> " if not buffer else "     -> ")
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            buffer = ""
            continue
        if not buffer and line.strip() in {".quit", ".exit"}:
            break
        if not buffer and line.strip() == ".help":
            print("SQL: CREATE TABLE, CREATE INDEX, INSERT, SELECT, UPDATE, DELETE, SHOW TABLES, EXPLAIN")
            print("Meta: .tables, .schema TABLE, .quit")
            continue
        if not buffer and line.strip() == ".tables":
            print(format_rows(engine.execute("SHOW TABLES")))
            continue
        if not buffer and line.strip().startswith(".schema"):
            name = line.strip().split(maxsplit=1)[1] if len(line.strip().split()) > 1 else None
            if name and name in engine.tables:
                print(json.dumps({"table": name, "columns": [c.__dict__ for c in engine.tables[name].columns]}, indent=2))
            else:
                print("unknown table")
            continue
        buffer += (" " if buffer else "") + line.strip()
        if not buffer.endswith(";"):
            continue
        try:
            result = engine.execute(buffer)
            print(format_rows(result) if isinstance(result, list) else json.dumps(result, indent=2, ensure_ascii=False))
        except NovaDBError as exc:
            print(f"error: {exc}", file=sys.stderr)
        finally:
            buffer = ""


def main() -> None:
    parser = argparse.ArgumentParser(description="NovaDB embedded SQL engine")
    parser.add_argument("path", nargs="?", default=":memory:", help="database directory, or :memory:")
    parser.add_argument("--sql", help="execute one SQL statement")
    parser.add_argument("--file", type=Path, help="execute semicolon-separated SQL script")
    args = parser.parse_args()
    engine = Engine(args.path)
    try:
        if args.sql:
            result = engine.execute(args.sql)
            print(format_rows(result) if isinstance(result, list) else json.dumps(result, indent=2, ensure_ascii=False))
        elif args.file:
            run_script(engine, args.file.read_text())
        else:
            repl(engine)
    finally:
        engine.close()


if __name__ == "__main__":
    main()
