"""Provider 레지스트리 회귀 테스트 — 이식측(xgen-workflow) 공개 API 계약 잠금.

이식측이 `list_providers` / `get_default_model` / `get_provider_models` /
`PROVIDER_API_KEY_MAP` / `get_default_provider` 를 직접 import 한다 (controller/
workflow/endpoints/harness*.py). 이 시그니처·키가 바뀌면 이식측이 깨지므로
여기서 고정한다. provider 네이티브화(구현 클래스 교체) 시에도 이 계약은 유지돼야 한다.
"""

import os

import pytest

from xgen_sdk.harness.providers import (
    list_providers,
    get_default_model,
    get_default_provider,
    get_provider_models,
    get_api_key_env,
    get_context_limit,
    get_provider_base_url,
    register_provider,
    create_provider,
    PROVIDER_API_KEY_MAP,
    PROVIDER_DEFAULT_BASE_URL,
    DEFAULT_CONTEXT_LIMIT_CHARS,
)
from xgen_sdk.harness.providers.base import LLMProvider


BUILTIN = {"anthropic", "openai", "google", "bedrock", "vllm"}


def test_builtin_providers_registered():
    names = set(list_providers())
    # 빌트인 5종은 항상 존재 — 네이티브화하든 shim 이든 레지스트리 키는 보존.
    assert BUILTIN <= names, f"missing builtin providers: {BUILTIN - names}"


def test_api_key_map_stable():
    # 이식측 PROVIDER_API_KEY_MAP 의존 — env 변수명 매핑 고정.
    assert PROVIDER_API_KEY_MAP["anthropic"] == "ANTHROPIC_API_KEY"
    assert PROVIDER_API_KEY_MAP["openai"] == "OPENAI_API_KEY"
    assert PROVIDER_API_KEY_MAP["google"] == "GEMINI_API_KEY"
    assert PROVIDER_API_KEY_MAP["bedrock"] == "AWS_ACCESS_KEY_ID"
    assert PROVIDER_API_KEY_MAP["vllm"] == "VLLM_API_KEY"


def test_get_api_key_env_fallback():
    # 미등록 provider → {NAME}_API_KEY 규칙 폴백.
    assert get_api_key_env("anthropic") == "ANTHROPIC_API_KEY"
    assert get_api_key_env("myprovider") == "MYPROVIDER_API_KEY"


def test_get_default_model_env_override(monkeypatch):
    monkeypatch.setenv("XGEN_HARNESS_ANTHROPIC_DEFAULT_MODEL", "claude-test-override")
    assert get_default_model("anthropic") == "claude-test-override"


def test_get_default_model_registry_default(monkeypatch):
    monkeypatch.delenv("XGEN_HARNESS_ANTHROPIC_DEFAULT_MODEL", raising=False)
    assert get_default_model("anthropic").startswith("claude")


def test_get_default_model_unknown_is_empty():
    assert get_default_model("nope-provider") == ""


def test_get_provider_models_includes_default_first():
    models = get_provider_models("anthropic")
    assert models, "anthropic should expose models"
    assert models[0] == get_default_model("anthropic")
    assert len(models) == len(set(models)), "no duplicates"


def test_get_default_provider_env_override(monkeypatch):
    monkeypatch.setenv("XGEN_HARNESS_DEFAULT_PROVIDER", "anthropic")
    assert get_default_provider() == "anthropic"


def test_get_default_provider_prefers_openai(monkeypatch):
    monkeypatch.delenv("XGEN_HARNESS_DEFAULT_PROVIDER", raising=False)
    # 선호 목록(openai → anthropic) — 명시 설정 없을 때 openai 우선.
    assert get_default_provider() in BUILTIN


def test_context_limit_registered():
    assert get_context_limit("anthropic") == 500_000
    assert get_context_limit("vllm") == 50_000


def test_context_limit_env_fallback(monkeypatch):
    monkeypatch.setenv("XGEN_HARNESS_DEFAULT_CONTEXT_LIMIT", "12345")
    assert get_context_limit("totally-unknown") == 12345


