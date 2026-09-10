"""Fail-closed evaluator for NovaDB's software production boundary."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GATE_VERSION = "novadb-production-gate/v1"


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool
    evidence: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "evidence": self.evidence,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ProductionGateReport:
    status: str
    production_approved: bool
    gate_version: str
    checks: tuple[GateCheck, ...]
    evidence_digest: str
    checked_at_ns: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "production_approved": self.production_approved,
            "gate_version": self.gate_version,
            "checks": [check.to_dict() for check in self.checks],
            "evidence_digest": self.evidence_digest,
            "checked_at_ns": self.checked_at_ns,
        }


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _section(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    value = manifest.get(name)
    return value if isinstance(value, dict) else {}


def _passed(section: dict[str, Any], *keys: str) -> bool:
    return all(section.get(key) is True for key in keys)


def evaluate_production_gate(manifest: dict[str, Any]) -> ProductionGateReport:
    """Evaluate supplied evidence without granting operational approval.

    The manifest is deliberately external to the engine. A passing software
    test cannot stand in for an independent recovery drill, key-management
    review, or production deployment approval.
    """
    if not isinstance(manifest, dict):
        raise ValueError("production manifest must be an object")

    tests = _section(manifest, "regression_tests")
    storage = _section(manifest, "durable_storage")
    recovery = _section(manifest, "crash_recovery")
    backup = _section(manifest, "backup_restore")
    mvcc = _section(manifest, "sql_mvcc")
    identity = _section(manifest, "identity_security")
    operations = _section(manifest, "operations")
    checks = (
        GateCheck(
            "regression_tests",
            _passed(tests, "passed", "reproducible", "revision_pinned"),
            "regression_tests",
            "run the dependency-free and integration suites at a pinned revision",
        ),
        GateCheck(
            "durable_storage",
            _passed(storage, "slotted_pages", "durable_indexes", "checksummed_frames"),
            "durable_storage",
            "verify slotted allocation, durable index images, and checksummed frames under the target filesystem",
        ),
        GateCheck(
            "crash_recovery",
            _passed(recovery, "fault_matrix_passed", "reopen_verified", "no_data_loss"),
            "crash_recovery",
            "complete the fsync-boundary crash matrix and verify recovery after every injected interruption",
        ),
        GateCheck(
            "backup_restore",
            _passed(backup, "checksums_verified", "restore_reopened", "restore_drill_passed"),
            "backup_restore",
            "complete a scheduled backup, checksum validation, restore, and reopen drill",
        ),
        GateCheck(
            "sql_mvcc",
            _passed(mvcc, "integrated", "snapshot_reads", "serializable_validation", "recovery_rules"),
            "sql_mvcc",
            "integrate row versions and serializable validation into SQL transactions and recovery",
        ),
        GateCheck(
            "identity_security",
            _passed(identity, "least_privilege", "rotation_tested", "external_identity_reviewed"),
            "identity_security",
            "review external identity, role boundaries, token rotation, and operational credential handling",
        ),
        GateCheck(
            "operations",
            _passed(operations, "encryption_key_management", "durable_audit_retention", "consensus_transport", "observability"),
            "operations",
            "provide key management, retained audit, real consensus transport, and operational observability",
        ),
        GateCheck(
            "independent_release_review",
            manifest.get("independent_review") is True,
            "independent_review",
            "obtain separate security, reliability, and operations sign-off",
        ),
    )
    passed = all(check.passed for check in checks)
    status = "eligible_for_independent_production_review" if passed else "blocked"
    checked_at_ns = int(manifest.get("checked_at_ns", time.time_ns()))
    payload = {
        "status": status,
        "production_approved": False,
        "gate_version": GATE_VERSION,
        "checks": [check.to_dict() for check in checks],
        "checked_at_ns": checked_at_ns,
    }
    return ProductionGateReport(
        status=status,
        production_approved=False,
        gate_version=GATE_VERSION,
        checks=checks,
        evidence_digest=_digest(payload),
        checked_at_ns=checked_at_ns,
    )


def load_manifest(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("production manifest is missing or invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("production manifest must be an object")
    return payload
