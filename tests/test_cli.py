"""CLI integration tests for ``python -m novadb``.

Run via subprocess against the real module entry point. Plain functions so the
critical ones can be wired into ``tests/test_runner.py`` (the CI-only path).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "novadb", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin"},
        timeout=60,
    )


def test_cli_version_exits_zero() -> None:
    result = _run_cli("--version")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("novadb "), result.stdout


def test_cli_sql_statement_prints_rows() -> None:
    result = _run_cli(":memory:", "--sql", "SELECT 1 + 1 AS s")
    assert result.returncode == 0, result.stderr
    assert "| 2 |" in result.stdout, result.stdout


def test_cli_sql_preserves_operator_literals() -> None:
    result = _run_cli(":memory:", "--sql", "SELECT 'x-y' AS q")
    assert result.returncode == 0, result.stderr
    assert "x-y" in result.stdout, result.stdout


def test_cli_sql_error_exits_one_no_traceback() -> None:
    result = _run_cli(":memory:", "--sql", "SELECT * FROM missing")
    assert result.returncode == 1, result.returncode
    assert result.stdout == "", result.stdout
    assert "error:" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def test_cli_bad_sql_exits_one() -> None:
    result = _run_cli(":memory:", "--sql", "NOT SQL AT ALL")
    assert result.returncode == 1, result.returncode
    assert "Traceback" not in result.stderr, result.stderr


def test_cli_missing_file_exits_one() -> None:
    result = _run_cli(":memory:", "--file", "/nonexistent/nowhere.sql")
    assert result.returncode == 1, result.returncode
    assert "error:" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def test_cli_file_script_runs_all_statements() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "script.sql"
        script.write_text(
            "CREATE TABLE t (id INT PRIMARY KEY, name TEXT);\n"
            "INSERT INTO t VALUES (1, 'x-y');\n"
            "SELECT * FROM t WHERE name = 'x-y';\n",
            encoding="utf-8",
        )
        result = _run_cli(":memory:", "--file", str(script))
        assert result.returncode == 0, result.stderr
        assert "created" in result.stdout, result.stdout
        assert "x-y" in result.stdout, result.stdout
        assert "1 rows" in result.stdout, result.stdout


def test_cli_file_script_error_exits_one() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "broken.sql"
        script.write_text("SELECT * FROM nope;\n", encoding="utf-8")
        result = _run_cli(":memory:", "--file", str(script))
        assert result.returncode == 1, result.returncode
        assert "error:" in result.stderr, result.stderr


def test_cli_backup_on_memory_exits_one() -> None:
    result = _run_cli(":memory:", "--backup", "/tmp/never-written.novabackup")
    assert result.returncode == 1, result.returncode
    assert "in-memory" in result.stderr, result.stderr


def test_cli_durable_backup_writes_archive() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_dir = Path(tmp) / "db"
        archive = Path(tmp) / "db.novabackup"
        create = _run_cli(str(db_dir), "--sql", "CREATE TABLE t (id INT PRIMARY KEY)")
        assert create.returncode == 0, create.stderr
        backup = _run_cli(str(db_dir), "--backup", str(archive))
        assert backup.returncode == 0, backup.stderr
        assert archive.exists() and archive.stat().st_size > 0


def test_cli_restore_requires_restore_to() -> None:
    result = _run_cli(":memory:", "--restore", "/tmp/x.novabackup")
    assert result.returncode == 2, result.returncode  # argparse usage error
    assert "--restore requires --restore-to" in result.stderr, result.stderr


def test_cli_production_gate_missing_manifest_exits_one() -> None:
    result = _run_cli("--production-gate", "/nonexistent/manifest.json")
    assert result.returncode == 1, result.returncode
    assert "error:" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def test_cli_durable_roundtrip_via_separate_processes() -> None:
    """CREATE + INSERT in one process, SELECT in another (real durability)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_dir = Path(tmp) / "db"
        script = Path(tmp) / "seed.sql"
        script.write_text(
            "CREATE TABLE t (id INT PRIMARY KEY, name TEXT);\n"
            "INSERT INTO t VALUES (1, 'x-y');\n",
            encoding="utf-8",
        )
        seed = _run_cli(str(db_dir), "--file", str(script))
        assert seed.returncode == 0, seed.stderr
        read = _run_cli(str(db_dir), "--sql", "SELECT * FROM t")
        assert read.returncode == 0, read.stderr
        assert "x-y" in read.stdout, read.stdout