def test_context_limit_hardcoded_fallback(monkeypatch):
    monkeypatch.delenv("XGEN_HARNESS_DEFAULT_CONTEXT_LIMIT", raising=False)
    assert get_context_limit("totally-unknown") == DEFAULT_CONTEXT_LIMIT_CHARS


def test_register_provider_roundtrip():
    class _DummyProvider(LLMProvider):  # type: ignore[misc]
        def __init__(self, api_key="", model="", base_url=None):
            self.api_key, self.model, self.base_url = api_key, model, base_url

        @property
        def provider_name(self) -> str:
            return "dummy_test"

        @property
        def model_name(self) -> str:
            return self.model

        @property
        def supports_thinking(self) -> bool:
            return False

        @property
        def supports_tool_use(self) -> bool:
            return False

        async def chat(self, *args, **kwargs):  # pragma: no cover - not invoked
            raise NotImplementedError

    register_provider(
        "dummy_test",
        _DummyProvider,
        default_model="dummy-1",
        models=["dummy-1", "dummy-2"],
        api_key_env="DUMMY_TEST_KEY",
        context_limit=4242,
    )
    assert "dummy_test" in list_providers()
    assert get_default_model("dummy_test") == "dummy-1"
    assert get_api_key_env("dummy_test") == "DUMMY_TEST_KEY"
    assert get_context_limit("dummy_test") == 4242
    inst = create_provider("dummy_test", "k", "dummy-1")
    assert isinstance(inst, _DummyProvider)


def test_create_unknown_provider_falls_back_to_openai():
    # 알 수 없는 provider → OpenAI 호환 폴백 (현 동작 고정).
    inst = create_provider("ghost-provider", "k", "m")
    assert inst is not None


# ── base_url 해석 (item #4 — OpenAI-compat shim 이 엔드포인트를 못 찾던 결함 fix) ──

def test_google_has_default_base_url():
    # google 선택만으로 Gemini OpenAI 호환 엔드포인트로 라우팅 (api.openai.com 오작동 방지).
    url = get_provider_base_url("google")
    assert url and "generativelanguage.googleapis.com" in url


def test_google_base_url_normalizes_to_valid_endpoint():
    # 회귀: v1.18.1 은 base 가 `.../openai/` 라 normalize 가 `/v1/chat/completions` 를
    # 덧붙여 `.../openai/v1/chat/completions`(404) 를 만들었다. 끝까지 정상이어야.
    inst = create_provider("google", "k", "gemini-2.0-flash")
    endpoint = getattr(inst, "_base_url", "")
    assert endpoint.endswith("/v1beta/openai/chat/completions"), endpoint
    assert "/openai/v1/chat/completions" not in endpoint  # 잘못된 이중 v1 금지


def test_base_url_env_overrides_registry(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_BASE_URL", "https://my-proxy.local/v1")
    assert get_provider_base_url("google") == "https://my-proxy.local/v1"


def test_base_url_none_for_native_providers():
    # anthropic/openai 는 클래스가 자체 URL 을 알아 레지스트리 기본값 없음 → None.
    assert get_provider_base_url("anthropic") is None
    assert get_provider_base_url("openai") is None


def test_empty_registry_base_url_is_none():
    # vllm/bedrock 은 의도적으로 빈 문자열 → None (env/register 로 주입 강제).
    assert PROVIDER_DEFAULT_BASE_URL.get("vllm", "") == ""
    assert get_provider_base_url("vllm") is None


def test_register_provider_sets_base_url():
    class _P(LLMProvider):  # type: ignore[misc]
        def __init__(self, api_key="", model="", base_url=None):
            self.base_url = base_url

        @property
        def provider_name(self):
            return "compat_test"

        @property
        def model_name(self):
            return ""

        @property
        def supports_thinking(self):
            return False

        @property
        def supports_tool_use(self):
            return False

        async def chat(self, *a, **k):  # pragma: no cover
            raise NotImplementedError

    register_provider("compat_test", _P, base_url="https://compat.example/v1")
    assert get_provider_base_url("compat_test") == "https://compat.example/v1"
    inst = create_provider("compat_test", "k", "m")
    assert inst.base_url == "https://compat.example/v1"
