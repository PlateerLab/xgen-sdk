"""v1.18.3 fix 회귀: compile python-literal 무손상 + frozen_source SSRF 가드.

둘 다 순수 함수 — 무네트워크.
"""

import ast

import pytest

from xgen_sdk.harness.compile.python_compile import _dict_to_python_literal
from xgen_sdk.harness.tools.frozen_source import _host_is_blocked


# ── python literal emission (문자열 값 무손상) ──

def test_literal_preserves_words_null_true_false():
    # 이전 버그: json 문자열 치환이 string 값 내부 null/true/false 까지 바꿈.
    obj = {
        "system_prompt": "judge true or false; null check",
        "arr": ["a null b", "true story"],
        "flags": {"x": True, "y": False, "z": None},
    }
    lit = _dict_to_python_literal(obj)
    assert ast.literal_eval(lit) == obj   # valid Python + 정확 round-trip


def test_literal_handles_unicode_and_nesting():
    obj = {"한글": "값 true", "n": [1, 2, {"k": None}]}
    assert ast.literal_eval(_dict_to_python_literal(obj)) == obj


# ── SSRF 가드 ──

@pytest.mark.parametrize("host", [
    "127.0.0.1", "::1", "localhost", "x.localhost",
    "169.254.169.254",            # 클라우드 메타데이터 — 자격증명 탈취 벡터
    "metadata.google.internal",
    "0.0.0.0",
])
def test_blocked_hosts(host):
    assert _host_is_blocked(host) is True


@pytest.mark.parametrize("host", ["1.1.1.1", "8.8.8.8"])
def test_public_hosts_allowed(host):
    assert _host_is_blocked(host) is False


def test_private_blocked_only_when_opted_in():
    # RFC1918 은 기본 허용(사내 API), block_private=True 일 때만 차단.
    assert _host_is_blocked("10.0.0.5") is False
    assert _host_is_blocked("10.0.0.5", block_private=True) is True
    assert _host_is_blocked("192.168.1.1", block_private=True) is True


def test_empty_host_blocked():
    assert _host_is_blocked("") is True
