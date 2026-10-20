"""Short-lived, service-owned capabilities for document resource access.

Document capabilities are deliberately separate from user/service principals.
They bind a workflow decision and immutable resource digests to a finite set of
document operations, and are issued and verified only as ``xgen-documents``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections.abc import Mapping
from typing import Any


DOCUMENT_CAPABILITY_KEYS_ENV = "XGEN_DOCUMENT_CAPABILITY_KEYS"
SERVICE_SIGNING_KEY_ENV = "XGEN_SERVICE_SIGNING_KEY"
SERVICE_KEY_ID_ENV = "XGEN_SERVICE_KEY_ID"

DOCUMENT_CAPABILITY_ISSUER = "xgen-documents"
DOCUMENT_CAPABILITY_AUDIENCE = "xgen-documents"
DOCUMENT_CAPABILITY_TYPE = "document-capability"
DOCUMENT_CAPABILITY_OPERATIONS = frozenset({"fetch", "metadata", "access", "download"})

_HEADER_KEYS = frozenset({"alg", "typ", "kid"})
_INPUT_CLAIM_KEYS = frozenset(
    {
        "deployment_id",
        "workflow_id",
        "policy_digest",
        "resource_id",
        "resource_digest",
        "operations",
    }
)
_PAYLOAD_KEYS = _INPUT_CLAIM_KEYS | frozenset({"iss", "aud", "type", "iat", "exp", "jti"})
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_B64_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_CAPABILITY_BYTES = 16_384


class DocumentCapabilityValidationError(ValueError):
    """The capability is malformed, expired, untrusted, or over-broad."""


class DocumentCapabilityConfigurationError(DocumentCapabilityValidationError):
    """The local document-capability signer or verifier is misconfigured."""


def _invalid(message: str = "invalid document capability") -> DocumentCapabilityValidationError:
    return DocumentCapabilityValidationError(message)


def _misconfigured(message: str) -> DocumentCapabilityConfigurationError:
    return DocumentCapabilityConfigurationError(message)


def _reject_constant(value: str) -> None:
    raise _invalid(f"non-finite JSON number: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid("duplicate JSON member")
        result[key] = value
    return result


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _json_segment(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    return _b64encode(raw)


def _decode_json_segment(segment: str) -> dict[str, Any]:
    if not segment or not _B64_RE.fullmatch(segment):
        raise _invalid()
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (ValueError, TypeError) as exc:
        raise _invalid() from exc
    if _b64encode(raw) != segment:
        raise _invalid("non-canonical JWT encoding")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, DocumentCapabilityValidationError) as exc:
        raise _invalid() from exc
    if not isinstance(value, dict):
        raise _invalid()
    return value


def _key_id(value: Any, *, source: str, configuration: bool) -> str:
    if not isinstance(value, str) or not _KEY_ID_RE.fullmatch(value):
        if configuration:
            raise _misconfigured(f"{source} must be a valid key id")
        raise _invalid("invalid key id")
    return value


def _key_bytes(value: Any, *, source: str) -> bytes:
    if not isinstance(value, str) or len(value.encode("utf-8")) < 32:
        raise _misconfigured(f"{source} must contain at least 32 bytes")
    return value.encode("utf-8")


def _not_after(value: Any, *, source: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _misconfigured(f"{source} must be a positive integer Unix timestamp")
    return value


def _parse_keyring(
    value: Mapping[str, Any], *, source: str
) -> dict[str, tuple[bytes, int | None]]:
    if not isinstance(value, Mapping) or not value:
        raise _misconfigured(f"{source} must be a non-empty object")
    result: dict[str, tuple[bytes, int | None]] = {}
    active_count = 0
    for raw_kid, config in value.items():
        kid = _key_id(raw_kid, source=source, configuration=True)
        if not isinstance(config, Mapping) or set(config) not in (
            {"key"},
            {"key", "not_after"},
        ):
            raise _misconfigured(f"{source} key entries require key and optional not_after")
        if "not_after" in config:
            cutoff = _not_after(config["not_after"], source=f"{source}.{kid}.not_after")
        else:
            cutoff = None
            active_count += 1
        result[kid] = (_key_bytes(config["key"], source=f"{source}.{kid}.key"), cutoff)
    if active_count != 1:
        raise _misconfigured(f"{source} requires exactly one active key")
    return result


def _environment_keyring() -> dict[str, tuple[bytes, int | None]]:
    raw = os.getenv(DOCUMENT_CAPABILITY_KEYS_ENV)
    if raw is None:
        kid = _key_id(os.getenv(SERVICE_KEY_ID_ENV), source=SERVICE_KEY_ID_ENV, configuration=True)
        key = _key_bytes(os.getenv(SERVICE_SIGNING_KEY_ENV), source=SERVICE_SIGNING_KEY_ENV)
        return {kid: (key, None)}
    if not raw:
        raise _misconfigured(f"{DOCUMENT_CAPABILITY_KEYS_ENV} must be a non-empty JSON object")
    try:
        parsed = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, DocumentCapabilityValidationError) as exc:
        raise _misconfigured(f"invalid {DOCUMENT_CAPABILITY_KEYS_ENV}") from exc
    if not isinstance(parsed, dict):
        raise _misconfigured(f"invalid {DOCUMENT_CAPABILITY_KEYS_ENV}")
    return _parse_keyring(parsed, source=DOCUMENT_CAPABILITY_KEYS_ENV)


def _nonempty_string(value: Any, *, claim: str) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise _invalid(f"{claim} must be a non-empty string without control characters")
    return value


def _canonical_business_claims(claims: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(claims, Mapping) or set(claims) != _INPUT_CLAIM_KEYS:
        raise _invalid("unsupported document capability claim schema")

    deployment_id = claims["deployment_id"]
    if isinstance(deployment_id, bool) or not isinstance(deployment_id, (str, int)):
        raise _invalid("deployment_id must be a string or integer")
    deployment_id = str(deployment_id)
    if not deployment_id or any(ord(char) < 32 for char in deployment_id):
        raise _invalid("deployment_id must be non-empty without control characters")

    policy_digest = claims["policy_digest"]
    resource_digest = claims["resource_digest"]
    if not isinstance(policy_digest, str) or not _DIGEST_RE.fullmatch(policy_digest):
        raise _invalid("policy_digest must be a lowercase SHA-256 hex digest")
    if not isinstance(resource_digest, str) or not _DIGEST_RE.fullmatch(resource_digest):
        raise _invalid("resource_digest must be a lowercase SHA-256 hex digest")

    operations = claims["operations"]
    if (
        not isinstance(operations, list)
        or not operations
        or any(not isinstance(operation, str) for operation in operations)
        or len(set(operations)) != len(operations)
        or not set(operations).issubset(DOCUMENT_CAPABILITY_OPERATIONS)
    ):
        raise _invalid("operations must be a unique non-empty list of supported finite operations")

    return {
        "deployment_id": deployment_id,
        "workflow_id": _nonempty_string(claims["workflow_id"], claim="workflow_id"),
        "policy_digest": policy_digest,
        "resource_id": _nonempty_string(claims["resource_id"], claim="resource_id"),
        "resource_digest": resource_digest,
        "operations": list(operations),
    }


def sign_document_capability(
    claims: Mapping[str, Any],
    *,
    signing_key: str | None = None,
    key_id: str | None = None,
    ttl_seconds: int = 60,
    now: int | None = None,
) -> str:
    """Sign a short-lived document capability using this service's current key."""

    canonical = _canonical_business_claims(claims)
    key = _key_bytes(
        signing_key if signing_key is not None else os.getenv(SERVICE_SIGNING_KEY_ENV),
        source=SERVICE_SIGNING_KEY_ENV,
    )
    kid = _key_id(
        key_id if key_id is not None else os.getenv(SERVICE_KEY_ID_ENV),
        source=SERVICE_KEY_ID_ENV,
        configuration=True,
    )
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 60:
        raise _invalid("ttl_seconds must be between 1 and 60")
    issued_at = int(time.time()) if now is None else now
    if isinstance(issued_at, bool) or not isinstance(issued_at, int):
        raise _invalid("now must be an integer Unix timestamp")

    payload = {
        "iss": DOCUMENT_CAPABILITY_ISSUER,
        "aud": DOCUMENT_CAPABILITY_AUDIENCE,
        "type": DOCUMENT_CAPABILITY_TYPE,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
        "jti": secrets.token_urlsafe(24),
        **canonical,
    }
    protected = _json_segment({"alg": "HS256", "typ": "JWT", "kid": kid})
    body = _json_segment(payload)
    signature = hmac.new(key, f"{protected}.{body}".encode("ascii"), hashlib.sha256).digest()
    return f"{protected}.{body}.{_b64encode(signature)}"


