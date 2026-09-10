"""File-backed bearer identities with PBKDF2 token storage and role checks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ITERATIONS = 310_000
ROLE_PERMISSIONS = {
    "reader": frozenset({"read", "memory_read", "prompt_preview"}),
    "operator": frozenset({"read", "write", "memory_read", "memory_write", "prompt_preview", "prompt_approve", "audit"}),
    "admin": frozenset({"read", "write", "memory_read", "memory_write", "prompt_preview", "prompt_approve", "audit", "identity_admin"}),
}


class IdentityError(RuntimeError):
    pass


@dataclass(frozen=True)
class Identity:
    username: str
    role: str
    salt: str
    digest: str
    disabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "role": self.role,
            "salt": self.salt,
            "digest": self.digest,
            "disabled": self.disabled,
        }


class IdentityStore:
    """Persist identities without writing bearer tokens to disk."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.identities: dict[str, Identity] = {}
        if self.path is not None and self.path.exists():
            self._load()

    @staticmethod
    def _validate_username(username: str) -> str:
        if not isinstance(username, str) or not username.strip() or len(username) > 120:
            raise IdentityError("username must be a non-empty short string")
        return username.strip()

    @staticmethod
    def _derive(token: str, salt: bytes) -> str:
        if not isinstance(token, str) or len(token) < 16:
            raise IdentityError("token must contain at least 16 characters")
        return hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), salt, ITERATIONS).hex()

    def _load(self) -> None:
        assert self.path is not None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("format") != "novadb-identities/v1":
                raise IdentityError("unsupported identity file format")
            self.identities = {
                item["username"]: Identity(**item)
                for item in payload.get("identities", [])
            }
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IdentityError("identity file is invalid") from exc
        for identity in self.identities.values():
            if identity.role not in ROLE_PERMISSIONS or len(identity.salt) != 32 or len(identity.digest) != 64:
                raise IdentityError("identity file contains invalid credentials")

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "format": "novadb-identities/v1",
            "iterations": ITERATIONS,
            "identities": [self.identities[name].to_dict() for name in sorted(self.identities)],
        }
        temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    def add(self, username: str, token: str, *, role: str = "operator") -> Identity:
        username = self._validate_username(username)
        if role not in ROLE_PERMISSIONS:
            raise IdentityError("unknown identity role")
        if username in self.identities:
            raise IdentityError("identity already exists")
        salt = secrets.token_bytes(16)
        identity = Identity(username, role, salt.hex(), self._derive(token, salt))
        self.identities[username] = identity
        self._save()
        return identity

    def issue(self, username: str, *, role: str = "operator") -> tuple[Identity, str]:
        token = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
        return self.add(username, token, role=role), token

    def revoke(self, username: str) -> None:
        identity = self.identities.get(username)
        if identity is None:
            raise IdentityError("identity does not exist")
        self.identities[username] = Identity(identity.username, identity.role, identity.salt, identity.digest, True)
        self._save()

    def authenticate(self, token: str | None) -> Identity | None:
        if not token or not isinstance(token, str) or len(token) < 16:
            return None
        for identity in self.identities.values():
            if identity.disabled:
                continue
            candidate = self._derive(token, bytes.fromhex(identity.salt))
            if hmac.compare_digest(candidate, identity.digest):
                return identity
        return None

    @staticmethod
    def allowed(identity: Identity, permission: str) -> bool:
        return permission in ROLE_PERMISSIONS.get(identity.role, frozenset())
