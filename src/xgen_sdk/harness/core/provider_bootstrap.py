"""
Provider Bootstrap — state.provider lazy 초기화 공용 헬퍼.

v0.12.0 에서 s00_harness.main_call._lazy_init_provider 로직을 여기로 이관. Planner(s00) 도
동일 경로로 provider 를 띄우고, 본문 LLM 호출은 이 함수에 위임한다. 중복 제거 + Planner
독립성 확보.

조회 순서 (변경 없음 — feedback_redis_env_order 준수):
  1. ExecutionContext(context var)
  2. ServiceProvider.config (Redis 우선)
  3. 환경변수
  4. 파일 폴백 (XGEN_HARNESS_API_KEY_FILE_DIR)
"""

from __future__ import annotations

import logging
import os
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .state import PipelineState
    from ..providers.base import LLMProvider

logger = logging.getLogger("harness.provider_bootstrap")


async def ensure_provider(state: "PipelineState", *, stage_id: str = "") -> "LLMProvider":
    """state.provider 가 없으면 config 를 바탕으로 생성해 주입.

    이미 주입되어 있으면 그대로 반환. API 키/모델/base_url 해석 순서는 기존
    본문 LLM 호출 로직 그대로 (Redis 우선). 해석 실패 시 `PipelineAbortError` raise.

    Parameters
    ----------
    state : PipelineState
    stage_id : str
        에러 이벤트 추적용 식별자. s00_harness / s00_harness.main_call 등.
    """
    if state.provider is not None:
        return state.provider

    from ..errors import PipelineAbortError
    from .execution_context import get_api_key as ctx_get_api_key
    from ..providers import (
        create_provider, get_api_key_env, resolve_api_key_from_file,
        get_default_model,
    )

    config = state.config
    if not config:
        raise PipelineAbortError("Config not set", stage_id or "provider_bootstrap")

    provider_name: str = (config.provider or "").lower()
    model_name: str = config.model or get_default_model(provider_name)
    if not provider_name or not model_name:
        raise PipelineAbortError(
            f"Provider/model not resolved (provider={provider_name!r}, model={model_name!r})",
            stage_id or "provider_bootstrap",
        )

    # ━━━━ API key ━━━━
    api_key: Optional[str] = ctx_get_api_key()
    if not api_key:
        services = state.metadata.get("services")
        if services and getattr(services, "config", None):
            try:
                api_key = await services.config.get_api_key(provider_name)
            except Exception as e:
                logger.debug("[provider_bootstrap] ServiceProvider API key lookup failed: %s", e)
    if not api_key:
        env_var = get_api_key_env(provider_name)
        api_key = os.environ.get(env_var, "")
        if not api_key:
            api_key = resolve_api_key_from_file(provider_name)
    if not api_key:
        raise PipelineAbortError(
            f"{provider_name} API key not configured",
            stage_id or "provider_bootstrap",
        )

    # ━━━━ base_url ━━━━
    base_url: Optional[str] = None
    env_var_url = f"{provider_name.upper()}_API_BASE_URL"
    services = state.metadata.get("services")
    if services and getattr(services, "config", None):
        try:
            get_setting = getattr(services.config, "get_setting", None)
            if get_setting is not None:
                base_url = await get_setting(env_var_url) or None
            else:
                base_url = await services.config.get_value(env_var_url, "") or None
        except Exception as e:
            logger.debug("[provider_bootstrap] base_url Redis 조회 실패: %s", e)
    if not base_url:
        base_url = os.environ.get(env_var_url, "") or None

    state.provider = create_provider(provider_name, api_key, model_name, base_url=base_url)
    logger.info(
        "[provider_bootstrap] lazy init provider=%s, model=%s (called_from=%s)",
        provider_name, model_name, stage_id or "?",
    )
    return state.provider


