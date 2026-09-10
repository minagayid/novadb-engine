"""Checksummed offline backup and restore for a NovaDB directory."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


BACKUP_VERSION = 1


class BackupError(RuntimeError):
    pass


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(source: Path) -> list[Path]:
    return sorted(
        path for path in source.rglob("*")
        if path.is_file() and not path.name.endswith(".tmp")
    )


def create_backup(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> dict[str, Any]:
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if not source_path.is_dir():
        raise BackupError("backup source is not a directory")
    if destination_path == source_path or source_path in destination_path.parents:
        raise BackupError("backup destination must be outside the database directory")
    files = _files(source_path)
    manifest = {
        "backup_version": BACKUP_VERSION,
        "database": source_path.name,
        "files": [
            {"path": path.relative_to(source_path).as_posix(), "size": path.stat().st_size, "sha256": _digest(path)}
            for path in files
        ],
    }
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_suffix(destination_path.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(source_path).as_posix())
        archive.writestr("BACKUP-MANIFEST.json", json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    with temporary.open("rb+") as handle:
        os.fsync(handle.fileno())
    temporary.replace(destination_path)
    return {
        "status": "created",
        "path": str(destination_path),
        "file_count": len(files),
        "sha256": _digest(destination_path),
    }


def restore_backup(archive_path: str | os.PathLike[str], destination: str | os.PathLike[str]) -> dict[str, Any]:
    archive_path = Path(archive_path).resolve()
    destination_path = Path(destination).resolve()
    if not archive_path.is_file():
        raise BackupError("backup archive does not exist")
    if destination_path.exists():
        raise BackupError("restore destination must not already exist")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="novadb-restore-", dir=str(destination_path.parent)))
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            try:
                manifest = json.loads(archive.read("BACKUP-MANIFEST.json").decode("utf-8"))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BackupError("backup manifest is missing or invalid") from exc
            if manifest.get("backup_version") != BACKUP_VERSION:
                raise BackupError("unsupported backup version")
            expected = {str(item["path"]): item for item in manifest.get("files", [])}
            if len(expected) != len(manifest.get("files", [])):
                raise BackupError("backup manifest contains duplicate files")
            names = {info.filename for info in archive.infolist() if info.filename != "BACKUP-MANIFEST.json"}
            if names != set(expected):
                raise BackupError("backup contents do not match the manifest")
            for name, item in expected.items():
                relative = PurePosixPath(name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise BackupError("backup contains an unsafe path")
                target = stage.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source_handle, target.open("wb") as target_handle:
                    shutil.copyfileobj(source_handle, target_handle)
                if target.stat().st_size != int(item["size"]) or _digest(target) != item["sha256"]:
                    raise BackupError(f"backup checksum mismatch for {name}")
        stage.replace(destination_path)
        return {"status": "restored", "path": str(destination_path), "file_count": len(expected)}
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
