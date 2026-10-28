from __future__ import annotations

import asyncio
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
    PrincipalConfigurationError,
    PrincipalValidationError,
    get_user_info_by_gateway,
    get_verified_principal_headers,
    service_principal_headers,
    sign_principal_for_service,
    validate_auth_configuration,
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
    for name in (
        "XGEN_AUTH_CONFIG_MODE", "XGEN_TRUSTED_SERVICE_KEYS", "XGEN_SERVICE_AUTHORIZATION_POLICY"
    ):
        monkeypatch.delenv(name, raising=False)
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


@pytest.fixture
def split_auth(monkeypatch: pytest.MonkeyPatch):
    keys = {"workflow-service": {"keys": {"current": {"key": SERVICE_KEY}}}}
    policy = {
        "schema_version": 1,
        "receiver": "xgen-workflow",
        "grants": {"workflow-service": {"permissions": ["document:read"]}},
    }

    def configure(*, key_map=None, authorization=None):
        monkeypatch.setenv("XGEN_AUTH_CONFIG_MODE", "split-v1")
        monkeypatch.delenv("XGEN_TRUSTED_SERVICES", raising=False)
        monkeypatch.setenv("XGEN_TRUSTED_SERVICE_KEYS", json.dumps(keys if key_map is None else key_map))
        monkeypatch.setenv(
            "XGEN_SERVICE_AUTHORIZATION_POLICY",
            json.dumps(policy if authorization is None else authorization),
        )

    configure()
    return configure, keys, policy


def _split_service_headers(**kwargs):
    return service_principal_headers(
        {"X-User-ID": "7", "X-User-Name": "worker", "X-User-Permissions": "document:read"},
        audience=kwargs.pop("audience", "xgen-workflow"),
        service_name="workflow-service",
        signing_key=kwargs.pop("signing_key", SERVICE_KEY),
        key_id=kwargs.pop("key_id", "current"),
        **kwargs,
    )


def test_split_mode_preserves_gateway_and_keyed_service_wire_contract(split_auth):
    validate_auth_configuration()
    assert get_user_info_by_gateway(_signed_request())["user_id"] == 42
    assert get_user_info_by_gateway(_request(_split_service_headers()))["permissions"] == {"document:read"}
    with TestClient(_middleware_app()) as client:
        assert client.get("/", headers=_split_service_headers()).json() == {"verified": True}


@pytest.mark.parametrize("permissions", [[], ["workflow:execute"]])
def test_split_grants_bound_the_declared_permissions(split_auth, permissions):
    configure, _, policy = split_auth
    policy["grants"]["workflow-service"]["permissions"] = permissions
    configure(authorization=policy)
    validate_auth_configuration()
    with pytest.raises(HTTPException) as exc:
        get_user_info_by_gateway(_request(_split_service_headers()))
    assert exc.value.status_code == 401


def test_split_key_without_grant_is_denied_even_for_empty_permission_profile(split_auth):
    configure, _, policy = split_auth
    policy["grants"] = {}
    configure(authorization=policy)
    validate_auth_configuration()
    headers = service_principal_headers(
        {"X-User-ID": "7", "X-User-Name": "worker"},
        audience="xgen-workflow", service_name="workflow-service",
        signing_key=SERVICE_KEY, key_id="current",
    )
    with pytest.raises(HTTPException) as exc:
        get_user_info_by_gateway(_request(headers))
    assert exc.value.status_code == 401


def test_split_empty_maps_allow_no_incoming_service_callers(split_auth):
    configure, _, policy = split_auth
    policy["grants"] = {}
    configure(key_map={}, authorization=policy)
    validate_auth_configuration()
    assert get_user_info_by_gateway(_signed_request())["user_id"] == 42
    with pytest.raises(HTTPException) as exc:
        get_user_info_by_gateway(_request(_split_service_headers()))
    assert exc.value.status_code == 401


@pytest.mark.parametrize("kind", ["audience", "header", "signature", "kid", "unkeyed"])
def test_split_service_rejects_tampering_and_wrong_audience(split_auth, monkeypatch, kind):
    options = {"audience": "xgen-documents"} if kind == "audience" else {}
    if kind == "kid":
        options["key_id"] = "unknown"
    if kind == "unkeyed":
        monkeypatch.delenv("XGEN_SERVICE_KEY_ID", raising=False)
        options["key_id"] = None
    if kind == "signature":
        options["signing_key"] = PREVIOUS_SERVICE_KEY
    headers = _split_service_headers(**options)
    if kind == "header":
        headers["X-User-Permissions"] = "workflow:execute"
    with pytest.raises(HTTPException) as exc:
        get_user_info_by_gateway(_request(headers))
    assert exc.value.status_code == 401


def test_split_retirement_and_key_revocation_are_effective_without_restart(split_auth):
    configure, keys, _ = split_auth
    now = int(time.time())
    keys["workflow-service"]["keys"]["previous"] = {
        "key": PREVIOUS_SERVICE_KEY, "not_after": now + 30,
    }
    configure(key_map=keys)
    validate_auth_configuration()
    bounded = _split_service_headers(signing_key=PREVIOUS_SERVICE_KEY, key_id="previous", now=now, ttl_seconds=20)
    overlap = _split_service_headers(signing_key=PREVIOUS_SERVICE_KEY, key_id="previous", now=now, ttl_seconds=60)
    assert get_user_info_by_gateway(_request(bounded))["user_id"] == 7
    with pytest.raises(HTTPException) as exc:
        get_user_info_by_gateway(_request(overlap))
    assert exc.value.status_code == 401
    del keys["workflow-service"]["keys"]["previous"]
    configure(key_map=keys)
    with pytest.raises(HTTPException) as exc:
        get_user_info_by_gateway(_request(bounded))
    assert exc.value.status_code == 401


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2), ("receiver", "xgen-documents"),
    ("grants", []), ("unexpected", "field"),
    ("grants", {"missing-service": {"permissions": []}}),
    ("grants", {"workflow-service": {"permissions": ["document:read", "document:read"]}}),
    ("grants", {"workflow-service": {"permissions": ["document:*"]}}),
    ("grants", {"workflow-service": {"permissions": ["document:%2A"]}}),
    ("grants", {"workflow-service": {"permissions": ["document:read,workflow:execute"]}}),
    ("grants", {"workflow-service": {"permissions": [" "]}}),
    ("grants", {"workflow-service": {"permissions": [1]}}),
    ("grants", {"workflow-service": {"permissions": [], "key": SERVICE_KEY}}),
])
def test_split_invalid_policy_fails_at_startup(split_auth, field, value):
    configure, _, policy = split_auth
    policy[field] = value
    configure(authorization=policy)
    with pytest.raises(PrincipalConfigurationError):
        validate_auth_configuration()


