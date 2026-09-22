"""Regression tests for the engine correctness fixes.

Plain pytest-discoverable functions; the critical ones are also wired into
``test_runner.py`` (the CI-only execution path), so keep every function here
dependency-free (no fixtures).
"""
from __future__ import annotations

from novadb import ConstraintError, Engine, NovaDBError, ParseError, Transaction


def test_string_literals_with_operators_are_preserved() -> None:
    db = Engine()
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'x-y'), (2, 'a+b'), (3, 'a/b'), (4, 'a*b')")
    # SELECT projection must return the literal, not None / mangled pieces.
    assert db.execute("SELECT 'x-y' AS q FROM t LIMIT 1") == [{"q": "x-y"}]
    assert db.execute("SELECT 'a+b' AS q FROM t LIMIT 1") == [{"q": "a+b"}]
    assert db.execute("SELECT 'a/b' AS q FROM t LIMIT 1") == [{"q": "a/b"}]
    assert db.execute("SELECT 'a*b' AS q FROM t LIMIT 1") == [{"q": "a*b"}]
    # WHERE must match rows whose values contain operators.
    assert db.execute("SELECT * FROM t WHERE name = 'x-y'") == [{"id": 1, "name": "x-y"}]
    assert db.execute("SELECT * FROM t WHERE name = 'a/b'") == [{"id": 3, "name": "a/b"}]
    # UPDATE must store the literal, not NULL.
    db.execute("UPDATE t SET name = 'new-name' WHERE id = 4")
    assert db.execute("SELECT name FROM t WHERE id = 4") == [{"name": "new-name"}]


def test_quoted_literals_bear_escaped_quotes_and_empties() -> None:
    db = Engine()
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'it''s'), (2, '')")
    assert db.execute("SELECT name FROM t WHERE id = 1") == [{"name": "it's"}]
    assert db.execute("SELECT name FROM t WHERE id = 2") == [{"name": ""}]


def test_arithmetic_still_evaluates_after_literal_fix() -> None:
    db = Engine()
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, n INT)")
    db.execute("INSERT INTO t VALUES (1, 4), (2, 7)")
    assert db.execute("SELECT n + 1 AS x FROM t WHERE id = 1") == [{"x": 5}]
    assert db.execute("SELECT n * 2 AS x FROM t ORDER BY id") == [{"x": 8}, {"x": 14}]
    # Quoted lookalikes stay strings: "5-3" is a literal, not 5 - 3.
    assert db.execute("SELECT '5-3' AS q FROM t LIMIT 1") == [{"q": "5-3"}]


def test_update_rejects_colliding_primary_key() -> None:
    db = Engine()
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'one'), (2, 'two')")
    try:
        db.execute("UPDATE t SET id = 1 WHERE id = 2")
    except ConstraintError as exc:
        assert "Duplicate primary key" in str(exc)
    else:
        raise AssertionError("UPDATE must reject a primary-key collision")
    # Rows are untouched and the key sets are still consistent.
    assert db.execute("SELECT * FROM t ORDER BY id") == [
        {"id": 1, "name": "one"},
        {"id": 2, "name": "two"},
    ]
    # In-place / no-op updates of the key are fine.
    assert db.execute("UPDATE t SET id = id WHERE id = 1")["count"] == 1
    assert db.execute("SELECT * FROM t ORDER BY id") == [
        {"id": 1, "name": "one"},
        {"id": 2, "name": "two"},
    ]


def test_update_pk_collision_via_transaction_api() -> None:
    db = Engine()
    db.execute("CREATE TABLE t (id INT PRIMARY KEY)")
    db.execute("INSERT INTO t VALUES (1), (2)")
    tx = db.begin()
    try:
        tx.update("t", {"id": "2"}, "id = 1")
    except ConstraintError:
        pass
    else:
        raise AssertionError("tx-level UPDATE must reject a PK collision")
    tx.insert("t", {"id": 3})
    tx.commit()
    assert db.execute("SELECT * FROM t ORDER BY id") == [
        {"id": 1},
        {"id": 2},
        {"id": 3},
    ]


def test_failed_update_rolls_back_all_rows_and_log() -> None:
    db = Engine()
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, n INT)")
    db.execute("INSERT INTO t VALUES (1, 10), (2, 20), (3, 30)")
    try:
        db.execute("UPDATE t SET n = 'not-an-int' WHERE id >= 1")
    except ConstraintError:
        pass
    else:
        raise AssertionError("coercion failure must raise")
    # Every row must be unchanged — a mid-loop failure used to leave the
    # rows already visited mutated.
    assert db.execute("SELECT * FROM t ORDER BY id") == [
        {"id": 1, "n": 10},
        {"id": 2, "n": 20},
        {"id": 3, "n": 30},
    ]
    # NOT NULL failure on a later row must also roll back earlier successes.
    db.execute("CREATE TABLE u (id INT PRIMARY KEY, n INT NOT NULL)")
    db.execute("INSERT INTO u VALUES (1, 10), (2, 20)")
    try:
        db.execute("UPDATE u SET n = NULL WHERE id >= 1")
    except ConstraintError:
        pass
    else:
        raise AssertionError("NOT NULL violation must raise")
    assert db.execute("SELECT * FROM u ORDER BY id") == [
        {"id": 1, "n": 10},
        {"id": 2, "n": 20},
    ]