def verify_document_capability(
    token: str,
    *,
    trusted_keys: Mapping[str, Any] | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify and return the exact document-capability payload.

    ``trusted_keys`` uses the same ``{kid: {key, not_after?}}`` shape as
    ``XGEN_DOCUMENT_CAPABILITY_KEYS`` and is intended for explicit tests and
    callers that load configuration outside the process environment.
    """

    if not isinstance(token, str) or len(token.encode("ascii", errors="ignore")) > _MAX_CAPABILITY_BYTES:
        raise _invalid()
    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise _invalid()
    protected_segment, payload_segment, signature_segment = parts
    header = _decode_json_segment(protected_segment)
    if set(header) != _HEADER_KEYS or header.get("alg") != "HS256" or header.get("typ") != "JWT":
        raise _invalid("document capabilities require HS256 and a key id")
    kid = _key_id(header.get("kid"), source="JWT kid", configuration=False)
    keyring = (
        _parse_keyring(trusted_keys, source="trusted_keys")
        if trusted_keys is not None
        else _environment_keyring()
    )
    selected = keyring.get(kid)
    if selected is None:
        raise _invalid("unknown document capability key id")
    key, key_not_after = selected

    if not signature_segment or not _B64_RE.fullmatch(signature_segment):
        raise _invalid()
    try:
        signature = base64.urlsafe_b64decode(
            signature_segment + "=" * (-len(signature_segment) % 4)
        )
    except (ValueError, TypeError) as exc:
        raise _invalid() from exc
    if _b64encode(signature) != signature_segment:
        raise _invalid("non-canonical JWT signature")
    signing_input = f"{protected_segment}.{payload_segment}".encode("ascii")
    expected = hmac.new(key, signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise _invalid("invalid document capability signature")

    payload = _decode_json_segment(payload_segment)
    if set(payload) != _PAYLOAD_KEYS:
        raise _invalid("unsupported document capability payload schema")
    if (
        payload.get("iss") != DOCUMENT_CAPABILITY_ISSUER
        or payload.get("aud") != DOCUMENT_CAPABILITY_AUDIENCE
        or payload.get("type") != DOCUMENT_CAPABILITY_TYPE
    ):
        raise _invalid("invalid document capability issuer, audience, or type")
    iat, exp = payload.get("iat"), payload.get("exp")
    if isinstance(iat, bool) or isinstance(exp, bool) or not isinstance(iat, int) or not isinstance(exp, int):
        raise _invalid("invalid document capability timestamps")
    current = int(time.time()) if now is None else now
    if isinstance(current, bool) or not isinstance(current, int):
        raise _invalid("now must be an integer Unix timestamp")
    if iat > current or exp <= current or exp <= iat or exp > iat + 60:
        raise _invalid("expired or invalid document capability lifetime")
    if key_not_after is not None and exp > key_not_after:
        raise _invalid("document capability expires after the signing key retirement cutoff")
    _nonempty_string(payload.get("jti"), claim="jti")

    canonical = _canonical_business_claims(
        {claim: payload[claim] for claim in _INPUT_CLAIM_KEYS}
    )
    if any(payload[claim] != canonical[claim] for claim in _INPUT_CLAIM_KEYS):
        raise _invalid("non-canonical document capability claims")
    return dict(payload)


__all__ = [
    "DOCUMENT_CAPABILITY_AUDIENCE",
    "DOCUMENT_CAPABILITY_ISSUER",
    "DOCUMENT_CAPABILITY_KEYS_ENV",
    "DOCUMENT_CAPABILITY_OPERATIONS",
    "DOCUMENT_CAPABILITY_TYPE",
    "DocumentCapabilityConfigurationError",
    "DocumentCapabilityValidationError",
    "sign_document_capability",
    "verify_document_capability",
]