@pytest.mark.parametrize("entry", [
    {"key": SERVICE_KEY},
    {"keys": {}},
    {"keys": {"current": {"key": SERVICE_KEY}}, "permissions": []},
    {"keys": {"current": {"key": "short"}}},
    {"keys": {"current": {"key": SERVICE_KEY, "not_after": True}}},
    {"keys": {"current": {"key": SERVICE_KEY, "not_after": -1}}},
    {"keys": {"current": {"key": SERVICE_KEY, "not_after": 123}}},
    {"keys": {"one": {"key": SERVICE_KEY}, "two": {"key": PREVIOUS_SERVICE_KEY}}},
    {"keys": {"current": {"key": SERVICE_KEY, "extra": True}}},
])
def test_split_invalid_keys_fail_even_without_an_issuer_grant(split_auth, entry):
    configure, _, policy = split_auth
    policy["grants"] = {}
    configure(key_map={"workflow-service": entry}, authorization=policy)
    with pytest.raises(PrincipalConfigurationError):
        validate_auth_configuration()


@pytest.mark.parametrize("name,raw", [
    ("XGEN_TRUSTED_SERVICE_KEYS", None),
    ("XGEN_SERVICE_AUTHORIZATION_POLICY", None),
    ("XGEN_TRUSTED_SERVICE_KEYS", ""),
    ("XGEN_SERVICE_AUTHORIZATION_POLICY", "{}"),
    ("XGEN_TRUSTED_SERVICE_KEYS", '{"workflow-service":{},"workflow-service":{}}'),
    ("XGEN_SERVICE_AUTHORIZATION_POLICY", '{"schema_version":NaN}'),
    ("XGEN_SERVICE_AUTHORIZATION_POLICY", "[]"),
    ("XGEN_TRUSTED_SERVICES", "{}"),
    ("XGEN_GATEWAY_PUBLIC_KEY_PEM", "invalid"),
    ("XGEN_GATEWAY_PUBLIC_KEY_PEM", None),
    ("XGEN_SERVICE_NAME", None),
    ("XGEN_AUTH_CONFIG_MODE", "split-v2"),
    ("XGEN_AUTH_CONFIG_MODE", "legacy"),
])
def test_split_invalid_environment_fails_before_first_request(split_auth, monkeypatch, name, raw):
    if raw is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, raw)
    with pytest.raises(PrincipalConfigurationError) as exc:
        validate_auth_configuration()
    assert SERVICE_KEY not in str(exc.value)
    with pytest.raises(PrincipalConfigurationError):
        with TestClient(_middleware_app()):
            pass


def test_split_broken_policy_prevents_anonymous_and_gateway_requests(split_auth, monkeypatch):
    monkeypatch.delenv("XGEN_SERVICE_AUTHORIZATION_POLICY")
    client = TestClient(_middleware_app())
    assert client.get("/").status_code == 503
    assert client.get("/", headers=dict(_signed_request().headers)).status_code == 503


def test_split_bad_startup_emits_explicit_asgi_failure(split_auth, monkeypatch):
    monkeypatch.delenv("XGEN_SERVICE_AUTHORIZATION_POLICY")
    messages = []

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        messages.append(message)

    async def app(scope, receive, send):
        pytest.fail("an invalid auth configuration must not enter the application")

    with pytest.raises(PrincipalConfigurationError):
        asyncio.run(PrincipalMiddleware(app)({"type": "lifespan"}, receive, send))
    assert messages == [{
        "type": "lifespan.startup.failed", "message": "Invalid XGen authentication configuration",
    }]


def test_startup_validator_checks_complete_gateway_ring(monkeypatch):
    monkeypatch.setenv("XGEN_GATEWAY_KEY_ID", "current")
    monkeypatch.setenv("XGEN_GATEWAY_PUBLIC_KEYS", json.dumps({
        "current": {"key": PUBLIC_KEY_PEM},
        "previous": {"key": "invalid", "not_after": int(time.time()) + 30},
    }))
    with pytest.raises(PrincipalConfigurationError):
        validate_auth_configuration()


def test_startup_validator_preserves_legacy_and_unconfigured_local_development(monkeypatch):
    validate_auth_configuration()
    assert get_user_info_by_gateway(_request(service_principal_headers(
        {"X-User-ID": "7", "X-User-Name": "worker", "X-User-Permissions": "document:read"},
        audience="xgen-workflow", service_name="workflow-service", signing_key=SERVICE_KEY,
    )))["user_id"] == 7
    for name in ("XGEN_GATEWAY_PUBLIC_KEY_PEM", "XGEN_SERVICE_NAME", "XGEN_TRUSTED_SERVICES"):
        monkeypatch.delenv(name)
    validate_auth_configuration()