def test_failed_update_leaves_transaction_usable() -> None:
    db = Engine()
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, n INT)")
    db.execute("INSERT INTO t VALUES (1, 10), (2, 20)")
    tx: Transaction = db.begin()
    try:
        tx.update("t", {"n": "junk"}, "id = 2")
    except ConstraintError:
        pass
    tx.insert("t", {"id": 3, "n": 30})
    tx.commit()
    assert db.execute("SELECT * FROM t ORDER BY id") == [
        {"id": 1, "n": 10},
        {"id": 2, "n": 20},
        {"id": 3, "n": 30},
    ]


def test_update_duplicate_pk_rolls_back_log_entries() -> None:
    db = Engine()
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, n INT)")
    db.execute("INSERT INTO t VALUES (1, 10), (2, 20)")
    tx = db.begin()
    try:
        tx.update("t", {"id": "1", "n": "99"}, "id = 2")
    except ConstraintError:
        pass
    # The failed update must not have appended operations: the commit below
    # must only carry the INSERT.
    tx.insert("t", {"id": 3, "n": 30})
    tx.commit()
    rows = db.execute("SELECT * FROM t ORDER BY id")
    assert rows == [{"id": 1, "n": 10}, {"id": 2, "n": 20}, {"id": 3, "n": 30}], rows


def test_limit_rejects_negative_and_garbage() -> None:
    db = Engine()
    db.execute("CREATE TABLE t (id INT PRIMARY KEY)")
    db.execute("INSERT INTO t VALUES (1), (2), (3), (4)")
    for bad in ("-1", "-2", "abc", "1.5", ""):
        try:
            db.execute(f"SELECT * FROM t LIMIT {bad}")
        except ParseError:
            continue
        raise AssertionError(f"LIMIT {bad!r} must raise ParseError")
    assert db.execute("SELECT * FROM t LIMIT 0") == []
    assert db.execute("SELECT * FROM t LIMIT 2") == [{"id": 1}, {"id": 2}]
    assert len(db.execute("SELECT * FROM t LIMIT 99")) == 4


def test_select_without_from() -> None:
    db = Engine()
    assert db.execute("SELECT 1 + 1 AS s") == [{"s": 2}]
    assert db.execute("SELECT 3 * 4 AS p, 'x-y'") == [{"p": 12, "'x-y'": "x-y"}]
    assert db.execute("SELECT 'a' || 'b'")[0] is not None
    assert db.execute("SELECT UPPER('ab') AS u") == [{"u": "AB"}]
    # SELECT ... still requires FROM once a table is referenced.
    db.execute("CREATE TABLE t (id INT PRIMARY KEY)")
    db.execute("INSERT INTO t VALUES (1)")
    assert db.execute("SELECT id FROM t") == [{"id": 1}]


def test_where_tail_parsing_with_quoted_operator_values() -> None:
    """The dead O(n^2) scan removal must not break keyword partitioning."""
    db = Engine()
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'a - b'), (2, 'plain')")
    # A hyphenated value used to be split into pieces by the arithmetic
    # misparse; with the literal fix the full WHERE/ORDER/LIMIT tail parses.
    rows = db.execute("SELECT * FROM t WHERE name = 'a - b' ORDER BY id DESC LIMIT 1")
    assert rows == [{"id": 1, "name": "a - b"}], rows
    rows = db.execute("SELECT * FROM t WHERE id >= 1 ORDER BY id LIMIT 1")
    assert rows == [{"id": 1, "name": "a - b"}], rows


def test_find_keyword_partitions_order_by_with_function_args() -> None:
    db = Engine()
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'xx'), (2, 'a'), (3, 'xxx')")
    # Function sort key with DESC still parses after the keyword-scan change.
    rows = db.execute("SELECT name FROM t ORDER BY LENGTH(name) DESC, id LIMIT 2")
    assert rows == [{"name": "xxx"}, {"name": "xx"}], rows
    # Multi-key ORDER BY applies every key, with per-key direction.
    rows = db.execute("SELECT id, name FROM t ORDER BY name ASC, id DESC")
    assert rows == [
        {"id": 2, "name": "a"},
        {"id": 1, "name": "xx"},
        {"id": 3, "name": "xxx"},
    ], rows