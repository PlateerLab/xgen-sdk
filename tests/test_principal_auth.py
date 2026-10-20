from __future__ import annotations

import base64
import json
import time
from collections.abc import Mapping

import pytest
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest
from starlette.websockets import WebSocketDisconnect

from xgen_sdk.auth import (
    PrincipalMiddleware,
    PrincipalValidationError,
    get_user_info_by_gateway,
    get_verified_principal_headers,
    service_principal_headers,
    sign_principal_for_service,
)


PRIVATE_KEY = RSA.generate(2048)
PUBLIC_KEY_PEM = PRIVATE_KEY.public_key().export_key().decode("ascii")
PREVIOUS_PRIVATE_KEY = RSA.generate(2048)
PREVIOUS_PUBLIC_KEY_PEM = PREVIOUS_PRIVATE_KEY.public_key().export_key().decode("ascii")
SERVICE_KEY = "service-test-key-that-is-at-least-32-bytes"
PREVIOUS_SERVICE_KEY = "previous-service-test-key-at-least-32-bytes"
CANONICAL = {
    "x-user-id": "42",
    "x-user-name": "Kim%20Hae-soo",
    "x-user-superuser": "false",
    "x-user-roles": "developer,reviewer",
    "x-user-permissions": "document:read,workflow:execute",
    "x-user-supervision-full": "team-a",
}


@pytest.fixture(autouse=True)
def _principal_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("XGEN_GATEWAY_PUBLIC_KEYS", raising=False)
    monkeypatch.delenv("XGEN_GATEWAY_KEY_ID", raising=False)
    monkeypatch.delenv("XGEN_SERVICE_KEY_ID", raising=False)
    monkeypatch.setenv("XGEN_GATEWAY_PUBLIC_KEY_PEM", PUBLIC_KEY_PEM)
    monkeypatch.setenv("XGEN_SERVICE_NAME", "xgen-workflow")
    monkeypatch.setenv(
        "XGEN_TRUSTED_SERVICES",
        json.dumps(
            {
                "workflow-service": {
                    "key": SERVICE_KEY,
                    "permissions": ["document:read", "workflow:execute"],
                }
            }
        ),
    )


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _gateway_token(
    headers: Mapping[str, str] = CANONICAL,
    *,
    claim_overrides: Mapping[str, object] | None = None,
    protected: Mapping[str, object] | None = None,
    private_key: RSA.RsaKey = PRIVATE_KEY,
) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": "xgen-gateway",
        "aud": "xgen-workflow",
        "type": "principal",
        "iat": now,
        "exp": now + 60,
        "headers": dict(headers),
    }
    claims.update(claim_overrides or {})
    jwt_header = dict(protected or {"alg": "RS256", "typ": "JWT"})
    encoded_header = _b64(json.dumps(jwt_header, separators=(",", ":"), sort_keys=True).encode())
    encoded_claims = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = pkcs1_15.new(private_key).sign(SHA256.new(signing_input))
    return f"{encoded_header}.{encoded_claims}.{_b64(signature)}"


def _request(headers: Mapping[str, str] | None = None, *, raw=None) -> StarletteRequest:
    if raw is None:
        raw = [(name.lower().encode(), value.encode()) for name, value in (headers or {}).items()]
    scope = {"type": "http", "method": "GET", "path": "/", "headers": raw, "query_string": b""}
    return StarletteRequest(scope)


def _signed_request(headers: Mapping[str, str] = CANONICAL, **token_kwargs) -> StarletteRequest:
    complete = dict(headers)
    complete["x-xgen-principal"] = _gateway_token(headers, **token_kwargs)
    return _request(complete)


def test_gateway_principal_preserves_user_session_shape():
    session = get_user_info_by_gateway(_signed_request())

    assert session == {
        "user_id": 42,
        "user_name": "Kim Hae-soo",
        "is_superuser": False,
        "roles": ["developer", "reviewer"],
        "permissions": {"document:read", "workflow:execute"},
        "supervision": {"full": ["team-a"], "monitor": ["team-a"], "audit": ["team-a"]},
        "supervision_full": ["team-a"],
    }


