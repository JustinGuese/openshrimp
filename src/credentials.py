"""Encrypted credential vault for openshrimp.

Provides a per-project, per-user key-value store backed by PostgreSQL and
encrypted at rest with a Fernet symmetric key from the OPENSHRIMP_VAULT_KEY
environment variable.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Iterable

from cryptography.fernet import Fernet, InvalidToken
from sqlmodel import Session, select

import db as _db
from models import Credential

_VAULT_KEY_ENV = "OPENSHRIMP_VAULT_KEY"
_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    """Return a cached Fernet instance using OPENSHRIMP_VAULT_KEY.

    The key must be a 32-byte urlsafe base64 value (Fernet.generate_key()).
    """
    global _fernet
    if _fernet is not None:
        return _fernet
    raw = os.environ.get(_VAULT_KEY_ENV, "").strip()
    if not raw:
        raise RuntimeError(
            f"{_VAULT_KEY_ENV} is not set. Generate a key with:\n"
            "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\n"
            "and set it in your environment to enable the credential vault."
        )
    try:
        key_bytes = raw.encode("utf-8")
        _fernet = Fernet(key_bytes)
        return _fernet
    except Exception as e:  # pragma: no cover - defensive
        raise RuntimeError(
            f"{_VAULT_KEY_ENV} must be a valid Fernet key "
            "(32 url-safe base64-encoded bytes)."
        ) from e


def _ensure_db() -> None:
    _db.init_db()


def store_secret(
    *,
    project_id: int | None,
    name: str,
    value: str,
    user_id: int | None = None,
) -> None:
    """Encrypt and store a secret value for (project_id, user_id, name).

    If a row already exists for this triplet, it is overwritten.
    """
    f = _get_fernet()
    token = f.encrypt(value.encode("utf-8")).decode("utf-8")
    _ensure_db()
    with Session(_db.get_engine()) as session:
        now = datetime.now()
        row = session.exec(
            select(Credential).where(
                Credential.project_id == project_id,
                Credential.user_id == user_id,
                Credential.name == name,
            )
        ).first()
        if row is None:
            row = Credential(
                project_id=project_id,
                user_id=user_id,
                name=name,
                value_encrypted=token,
                created_at=now,
                updated_at=now,
                last_used_at=None,
            )
            session.add(row)
        else:
            row.value_encrypted = token
            row.updated_at = now
        session.commit()


def get_secret(
    *,
    project_id: int | None,
    name: str,
    user_id: int | None = None,
) -> str | None:
    """Return the decrypted secret for (project_id, user_id, name), or None."""
    f = _get_fernet()
    _ensure_db()
    with Session(_db.get_engine()) as session:
        row = session.exec(
            select(Credential).where(
                Credential.project_id == project_id,
                Credential.user_id == user_id,
                Credential.name == name,
            )
        ).first()
        if row is None:
            return None
        try:
            plaintext = f.decrypt(row.value_encrypted.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            # Corrupted or invalid key — treat as missing
            return None
        row.last_used_at = datetime.now()
        row.updated_at = datetime.now()
        session.add(row)
        session.commit()
        return plaintext


def list_secret_names(
    *,
    project_id: int | None,
    user_id: int | None = None,
) -> list[str]:
    """Return all stored secret names for a project/user (values are not returned)."""
    _ensure_db()
    with Session(_db.get_engine()) as session:
        rows: Iterable[Credential] = session.exec(
            select(Credential).where(
                Credential.project_id == project_id,
                Credential.user_id == user_id,
            ).order_by(Credential.name)
        )
        return [r.name for r in rows]


def delete_secret(
    *,
    project_id: int | None,
    name: str,
    user_id: int | None = None,
) -> bool:
    """Delete a stored secret. Returns True if a row was deleted."""
    _ensure_db()
    with Session(_db.get_engine()) as session:
        row = session.exec(
            select(Credential).where(
                Credential.project_id == project_id,
                Credential.user_id == user_id,
                Credential.name == name,
            )
        ).first()
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True