async def resolve_judge_provider(
    state: "PipelineState", *, stage_id: str = "",
) -> "LLMProvider":
    """v1.9.0 P0#3 — judge_provider 별도 인스턴스 해석.

    s08_decide.judge_then_loop 가 호출. 의사결정:

    1. ``config.judge_use_main=True`` → 본문 provider 그대로 (사용자 명시 "본문 재사용"
       UI chip. judge_model 박혀있어도 무시 — v1.7.1 정신 보존).
    2. ``config.judge_provider`` 빈 칸 → 본문 provider 그대로 (BC default).
    3. ``judge_provider == config.provider`` → 본문 provider 그대로. ``judge_model``
       박혀있으면 aux_call 에서 model 인자로 override (현재 동작 = 같은 provider 다른 model).
    4. ``judge_provider != config.provider`` → **별도 provider 인스턴스 구축**.
       API key / base_url 은 동일 lookup 체인 (ctx → ServiceProvider.config → env →
       file). judge 의 model 이 비어있으면 그 provider 의 ``get_default_model`` 사용.

    "Judge 가 자기 답을 자기 평가" 약점 (self-promotion bias) 회피 + UI 의 judge_provider
    필드 진정성 보장 (v1.1.0 부터 필드만 존재 / 동작 X 였던 거 fix).
    """
    main_provider = await ensure_provider(state, stage_id=stage_id)

    from ..errors import PipelineAbortError
    from .execution_context import get_api_key as ctx_get_api_key
    from ..providers import (
        create_provider, get_api_key_env, resolve_api_key_from_file,
        get_default_model,
    )

    config = state.config
    if config is None:
        return main_provider

    if bool(getattr(config, "judge_use_main", False)):
        return main_provider

    judge_provider_name = (str(getattr(config, "judge_provider", "") or "")).strip().lower()
    if not judge_provider_name:
        return main_provider
    main_provider_name = (str(getattr(config, "provider", "") or "")).strip().lower()
    if judge_provider_name == main_provider_name:
        # 같은 provider — 별도 인스턴스 띄울 필요 없음. aux_call 이 model 인자로 override.
        return main_provider

    # ── 별도 provider 인스턴스 구축 ──
    judge_model = (str(getattr(config, "judge_model", "") or "")).strip()
    if not judge_model:
        judge_model = get_default_model(judge_provider_name)
    if not judge_model:
        # judge_provider 박혀있으나 model 해석 불가 → 본문 provider 폴백 (graceful).
        logger.warning(
            "[provider_bootstrap] judge_provider=%s 박혀있으나 default model 미발견 — "
            "본문 provider 폴백",
            judge_provider_name,
        )
        return main_provider

    # API 키 lookup (본문과 동일 체인). 단 — ctx 의 키는 본문 provider 용일 수 있어
    # judge 에 그대로 쓰면 안 됨. ServiceProvider / env / file 만 사용.
    api_key: Optional[str] = None
    services = state.metadata.get("services")
    if services and getattr(services, "config", None):
        try:
            api_key = await services.config.get_api_key(judge_provider_name)
        except Exception as e:
            logger.debug(
                "[provider_bootstrap] judge_provider %s ServiceProvider API key lookup 실패: %s",
                judge_provider_name, e,
            )
    if not api_key:
        env_var = get_api_key_env(judge_provider_name)
        api_key = os.environ.get(env_var, "")
        if not api_key:
            api_key = resolve_api_key_from_file(judge_provider_name)
    if not api_key:
        # 키 없으면 graceful 폴백 — judge 전용 키 미구성을 사용자에게 알려야 하나
        # judge 실패로 전체 라운드 abort 시키지 않음 (사용자 호소: 답변 우선).
        logger.warning(
            "[provider_bootstrap] judge_provider=%s API key 미구성 — 본문 provider 폴백",
            judge_provider_name,
        )
        return main_provider

    # base_url
    base_url: Optional[str] = None
    env_var_url = f"{judge_provider_name.upper()}_API_BASE_URL"
    if services and getattr(services, "config", None):
        try:
            get_setting = getattr(services.config, "get_setting", None)
            if get_setting is not None:
                base_url = await get_setting(env_var_url) or None
            else:
                base_url = await services.config.get_value(env_var_url, "") or None
        except Exception:
            base_url = None
    if not base_url:
        base_url = os.environ.get(env_var_url, "") or None

    try:
        judge_provider_inst = create_provider(
            judge_provider_name, api_key, judge_model, base_url=base_url,
        )
        logger.info(
            "[provider_bootstrap] judge provider 별도 인스턴스: %s/%s "
            "(본문 %s/%s 와 분리)",
            judge_provider_name, judge_model,
            main_provider_name, getattr(main_provider, "model_name", "?"),
        )
        return judge_provider_inst
    except Exception as e:
        logger.warning(
            "[provider_bootstrap] judge_provider=%s 인스턴스 생성 실패 (%s) — 본문 폴백",
            judge_provider_name, e,
        )
        return main_provider