@pytest.mark.parametrize(
    "request_factory",
    [
        lambda: _request(CANONICAL),
        lambda: _signed_request(claim_overrides={"aud": "another-audience"}),
        lambda: _signed_request(claim_overrides={"type": "service"}),
        lambda: _signed_request(claim_overrides={"iss": "another-gateway"}),
        lambda: _signed_request(claim_overrides={"exp": int(time.time()) - 1}),
        lambda: _signed_request(
            claim_overrides={"iat": int(time.time()), "exp": int(time.time()) + 61}
        ),
        lambda: _signed_request(protected={"alg": "HS256", "typ": "JWT"}),
    ],
    ids=["missing-proof", "audience", "type", "issuer", "expired", "too-long", "algorithm-confusion"],
)
def test_gateway_principal_rejects_invalid_proofs(request_factory):
    with pytest.raises(HTTPException) as exc:
        get_user_info_by_gateway(request_factory())
    assert exc.value.status_code == 401


def test_gateway_principal_requires_configured_exact_audience(monkeypatch: pytest.MonkeyPatch):
    request = _signed_request()
    monkeypatch.delenv("XGEN_SERVICE_NAME")

    with pytest.raises(HTTPException) as exc:
        get_user_info_by_gateway(request)
    assert exc.value.status_code == 503


def test_gateway_principal_rejects_tampered_bound_header():
    request = _signed_request({**CANONICAL, "x-user-id": "99"})
    raw = list(request.scope["headers"])
    raw[0] = (b"x-user-id", b"42")

    with pytest.raises(HTTPException) as exc:
        get_user_info_by_gateway(_request(raw=raw))
    assert exc.value.status_code == 401


def test_gateway_principal_rejects_tampered_signature():
    request = _signed_request()
    raw = list(request.scope["headers"])
    index = next(index for index, (name, _) in enumerate(raw) if name == b"x-xgen-principal")
    token = raw[index][1].decode("ascii")
    replacement = "A" if token[-1] != "A" else "B"
    raw[index] = (b"x-xgen-principal", f"{token[:-1]}{replacement}".encode("ascii"))

    with pytest.raises(HTTPException) as exc:
        get_user_info_by_gateway(_request(raw=raw))
    assert exc.value.status_code == 401


def test_gateway_principal_rejects_extra_payload_fields():
    with pytest.raises(HTTPException) as exc:
        get_user_info_by_gateway(_signed_request(claim_overrides={"jti": "unexpected"}))
    assert exc.value.status_code == 401


def test_gateway_keyring_accepts_current_and_bounded_previous_keys(
    monkeypatch: pytest.MonkeyPatch,
):
    cutoff = int(time.time()) + 120
    monkeypatch.setenv("XGEN_GATEWAY_KEY_ID", "current")
    monkeypatch.setenv(
        "XGEN_GATEWAY_PUBLIC_KEYS",
        json.dumps(
            {
                "current": {"key": PUBLIC_KEY_PEM},
                "previous": {"key": PREVIOUS_PUBLIC_KEY_PEM, "not_after": cutoff},
            }
        ),
    )

    current = _signed_request(protected={"alg": "RS256", "typ": "JWT", "kid": "current"})
    previous = _signed_request(
        protected={"alg": "RS256", "typ": "JWT", "kid": "previous"},
        private_key=PREVIOUS_PRIVATE_KEY,
    )

    assert get_user_info_by_gateway(current)["user_id"] == 42
    assert get_user_info_by_gateway(previous)["user_id"] == 42


