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

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from xgen_sdk.auth import require_login, require_perm
from xgen_sdk.auth.permission_registry import registry


def _request(headers: dict[str, str] | None = None) -> Request:
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
