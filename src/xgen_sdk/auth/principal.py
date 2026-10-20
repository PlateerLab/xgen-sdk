"""Signed Gateway and service principal assertions.

Identity headers are attacker-controlled until their values are bound to a valid
``X-Xgen-Principal`` assertion.  This module owns that trust boundary for both
FastAPI dependencies and raw ASGI applications.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import unquote

from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15


GATEWAY_PUBLIC_KEY_ENV = "XGEN_GATEWAY_PUBLIC_KEY_PEM"
SERVICE_NAME_ENV = "XGEN_SERVICE_NAME"
SERVICE_SIGNING_KEY_ENV = "XGEN_SERVICE_SIGNING_KEY"
TRUSTED_SERVICES_ENV = "XGEN_TRUSTED_SERVICES"

PROOF_HEADER = "x-xgen-principal"
IDENTITY_HEADERS = (
    "x-user-id",
    "x-user-name",
    "x-user-superuser",
    "x-user-roles",
    "x-user-permissions",
    "x-user-supervision-full",
)
_IDENTITY_HEADER_SET = frozenset(IDENTITY_HEADERS)
_WIRE_HEADER_NAMES = {
    "x-user-id": "X-User-ID",
    "x-user-name": "X-User-Name",
    "x-user-superuser": "X-User-Superuser",
    "x-user-roles": "X-User-Roles",
    "x-user-permissions": "X-User-Permissions",
    "x-user-supervision-full": "X-User-Supervision-Full",
    PROOF_HEADER: "X-Xgen-Principal",
}
_GATEWAY_JWT_HEADER = {"alg": "RS256", "typ": "JWT"}
_SERVICE_JWT_HEADER = {"alg": "HS256", "typ": "JWT"}
_CLAIM_KEYS = frozenset({"iss", "aud", "type", "iat", "exp", "headers"})
_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_B64_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_ASSERTION_BYTES = 16_384


class PrincipalValidationError(ValueError):
    """The presented identity headers are not backed by a trusted assertion."""


class PrincipalConfigurationError(PrincipalValidationError):
    """A receiving or issuing service lacks required trust configuration."""


def _invalid(message: str = "invalid principal assertion") -> PrincipalValidationError:
    return PrincipalValidationError(message)


def _misconfigured(message: str) -> PrincipalConfigurationError:
    return PrincipalConfigurationError(message)


def _reject_constant(value: str) -> None:
    raise _invalid(f"non-finite JSON number: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid("duplicate JSON member")
        result[key] = value
    return result


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
    except (UnicodeDecodeError, json.JSONDecodeError, PrincipalValidationError) as exc:
        raise _invalid() from exc
    if not isinstance(value, dict):
        raise _invalid()
    return value


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _json_segment(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64encode(raw)


def _key_bytes(value: str | None, *, env_name: str) -> bytes:
    if not isinstance(value, str) or len(value.encode("utf-8")) < 32:
        raise _misconfigured(f"{env_name} must contain at least 32 bytes")
    return value.encode("utf-8")


def _gateway_public_key(value: str | None) -> RSA.RsaKey:
    if not isinstance(value, str) or not value:
        raise _misconfigured(f"{GATEWAY_PUBLIC_KEY_ENV} is not configured")
    try:
        key = RSA.import_key(value)
    except (ValueError, IndexError, TypeError) as exc:
        raise _misconfigured(f"{GATEWAY_PUBLIC_KEY_ENV} is not a valid RSA public key") from exc
    if key.has_private():
        raise _misconfigured(f"{GATEWAY_PUBLIC_KEY_ENV} must contain a public key only")
    if key.size_in_bits() < 2048:
        raise _misconfigured(f"{GATEWAY_PUBLIC_KEY_ENV} must be at least 2048 bits")
    return key


def _service_name(value: str | None, *, source: str) -> str:
    if not isinstance(value, str) or not _SERVICE_NAME_RE.fullmatch(value) or value == "xgen-gateway":
        raise _misconfigured(f"{source} must be a valid non-gateway service name")
    return value


def _safe_header_value(value: Any) -> str:
    if not isinstance(value, str):
        raise _invalid("principal header values must be strings")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _invalid("principal header contains a control character")
    return value


def _canonical_identity_headers(headers: Mapping[str, str], *, defaults: bool) -> dict[str, str]:
    canonical: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        if not isinstance(raw_name, str):
            raise _invalid("principal header names must be strings")
        name = raw_name.lower()
        if name.startswith("x-user_") or name == "x-userid":
            raise _invalid("ambiguous identity header alias")
        if name not in _IDENTITY_HEADER_SET:
            raise _invalid(f"unsupported principal header: {raw_name}")
        if name in canonical:
            raise _invalid("duplicate identity header")
        canonical[name] = _safe_header_value(raw_value)

    if defaults:
        canonical.setdefault("x-user-superuser", "false")
        canonical.setdefault("x-user-roles", "")
        canonical.setdefault("x-user-permissions", "")
        canonical.setdefault("x-user-supervision-full", "")
    if set(canonical) != _IDENTITY_HEADER_SET:
        raise _invalid("all canonical identity headers are required")
    if not canonical["x-user-id"] or not canonical["x-user-name"]:
        raise _invalid("user id and name are required")
    if canonical["x-user-superuser"].lower() not in {"true", "false"}:
        raise _invalid("invalid superuser value")
    return {name: canonical[name] for name in IDENTITY_HEADERS}


def _split_permissions(value: str) -> frozenset[str]:
    return frozenset(unquote(item.strip()) for item in value.split(",") if item.strip())


def _trusted_services(raw: str | None) -> dict[str, tuple[bytes, frozenset[str]]]:
    if not raw:
        raise _misconfigured(f"{TRUSTED_SERVICES_ENV} is not configured")
    try:
        parsed = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, PrincipalValidationError) as exc:
        raise _misconfigured(f"invalid {TRUSTED_SERVICES_ENV}") from exc
    if not isinstance(parsed, dict):
        raise _misconfigured(f"invalid {TRUSTED_SERVICES_ENV}")

    trusted: dict[str, tuple[bytes, frozenset[str]]] = {}
    for service, config in parsed.items():
        if not isinstance(service, str) or not _SERVICE_NAME_RE.fullmatch(service):
            raise _misconfigured("invalid trusted service name")
        if not isinstance(config, dict) or set(config) != {"key", "permissions"}:
            raise _misconfigured("trusted service entries require only key and permissions")
        permissions = config["permissions"]
        if (
            not isinstance(permissions, list)
            or any(
                not isinstance(permission, str)
                or not permission
                or "*" in unquote(permission)
                for permission in permissions
            )
            or len(set(permissions)) != len(permissions)
        ):
            raise _misconfigured("trusted service permissions must be unique finite strings")
        trusted[service] = (
            _key_bytes(config["key"], env_name=f"{TRUSTED_SERVICES_ENV}.{service}.key"),
            frozenset(permissions),
        )
    return trusted


def _raw_principal_headers(scope: Mapping[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    identity: dict[str, str] = {}
    proof: str | None = None
    relevant = False
    for pair in scope.get("headers", []):
        if not isinstance(pair, Sequence) or len(pair) != 2:
            raise _invalid("invalid ASGI header entry")
        raw_name, raw_value = pair
        if not isinstance(raw_name, bytes) or not isinstance(raw_value, bytes):
            raise _invalid("invalid ASGI header entry")
        try:
            name = raw_name.decode("ascii").lower()
        except UnicodeDecodeError as exc:
            raise _invalid("invalid HTTP header name") from exc
        if name.startswith("x-user_") or name == "x-userid":
            raise _invalid("ambiguous identity header alias")
        if name in _IDENTITY_HEADER_SET:
            relevant = True
            if name in identity:
                raise _invalid("duplicate identity header")
            identity[name] = _safe_header_value(raw_value.decode("latin-1"))
        elif name == PROOF_HEADER:
            relevant = True
            if proof is not None:
                raise _invalid("duplicate principal proof header")
            try:
                proof = raw_value.decode("ascii")
            except UnicodeDecodeError as exc:
                raise _invalid("principal proof must be ASCII") from exc
    if not relevant:
        return None, None
    return identity, proof


def _verify_assertion(
    proof: str,
    actual_headers: Mapping[str, str],
    *,
    now: int | None = None,
) -> dict[str, Any]:
    if not isinstance(proof, str) or len(proof.encode("ascii", errors="ignore")) > _MAX_ASSERTION_BYTES:
        raise _invalid()
    parts = proof.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise _invalid()
    protected, payload_segment, signature_segment = parts
    header = _decode_json_segment(protected)
    payload = _decode_json_segment(payload_segment)
    if set(payload) != _CLAIM_KEYS:
        raise _invalid("unsupported JWT schema")

    principal_type = payload.get("type")
    issuer = payload.get("iss")
    service_permissions: frozenset[str] | None = None
    if principal_type == "principal" and issuer == "xgen-gateway":
        if header != _GATEWAY_JWT_HEADER:
            raise _invalid("gateway principals require RS256")
        gateway_key = _gateway_public_key(os.getenv(GATEWAY_PUBLIC_KEY_ENV))
        service_key = None
    elif principal_type == "service" and isinstance(issuer, str):
        if header != _SERVICE_JWT_HEADER:
            raise _invalid("service principals require HS256")
        service = _trusted_services(os.getenv(TRUSTED_SERVICES_ENV)).get(issuer)
        if service is None:
            raise _invalid("untrusted service issuer")
        service_key, service_permissions = service
        gateway_key = None
    else:
        raise _invalid("invalid principal issuer or type")

    if not _B64_RE.fullmatch(signature_segment):
        raise _invalid()
    try:
        signature = base64.urlsafe_b64decode(signature_segment + "=" * (-len(signature_segment) % 4))
    except (ValueError, TypeError) as exc:
        raise _invalid() from exc
    if _b64encode(signature) != signature_segment:
        raise _invalid("non-canonical JWT signature")
    signing_input = f"{protected}.{payload_segment}".encode("ascii")
    if gateway_key is not None:
        try:
            pkcs1_15.new(gateway_key).verify(SHA256.new(signing_input), signature)
        except (ValueError, TypeError) as exc:
            raise _invalid("invalid principal signature") from exc
    else:
        assert service_key is not None
        expected = hmac.new(service_key, signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise _invalid("invalid principal signature")

    expected_audience = _service_name(os.getenv(SERVICE_NAME_ENV), source=SERVICE_NAME_ENV)
    if payload["aud"] != expected_audience:
        raise _invalid("invalid principal audience")
    iat, exp = payload["iat"], payload["exp"]
    if isinstance(iat, bool) or isinstance(exp, bool) or not isinstance(iat, int) or not isinstance(exp, int):
        raise _invalid("invalid principal timestamps")
    current = int(time.time()) if now is None else now
    if iat > current or exp <= current or exp <= iat or exp > iat + 60:
        raise _invalid("expired or invalid principal lifetime")

    claimed_headers = payload["headers"]
    if not isinstance(claimed_headers, dict):
        raise _invalid("invalid bound headers")
    canonical_claims = _canonical_identity_headers(claimed_headers, defaults=False)
    canonical_actual = _canonical_identity_headers(actual_headers, defaults=False)
    if canonical_claims != canonical_actual:
        raise _invalid("principal header binding mismatch")

    if principal_type == "service":
        if canonical_claims["x-user-superuser"].lower() != "false":
            raise _invalid("service assertions cannot grant superuser")
        declared = _split_permissions(canonical_claims["x-user-permissions"])
        if any("*" in permission for permission in declared) or not declared.issubset(
            service_permissions or frozenset()
        ):
            raise _invalid("service assertion permissions exceed configured grants")
    return payload


def verified_identity_headers(scope: Mapping[str, Any], *, now: int | None = None) -> dict[str, str] | None:
    """Return identity headers only after proof verification.

    ``None`` means that the request is anonymous and carried no identity or proof
    headers.  Any partial, duplicated, aliased, or invalid authentication input
    raises :class:`PrincipalValidationError`.
    """

    identity, proof = _raw_principal_headers(scope)
    if identity is None:
        return None
    if proof is None:
        raise _invalid("identity headers require a principal proof")
    _verify_assertion(proof, identity, now=now)
    return _canonical_identity_headers(identity, defaults=False)


def get_verified_principal_headers(request: Any) -> dict[str, str]:
    """Return an existing proof for forwarding only within its current audience."""

    identity = verified_identity_headers(request.scope)
    if identity is None:
        raise _invalid("request has no verified principal")
    _, proof = _raw_principal_headers(request.scope)
    assert proof is not None
    result = {_WIRE_HEADER_NAMES[name]: value for name, value in identity.items()}
    result[_WIRE_HEADER_NAMES[PROOF_HEADER]] = proof
    return result


def sign_principal_for_service(
    delegated_headers: Mapping[str, str],
    *,
    audience: str,
    service_name: str | None = None,
    signing_key: str | None = None,
    ttl_seconds: int = 60,
    now: int | None = None,
) -> str:
    """Sign a server-created, finite-permission delegated user profile.

    Callers must source ``delegated_headers`` from trusted server state, such as a
    verified principal or a database lookup.  This helper intentionally has no
    unsigned fallback and never reads identity from an HTTP body.
    """

    issuer = _service_name(
        service_name if service_name is not None else os.getenv(SERVICE_NAME_ENV),
        source=SERVICE_NAME_ENV,
    )
    target = _service_name(audience, source="audience")
    key = _key_bytes(
        signing_key if signing_key is not None else os.getenv(SERVICE_SIGNING_KEY_ENV),
        env_name=SERVICE_SIGNING_KEY_ENV,
    )
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 60:
        raise _invalid("service assertion ttl_seconds must be between 1 and 60")

    headers = _canonical_identity_headers(delegated_headers, defaults=True)
    if headers["x-user-superuser"].lower() != "false":
        raise _invalid("service assertions cannot grant superuser")
    permissions = _split_permissions(headers["x-user-permissions"])
    if any("*" in permission for permission in permissions):
        raise _invalid("service assertions require finite permissions")

    issued_at = int(time.time()) if now is None else now
    if isinstance(issued_at, bool) or not isinstance(issued_at, int):
        raise _invalid("now must be an integer Unix timestamp")
    payload = {
        "iss": issuer,
        "aud": target,
        "type": "service",
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
        "headers": headers,
    }
    protected = _json_segment(_SERVICE_JWT_HEADER)
    body = _json_segment(payload)
    signature = hmac.new(key, f"{protected}.{body}".encode("ascii"), hashlib.sha256).digest()
    return f"{protected}.{body}.{_b64encode(signature)}"


def service_principal_headers(
    delegated_headers: Mapping[str, str],
    *,
    audience: str,
    service_name: str | None = None,
    signing_key: str | None = None,
    ttl_seconds: int = 60,
    now: int | None = None,
) -> dict[str, str]:
    """Return canonical identity headers plus a new scoped service proof."""

    identity = _canonical_identity_headers(delegated_headers, defaults=True)
    proof = sign_principal_for_service(
        identity,
        audience=audience,
        service_name=service_name,
        signing_key=signing_key,
        ttl_seconds=ttl_seconds,
        now=now,
    )
    result = {_WIRE_HEADER_NAMES[name]: value for name, value in identity.items()}
    result[_WIRE_HEADER_NAMES[PROOF_HEADER]] = proof
    return result


class PrincipalMiddleware:
    """Reject unverified identity headers on HTTP and WebSocket requests."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        try:
            identity = verified_identity_headers(scope)
        except PrincipalConfigurationError:
            if scope.get("type") == "websocket":
                await send({"type": "websocket.close", "code": 1013, "reason": "XGen principal verifier unavailable"})
            else:
                body = b'{"detail":"XGen principal verifier is not configured"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 503,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
            return
        except PrincipalValidationError:
            if scope.get("type") == "websocket":
                await send({"type": "websocket.close", "code": 4401, "reason": "Invalid XGen principal"})
            else:
                body = b'{"detail":"Invalid XGen principal"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
            return

        if identity is not None:
            state = scope.setdefault("state", {})
            state["xgen_principal_verified"] = True
            state["xgen_principal_headers"] = dict(identity)
        await self.app(scope, receive, send)


__all__ = [
    "GATEWAY_PUBLIC_KEY_ENV",
    "IDENTITY_HEADERS",
    "PROOF_HEADER",
    "PrincipalMiddleware",
    "PrincipalConfigurationError",
    "PrincipalValidationError",
    "SERVICE_NAME_ENV",
    "SERVICE_SIGNING_KEY_ENV",
    "TRUSTED_SERVICES_ENV",
    "get_verified_principal_headers",
    "service_principal_headers",
    "sign_principal_for_service",
    "verified_identity_headers",
]