def test_gateway_keyring_rejects_unkeyed_unknown_and_overlapping_previous_tokens(
    monkeypatch: pytest.MonkeyPatch,
):
    now = int(time.time())
    monkeypatch.setenv("XGEN_GATEWAY_KEY_ID", "current")
    monkeypatch.setenv(
        "XGEN_GATEWAY_PUBLIC_KEYS",
        json.dumps(
            {
                "current": {"key": PUBLIC_KEY_PEM},
                "previous": {"key": PREVIOUS_PUBLIC_KEY_PEM, "not_after": now + 30},
            }
        ),
    )
    candidates = [
        _signed_request(),
        _signed_request(
            protected={"alg": "RS256", "typ": "JWT", "kid": "unknown"}
        ),
        _signed_request(
            protected={"alg": "RS256", "typ": "JWT", "kid": "previous"},
            private_key=PREVIOUS_PRIVATE_KEY,
            claim_overrides={"iat": now, "exp": now + 60},
        ),
    ]

    for request in candidates:
        with pytest.raises(HTTPException) as exc:
            get_user_info_by_gateway(request)
        assert exc.value.status_code == 401


@pytest.mark.parametrize("duplicate", ["x-user-id", "x-xgen-principal"])
def test_gateway_principal_rejects_duplicate_security_headers(duplicate: str):
    request = _signed_request()
    raw = list(request.scope["headers"])
    value = next(value for name, value in raw if name == duplicate.encode())
    raw.append((duplicate.encode(), value))

    with pytest.raises(HTTPException) as exc:
        get_user_info_by_gateway(_request(raw=raw))
    assert exc.value.status_code == 401


@pytest.mark.parametrize("alias", ["x-user_id", "x-userid", "x-user_permissions"])
def test_gateway_principal_rejects_ambiguous_aliases(alias: str):
    request = _signed_request()
    raw = list(request.scope["headers"])
    raw.append((alias.encode(), b"attacker"))

    with pytest.raises(HTTPException) as exc:
        get_user_info_by_gateway(_request(raw=raw))
    assert exc.value.status_code == 401


def test_existing_verified_principal_can_be_forwarded_without_resigning():
    request = _signed_request()
    forwarded = get_verified_principal_headers(request)

    assert forwarded["X-Xgen-Principal"] == request.headers["x-xgen-principal"]
    assert forwarded["X-User-ID"] == "42"
    assert get_user_info_by_gateway(_request(forwarded))["user_id"] == 42


def test_service_assertion_is_scoped_and_accepted():
    forwarded = service_principal_headers(
        {
            "X-User-ID": "7",
            "X-User-Name": "worker",
            "X-User-Permissions": "document:read",
        },
        audience="xgen-workflow",
        service_name="workflow-service",
        signing_key=SERVICE_KEY,
    )

    session = get_user_info_by_gateway(_request(forwarded))
    assert session["user_id"] == 7
    assert session["permissions"] == {"document:read"}
    assert session["is_superuser"] is False


def test_service_keyring_accepts_current_and_bounded_previous_keys(
    monkeypatch: pytest.MonkeyPatch,
):
    now = int(time.time())
    monkeypatch.setenv(
        "XGEN_TRUSTED_SERVICES",
        json.dumps(
            {
                "workflow-service": {
                    "keys": {
                        "current": {"key": SERVICE_KEY},
                        "previous": {
                            "key": PREVIOUS_SERVICE_KEY,
                            "not_after": now + 120,
                        },
                    },
                    "permissions": ["document:read"],
                }
            }
        ),
    )
    delegated = {
        "X-User-ID": "7",
        "X-User-Name": "worker",
        "X-User-Permissions": "document:read",
    }

    current = service_principal_headers(
        delegated,
        audience="xgen-workflow",
        service_name="workflow-service",
        signing_key=SERVICE_KEY,
        key_id="current",
    )
    previous = service_principal_headers(
        delegated,
        audience="xgen-workflow",
        service_name="workflow-service",
        signing_key=PREVIOUS_SERVICE_KEY,
        key_id="previous",
    )

    assert get_user_info_by_gateway(_request(current))["user_id"] == 7
    assert get_user_info_by_gateway(_request(previous))["user_id"] == 7


