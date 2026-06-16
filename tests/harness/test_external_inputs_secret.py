"""external_inputs SECRET 비-baking 회귀 테스트 (v1.18.3 보안 fix).

SECRET 타입은 평문 default 를 배포 산출물에 박지 않는다(유출 방지) + baked default
로 런타임 폴백하지 않는다(env/override 강제).
"""

import pytest

from xgen_sdk.harness.compile.external_inputs import (
    ExternalInputSpec,
    InputType,
    scan_placeholders,
    collect_runtime_values,
    MissingExternalInputError,
)


def test_secret_to_dict_drops_default():
    sp = ExternalInputSpec(name="OPENAI_API_KEY", type="secret", default="sk-LIVE", required=False)
    d = sp.to_dict()
    assert "default" not in d            # 평문 시크릿 직렬화 금지
    assert d.get("required") is True     # SECRET 은 항상 required


def test_non_secret_keeps_default():
    sp = ExternalInputSpec(name="QDRANT_URL", type="url", default="http://q:6333")
    assert sp.to_dict().get("default") == "http://q:6333"


def test_from_dict_strips_secret_default():
    sp = ExternalInputSpec.from_dict("X_API_KEY", {"type": "secret", "default": "leaked", "required": False})
    assert sp.default is None
    assert sp.required is True


def test_scan_secret_ignores_inline_default():
    # ${OPENAI_API_KEY:sk-LIVE} — SECRET 으로 확정되면 인라인 default 무시.
    specs = scan_placeholders(
        {"system_prompt": "use ${OPENAI_API_KEY:sk-LIVE}"},
        registered_api_key_envs={"OPENAI_API_KEY"},
    )
    sp = specs["OPENAI_API_KEY"]
    assert sp.type == InputType.SECRET.value
    assert sp.default is None
    assert sp.required is True
    assert "sk-LIVE" not in str(sp.to_dict())


def test_collect_runtime_does_not_fallback_to_secret_default():
    # SECRET 에 default 가 박혀 있어도(레거시 spec) env 없으면 폴백하지 않고 에러.
    specs = {"K_API_KEY": ExternalInputSpec(name="K_API_KEY", type="secret", default="baked", required=True)}
    with pytest.raises(MissingExternalInputError):
        collect_runtime_values(specs, env={}, overrides={})


def test_collect_runtime_uses_env_for_secret():
    specs = {"K_API_KEY": ExternalInputSpec(name="K_API_KEY", type="secret", required=True)}
    resolved = collect_runtime_values(specs, env={"K_API_KEY": "real"}, overrides={})
    assert resolved["K_API_KEY"] == "real"
