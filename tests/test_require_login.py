"""require_login — 로그인만 요구하는 Depends 팩토리 (1.38.0).

계약:
  1. 레지스트리에 아무것도 등록하지 않는다 ("기본 기능" 은 권한 키를 갖지 않는다).
  2. 게이트웨이 헤더가 없으면 401 (require_perm 과 동일 경로).
  3. 권한이 하나도 없는 사용자도 통과하고, 핸들러가 받는 session 은 require_perm 이
     넘기던 것과 같은 dict 이다.
  4. superuser 도 그대로 통과한다(permissions == {"*:*"}).
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from fastapi import HTTPException
from starlette.requests import Request

from xgen_sdk.auth import require_login, require_perm
from xgen_sdk.auth.permission_registry import registry


PRIVATE_KEY = RSA.generate(2048)


@pytest.fixture(autouse=True)
def _gateway_public_key(monkeypatch):
    monkeypatch.setenv("XGEN_GATEWAY_PUBLIC_KEY_PEM", PRIVATE_KEY.public_key().export_key().decode("ascii"))
    monkeypatch.setenv("XGEN_SERVICE_NAME", "xgen-workflow")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _gateway_token(headers: dict[str, str]) -> str:
    canonical = {
        "x-user-id": headers["X-User-ID"],
        "x-user-name": headers["X-User-Name"],
        "x-user-superuser": headers.get("X-User-Superuser", "false"),
        "x-user-roles": headers.get("X-User-Roles", ""),
        "x-user-permissions": headers.get("X-User-Permissions", ""),
        "x-user-supervision-full": headers.get("X-User-Supervision-Full", ""),
    }
    now = int(time.time())
    protected = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":"), sort_keys=True).encode())
    payload = _b64(
        json.dumps(
            {
                "iss": "xgen-gateway",
                "aud": "xgen-workflow",
                "type": "principal",
                "iat": now,
                "exp": now + 60,
                "headers": canonical,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    signing_input = f"{protected}.{payload}".encode("ascii")
    signature = pkcs1_15.new(PRIVATE_KEY).sign(SHA256.new(signing_input))
    return f"{protected}.{payload}.{_b64(signature)}"


def _request(headers: dict[str, str] | None = None) -> Request:
    if headers:
        token = _gateway_token(headers)
        headers = {
            "X-User-ID": headers["X-User-ID"],
            "X-User-Name": headers["X-User-Name"],
            "X-User-Superuser": headers.get("X-User-Superuser", "false"),
            "X-User-Roles": headers.get("X-User-Roles", ""),
            "X-User-Permissions": headers.get("X-User-Permissions", ""),
            "X-User-Supervision-Full": headers.get("X-User-Supervision-Full", ""),
            "X-Xgen-Principal": token,
        }
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": "GET", "path": "/", "headers": raw, "query_string": b""}
    return Request(scope)


def test_require_login_registers_nothing():
    before = set(registry.all_permission_keys())
    require_login(description="문서용 설명은 등록되지 않는다")
    assert set(registry.all_permission_keys()) == before


def test_require_login_401_without_gateway_headers():
    dep = require_login()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(dep(_request()))
    assert exc.value.status_code == 401


def test_require_login_passes_user_with_no_permissions():
    dep = require_login()
    session = asyncio.run(dep(_request({"X-User-ID": "42", "X-User-Name": "u"})))
    assert session["user_id"] == 42
    assert session["permissions"] == set()
    assert session["is_superuser"] is False


def test_require_login_passes_superuser():
    dep = require_login()
    session = asyncio.run(dep(_request({"X-User-ID": "1", "X-User-Name": "root", "X-User-Superuser": "true"})))
    assert session["is_superuser"] is True
    assert session["permissions"] == {"*:*"}


def test_require_login_session_shape_matches_require_perm():
    headers = {"X-User-ID": "7", "X-User-Name": "dev", "X-User-Permissions": "main.x:read", "X-User-Roles": "r1"}
    gated = asyncio.run(require_perm("main.x:read", description="t")(_request(headers)))
    open_ = asyncio.run(require_login()(_request(headers)))
    assert gated == open_