def test_service_keyring_rejects_unkeyed_revoked_and_cutoff_overlap(
    monkeypatch: pytest.MonkeyPatch,
):
    now = int(time.time())
    delegated = {
        "X-User-ID": "7",
        "X-User-Name": "worker",
        "X-User-Permissions": "document:read",
    }
    monkeypatch.setenv(
        "XGEN_TRUSTED_SERVICES",
        json.dumps(
            {
                "workflow-service": {
                    "keys": {
                        "current": {"key": SERVICE_KEY},
                        "previous": {
                            "key": PREVIOUS_SERVICE_KEY,
                            "not_after": now + 30,
                        },
                    },
                    "permissions": ["document:read"],
                }
            }
        ),
    )
    unkeyed = service_principal_headers(
        delegated,
        audience="xgen-workflow",
        service_name="workflow-service",
        signing_key=SERVICE_KEY,
    )
    overlap = service_principal_headers(
        delegated,
        audience="xgen-workflow",
        service_name="workflow-service",
        signing_key=PREVIOUS_SERVICE_KEY,
        key_id="previous",
        now=now,
        ttl_seconds=60,
    )
    revoked = service_principal_headers(
        delegated,
        audience="xgen-workflow",
        service_name="workflow-service",
        signing_key=PREVIOUS_SERVICE_KEY,
        key_id="revoked",
    )

    for headers in (unkeyed, overlap, revoked):
        with pytest.raises(HTTPException) as exc:
            get_user_info_by_gateway(_request(headers))
        assert exc.value.status_code == 401


def test_service_assertion_rejects_permissions_outside_receiver_allowlist():
    forwarded = service_principal_headers(
        {"X-User-ID": "7", "X-User-Name": "worker", "X-User-Permissions": "admin:delete"},
        audience="xgen-workflow",
        service_name="workflow-service",
        signing_key=SERVICE_KEY,
    )

    with pytest.raises(HTTPException) as exc:
        get_user_info_by_gateway(_request(forwarded))
    assert exc.value.status_code == 401


@pytest.mark.parametrize(
    "extra",
    [
        {"X-User-Superuser": "true"},
        {"X-User-Permissions": "*:*"},
        {"X-User-Permissions": "document:*"},
        {"X-User-Permissions": "%2A%3A%2A"},
    ],
)
def test_service_signer_refuses_privilege_escalation(extra: Mapping[str, str]):
    with pytest.raises(PrincipalValidationError):
        sign_principal_for_service(
            {"X-User-ID": "7", "X-User-Name": "worker", **extra},
            audience="xgen-workflow",
            service_name="workflow-service",
            signing_key=SERVICE_KEY,
        )


def _middleware_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(PrincipalMiddleware)

    @app.get("/")
    async def root(request: Request):
        return {"verified": bool(request.scope.get("state", {}).get("xgen_principal_verified"))}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("ok")
        await websocket.close()

    return app


def test_middleware_allows_anonymous_http_and_verified_http():
    client = TestClient(_middleware_app())
    assert client.get("/").json() == {"verified": False}

    response = client.get("/", headers=dict(_signed_request().headers))
    assert response.status_code == 200
    assert response.json() == {"verified": True}


def test_middleware_allows_anonymous_http_without_verifier_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("XGEN_SERVICE_NAME")
    monkeypatch.delenv("XGEN_GATEWAY_PUBLIC_KEY_PEM")
    response = TestClient(_middleware_app()).get("/")
    assert response.status_code == 200
    assert response.json() == {"verified": False}


def test_middleware_rejects_unsigned_http_identity():
    response = TestClient(_middleware_app()).get("/", headers={"X-User-ID": "42"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid XGen principal"}


def test_middleware_rejects_unsigned_websocket_identity():
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(_middleware_app()).websocket_connect("/ws", headers={"X-User-ID": "42"}):
            pass
    assert exc.value.code == 4401


def test_middleware_allows_anonymous_websocket():
    with TestClient(_middleware_app()).websocket_connect("/ws") as websocket:
        assert websocket.receive_text() == "ok"


def test_middleware_allows_verified_websocket():
    with TestClient(_middleware_app()).websocket_connect(
        "/ws", headers=dict(_signed_request().headers)
    ) as websocket:
        assert websocket.receive_text() == "ok"
