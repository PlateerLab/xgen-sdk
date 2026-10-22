"""Fail-closed credential helpers.

Database connection fields must use the row-aware helpers. The generic wrapper is
kept for other encrypted application records, but it also uses the configured v1
keyring and never treats failed decryption as plaintext.
"""
from __future__ import annotations

from typing import Mapping, Optional
from uuid import uuid4

from .envelope import (
    FORMAT_ENVELOPE_V1,
    CredentialEnvelopeError,
    CredentialKeyring,
    decrypt_envelope,
    encrypt_envelope,
    validate_keyring_environment,
)


_GENERIC_BINDING = "non-db-connection-secret"
_GENERIC_FIELD = "custom_password"


def new_credential_binding_id() -> str:
    """Generate the immutable identifier bound into both row credentials."""
    return str(uuid4())


def encrypt_credential(
    plaintext: str,
    *,
    binding_id: str = _GENERIC_BINDING,
    field_name: str = _GENERIC_FIELD,
    keyring: CredentialKeyring | None = None,
) -> str:
    return encrypt_envelope(
        plaintext, binding_id=binding_id, field_name=field_name, keyring=keyring
    )


def decrypt_credential(
    encrypted: str,
    *,
    binding_id: str = _GENERIC_BINDING,
    field_name: str = _GENERIC_FIELD,
    keyring: CredentialKeyring | None = None,
) -> str:
    return decrypt_envelope(
        encrypted, binding_id=binding_id, field_name=field_name, keyring=keyring
    )


def encrypt_if_present(
    value: Optional[str],
    *,
    binding_id: str = _GENERIC_BINDING,
    field_name: str = _GENERIC_FIELD,
    keyring: CredentialKeyring | None = None,
) -> Optional[str]:
    if value is None or value == "":
        return value
    return encrypt_credential(
        value, binding_id=binding_id, field_name=field_name, keyring=keyring
    )


def decrypt_row_credential(
    row: Mapping[str, object],
    field_name: str,
    *,
    keyring: CredentialKeyring | None = None,
) -> Optional[str]:
    """Decrypt one DB credential only when its explicit storage format is v1."""
    value = row.get(field_name)
    if value is None or value == "":
        return value  # type: ignore[return-value]
    if row.get("credential_format") != FORMAT_ENVELOPE_V1:
        raise CredentialEnvelopeError(
            "CREDENTIAL_FORMAT_UNMIGRATED",
            "Database credential migration is required.",
        )
    binding_id = str(row.get("credential_binding_id") or "")
    if not isinstance(value, str):
        raise CredentialEnvelopeError(
            "CREDENTIAL_ENVELOPE_INVALID", "Credential envelope is invalid."
        )
    return decrypt_envelope(
        value, binding_id=binding_id, field_name=field_name, keyring=keyring
    )


__all__ = [
    "CredentialEnvelopeError",
    "CredentialKeyring",
    "FORMAT_ENVELOPE_V1",
    "decrypt_credential",
    "decrypt_row_credential",
    "encrypt_credential",
    "encrypt_if_present",
    "new_credential_binding_id",
    "validate_keyring_environment",
]
