"""Versioned, row-bound envelopes for stored database credentials."""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from typing import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ENVELOPE_VERSION = 1
ENVELOPE_PREFIX = "xgen-db-credential:"
KEYRING_ENV = "DB_CREDENTIAL_KEYRING"
ACTIVE_KID_ENV = "DB_CREDENTIAL_ACTIVE_KID"
FORMAT_ENVELOPE_V1 = "envelope-v1"
FORMAT_LEGACY_AES_GCM_V0 = "aes-gcm-v0"
FORMAT_LEGACY_PLAINTEXT = "plaintext"
FORMAT_INVALID = "invalid"

_KID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FIELD_NAMES = frozenset({"db_password", "custom_password"})


class CredentialConfigurationError(RuntimeError):
    pass


class CredentialEnvelopeError(RuntimeError):
    """Stable fail-closed error; never includes ciphertext or plaintext."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class CredentialKeyring:
    active_kid: str
    keys: Mapping[str, bytes]

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "CredentialKeyring":
        env = os.environ if environ is None else environ
        raw = (env.get(KEYRING_ENV) or "").strip()
        active_kid = (env.get(ACTIVE_KID_ENV) or "").strip()
        if not raw or not active_kid:
            raise CredentialConfigurationError(
                f"{KEYRING_ENV} and {ACTIVE_KID_ENV} are required"
            )
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise CredentialConfigurationError(f"{KEYRING_ENV} must be a JSON object") from exc
        if not isinstance(parsed, dict) or not parsed:
            raise CredentialConfigurationError(f"{KEYRING_ENV} must contain at least one key")

        keys: dict[str, bytes] = {}
        for kid, encoded in parsed.items():
            if not isinstance(kid, str) or not _KID_RE.fullmatch(kid):
                raise CredentialConfigurationError("credential key IDs must use safe ASCII characters")
            if not isinstance(encoded, str):
                raise CredentialConfigurationError("credential keys must be base64url strings")
            try:
                key = _b64decode(encoded)
            except ValueError as exc:
                raise CredentialConfigurationError("credential keys must be valid base64url") from exc
            if len(key) != 32:
                raise CredentialConfigurationError("credential keys must decode to exactly 32 bytes")
            keys[kid] = key
        if active_kid not in keys:
            raise CredentialConfigurationError(f"{ACTIVE_KID_ENV} is not present in {KEYRING_ENV}")
        return cls(active_kid=active_kid, keys=keys)


def validate_keyring_environment(environ: Mapping[str, str] | None = None) -> CredentialKeyring:
    return CredentialKeyring.from_environment(environ)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid base64url")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("invalid base64url") from exc


def _aad(binding_id: str, field_name: str) -> bytes:
    binding = str(binding_id or "").strip()
    if not binding or len(binding) > 128:
        raise CredentialEnvelopeError("CREDENTIAL_BINDING_INVALID", "Credential row binding is invalid.")
    if field_name not in _FIELD_NAMES:
        raise CredentialEnvelopeError("CREDENTIAL_FIELD_INVALID", "Credential field binding is invalid.")
    return f"xgen|db_connections|v1|{binding}|{field_name}".encode("utf-8")


def encrypt_envelope(
    plaintext: str,
    *,
    binding_id: str,
    field_name: str,
    keyring: CredentialKeyring | None = None,
) -> str:
    if not isinstance(plaintext, str) or not plaintext:
        raise CredentialEnvelopeError("CREDENTIAL_EMPTY", "Credential value is empty.")
    ring = keyring or CredentialKeyring.from_environment()
    nonce = os.urandom(12)
    ciphertext = AESGCM(ring.keys[ring.active_kid]).encrypt(
        nonce, plaintext.encode("utf-8"), _aad(binding_id, field_name)
    )
    payload = {
        "version": ENVELOPE_VERSION,
        "kid": ring.active_kid,
        "nonce": _b64encode(nonce),
        "ciphertext": _b64encode(ciphertext),
    }
    encoded = _b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return ENVELOPE_PREFIX + encoded


def envelope_kid(envelope: str) -> str:
    return _parse(envelope)["kid"]


def _parse(envelope: str) -> dict[str, object]:
    if not isinstance(envelope, str) or not envelope.startswith(ENVELOPE_PREFIX):
        raise CredentialEnvelopeError("CREDENTIAL_ENVELOPE_INVALID", "Credential envelope is invalid.")
    try:
        payload = json.loads(_b64decode(envelope[len(ENVELOPE_PREFIX):]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialEnvelopeError("CREDENTIAL_ENVELOPE_INVALID", "Credential envelope is invalid.") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "kid", "nonce", "ciphertext"}:
        raise CredentialEnvelopeError("CREDENTIAL_ENVELOPE_INVALID", "Credential envelope is invalid.")
    if payload.get("version") != ENVELOPE_VERSION:
        raise CredentialEnvelopeError("CREDENTIAL_VERSION_UNSUPPORTED", "Credential envelope version is unsupported.")
    kid = payload.get("kid")
    if not isinstance(kid, str) or not _KID_RE.fullmatch(kid):
        raise CredentialEnvelopeError("CREDENTIAL_ENVELOPE_INVALID", "Credential envelope is invalid.")
    try:
        nonce = _b64decode(payload.get("nonce"))  # type: ignore[arg-type]
        ciphertext = _b64decode(payload.get("ciphertext"))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CredentialEnvelopeError("CREDENTIAL_ENVELOPE_INVALID", "Credential envelope is invalid.") from exc
    if len(nonce) != 12 or len(ciphertext) < 16:
        raise CredentialEnvelopeError("CREDENTIAL_ENVELOPE_INVALID", "Credential envelope is invalid.")
    return {"kid": kid, "nonce": nonce, "ciphertext": ciphertext}


def decrypt_envelope(
    envelope: str,
    *,
    binding_id: str,
    field_name: str,
    keyring: CredentialKeyring | None = None,
) -> str:
    payload = _parse(envelope)
    ring = keyring or CredentialKeyring.from_environment()
    kid = payload["kid"]
    key = ring.keys.get(kid)  # type: ignore[arg-type]
    if key is None:
        raise CredentialEnvelopeError("CREDENTIAL_KEY_UNKNOWN", "Credential key is unavailable.")
    try:
        plaintext = AESGCM(key).decrypt(
            payload["nonce"],  # type: ignore[arg-type]
            payload["ciphertext"],  # type: ignore[arg-type]
            _aad(binding_id, field_name),
        )
        return plaintext.decode("utf-8")
    except (InvalidTag, UnicodeDecodeError) as exc:
        raise CredentialEnvelopeError("CREDENTIAL_AUTH_FAILED", "Credential authentication failed.") from exc
