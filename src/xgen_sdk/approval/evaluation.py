"""평가지 **선택 항목**의 정본 — 무엇이 켜졌는지는 서버가 정한다.

평가지 값에는 채점한 양식 버전의 선택 항목(``options``)이 실린다. 채움 판정
(:func:`xgen_sdk.approval.blocks.evaluation_gaps`)은 그것을 보고 영향 범위를
요구할지 정하고, core 는 ``agent_risk_grade`` 를 보고 거버넌스 위험 등급 원장에
남길지 정한다. 그 값을 **보낸 쪽이 적은 대로** 믿으면, 결재자가 API 로
``options.impact_scope=false`` 를 보내 필수 영향 범위를 건너뛰거나
``agent_risk_grade=false`` 로 원장에서 빠질 수 있다.

값이 들어오는 길은 여럿이다(core 상신 · 칸 채우기, workflow 배포 결재 상신,
workflow 결재 서비스). 모두 ``store.submit`` / ``store.fill_block`` 을 지나므로
여기 한 곳에서 :func:`resolve_options` 로 덮어쓴다.

규칙 원본은 core ``service/approval/evaluation_forms.py`` 이고, 그 파일은 이제 이
모듈의 이름을 다시 내보낸다. 프론트 정규화도 같은 벡터 표로 시험한다.

표 이름(``risk_assessment_templates`` / ``risk_assessment_policies``)은 core 의
내부 이름을 그대로 쓴다. 알림(:mod:`xgen_sdk.approval.notifier`)이
``user_notifications`` 를 읽듯이 같은 DB 를 직접 읽는다 — core 가 내려가 있어도
결재를 올릴 수 있어야 한다.
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Any, Dict, List, Optional

from xgen_sdk.approval import blocks as blocks_mod
from xgen_sdk.approval.engine import (
    EVALUATION_VERSION_MISMATCH_MESSAGE,
    EVALUATION_VERSION_NOT_FOUND_MESSAGE,
    EvaluationVersionMismatch,
    EvaluationVersionNotFound,
)
from xgen_sdk.approval.sql import q as _q

logger = logging.getLogger("approval-evaluation")

__all__ = [
    "IMPACT_SCOPE_CHOICES", "TRAIT_CHOICES", "FULL_OPTIONS", "FLAG_OPTIONS", "CHOICE_OPTIONS",
    "BUILTIN_TEMPLATE_ID", "LEGACY_TEMPLATE_ID",
    "normalize_options", "resolve_options",
    "EvaluationVersionNotFound", "EvaluationVersionMismatch",
    "EVALUATION_VERSION_NOT_FOUND_MESSAGE", "EVALUATION_VERSION_MISMATCH_MESSAGE",
]

#: 기본 제공 **AI 위험도 평가지**. 기본 양식이 하나도 없을 때 칸이 따르는 양식이다.
BUILTIN_TEMPLATE_ID = "ai-risk"
#: 옛 전역 정책 계보의 template_id. 버전 행의 template_id 가 비어 있으면 이것이다(core 모델 기본값).
LEGACY_TEMPLATE_ID = "default"


# ───────── 선택 항목 (core evaluation_forms 와 같은 규칙) ─────────

#: 영향 범위 선택지 — 화면 문구(mypage-approval risk.ko.ts scopeCustomer…)와 같다.
IMPACT_SCOPE_CHOICES: List[Dict[str, str]] = [
    {"code": "CUSTOMER", "label": "대고객"},
    {"code": "EMPLOYEES", "label": "임직원"},
    {"code": "DEPARTMENT", "label": "부서"},
    {"code": "PERSONAL", "label": "개인"},
]

#: 특성 체크 선택지 — 화면 문구(evalRiskTraitPersonalData / evalRiskTraitHighImpactAi)와 같다.
TRAIT_CHOICES: List[Dict[str, str]] = [
    {"code": "PERSONAL_DATA", "label": "개인정보 처리"},
    {"code": "HIGH_IMPACT_AI", "label": "고영향 AI 해당"},
]

#: 전부 켠 한 벌 = AI 위험도 평가지. 선택 항목이 생기기 전의 양식은 모두 이것이었다.
#: 가장 엄격한 한 벌이기도 하다 — 양식을 읽지 못하면 이것으로 본다.
FULL_OPTIONS: Dict[str, Any] = {
    "mitigation": True,
    "impact_scope": {"enabled": True, "choices": IMPACT_SCOPE_CHOICES},
    "traits": {"enabled": True, "choices": TRAIT_CHOICES},
    "attachments": True,
    "agent_risk_grade": True,
}

#: 켜고 끄기만 하는 선택 항목.
FLAG_OPTIONS = ("mitigation", "attachments", "agent_risk_grade")
#: 켜면 선택지를 함께 드는 선택 항목(``{enabled, choices}``).
CHOICE_OPTIONS = ("impact_scope", "traits")


def _choices(raw: Any, default: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """``[{code, label}]`` 만 남긴다. 비었거나 모양이 틀리면 기본 선택지."""
    if not isinstance(raw, list):
        return copy.deepcopy(default)
    out: List[Dict[str, str]] = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "label": str(item.get("label") or code).strip() or code})
    return out or copy.deepcopy(default)


def normalize_options(policy_data: Any) -> Dict[str, Any]:
    """양식 버전의 선택 항목을 **빠짐없는 모양**으로.

    ``blocks._option_on`` 과 같은 답을 낸다(프론트도 이것을 따른다):

    * ``options`` 가 dict 가 아니면(없음 포함) 전부 켠 한 벌(:data:`FULL_OPTIONS`) —
      옛 양식은 모두 AI 위험도였다.
    * 빠진 하위 키는 전부 켠 한 벌의 값을 따른다. ``{"enabled": …}`` 에서 ``enabled`` 가
      빠져도 켜진 것이다. 키가 있으면 그 값의 참거짓을 따른다(``null`` 은 꺼짐).
    * ``impact_scope`` / ``traits`` 는 ``true``/``false`` 로 와도 ``{enabled, choices}`` 로 편다.
      선택지가 비었거나 모양이 틀리면 기본 선택지다 — 켜 놓고 고를 것이 없으면 아무도 그
      칸을 채울 수 없다.
    """
    raw = policy_data.get("options") if isinstance(policy_data, dict) else None
    if not isinstance(raw, dict):
        return copy.deepcopy(FULL_OPTIONS)
    out: Dict[str, Any] = {}
    for key in FLAG_OPTIONS:
        if key not in raw:
            out[key] = FULL_OPTIONS[key]
            continue
        value = raw[key]
        out[key] = bool(value.get("enabled", True)) if isinstance(value, dict) else bool(value)
    for key in CHOICE_OPTIONS:
        default = FULL_OPTIONS[key]
        if key not in raw:
            out[key] = copy.deepcopy(default)
            continue
        value = raw[key]
        if isinstance(value, dict):
            out[key] = {
                "enabled": bool(value.get("enabled", True)),
                "choices": _choices(value.get("choices"), default["choices"]),
            }
        else:
            out[key] = {"enabled": bool(value), "choices": copy.deepcopy(default["choices"])}
    return out


# ───────── 칸 하나가 따르는 선택 항목 ─────────

def _truthy(value: Any) -> bool:
    """DB 가 돌려준 참거짓. 드라이버에 따라 ``True`` / ``1`` / ``'t'`` 로 온다."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "t", "true", "y", "yes")
    return bool(value)


def _parse_policy_data(raw: Any) -> Dict[str, Any]:
    """저장된 ``policy_data`` → dict. 깨졌거나 비었으면 빈 dict(= 전부 켠 한 벌)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _config_template_id(block: Dict[str, Any]) -> str:
    """칸 설정이 **고정한** 양식. 비어 있으면 ``""`` — 양식 편집기의 "기본 양식 사용" 이다."""
    config = blocks_mod.parse_data((block or {}).get("config"))
    return _text(config.get("template_id") if isinstance(config, dict) else None)


def _resolve_template_id(app_db, requested: str) -> str:
    """요청한 양식이 있으면 그것, 아니면 기본 양식(is_default), 없으면 기본 제공.

    core ``resolve_template_id`` / ``default_template_id`` 와 같은 규칙이다.
    """
    templates = _q(app_db, "SELECT template_id, is_default FROM risk_assessment_templates ORDER BY id")
    if requested and any(str(t.get("template_id") or "") == requested for t in templates):
        return requested
    for t in templates:
        if _truthy(t.get("is_default")):
            return str(t.get("template_id") or "") or BUILTIN_TEMPLATE_ID
    return BUILTIN_TEMPLATE_ID


def _policy_row(app_db, policy_id: Any) -> Optional[Dict[str, Any]]:
    """버전 행(row id). 숫자가 아니거나 그런 행이 없으면 ``None``."""
    if isinstance(policy_id, bool):
        return None
    pid = str(policy_id).strip()
    if not pid.isdigit():
        return None
    rows = _q(app_db, """
        SELECT id, template_id, version, policy_data, is_active
          FROM risk_assessment_policies WHERE id = %s
    """, (int(pid),))
    return rows[0] if rows else None


def _version_number(row: Dict[str, Any]) -> int:
    try:
        return int(row.get("version") or 0)
    except (TypeError, ValueError):
        return 0


def _active_policy_row(app_db, template_id: str) -> Optional[Dict[str, Any]]:
    """양식의 활성 버전. 여럿이면 버전이 가장 큰 것, 하나도 없으면 ``None``."""
    rows = _q(app_db, """
        SELECT id, template_id, version, policy_data, is_active
          FROM risk_assessment_policies WHERE template_id = %s
    """, (template_id,))
    active = [r for r in rows if _truthy(r.get("is_active"))]
    if not active:
        return None
    return max(active, key=_version_number)


def _has_policy_id(policy_id: Any) -> bool:
    return policy_id is not None and str(policy_id).strip() != ""


def resolve_options(app_db, block: Dict[str, Any], data: Any) -> Dict[str, Any]:
    """평가지 칸에 들어오는 값이 **따라야 할** 선택 항목 — 보낸 ``options`` 는 보지 않는다.

    칸이 양식을 **고정했는가**(설정 ``template_id`` 가 비어 있지 않은가)에 따라 다르다.

    고정한 칸
      * 칸의 양식은 설정의 양식(있는 양식일 때), 아니면 기본 양식, 없으면 기본 제공 ``ai-risk``.
      * 값에 ``policy_id`` 가 있으면 그 버전 행의 선택 항목이다. 그런 버전이 없으면
        :class:`EvaluationVersionNotFound`, 칸의 양식이 아닌 버전이면
        :class:`EvaluationVersionMismatch` (둘 다 :class:`ApprovalError` — core 가 400 으로 낸다).
      * ``policy_id`` 가 없으면 칸 양식의 활성 버전(여럿이면 가장 큰 버전) 것, 활성 버전이
        없으면 전부 켠 한 벌.

    고정하지 않은 칸("기본 양식 사용") — 평가자가 평가지에서 다른 양식을 고를 수 있다 (2.3.1)
      * 값에 ``policy_id`` 가 있으면 그 버전 행의 선택 항목이다. 그런 버전이 없으면
        :class:`EvaluationVersionNotFound`. 어느 양식의 버전이든 받는다.
      * ``policy_id`` 가 없으면 값의 ``template_id`` 양식(있는 양식일 때), 아니면 기본 양식,
        없으면 ``ai-risk`` 의 활성 버전 것, 활성 버전이 없으면 전부 켠 한 벌.

    어느 쪽이든 **읽기 자체가 실패**하면(표 없음 · DB 오류) 전부 켠 한 벌을 돌려주고 경고만
    남긴다. 가장 엄격한 한 벌이라 아무것도 건너뛸 수 없고, 읽기 오류로 결재를 막지도 않는다.
    위의 두 오류는 읽기가 성공했을 때만 난다.

    돌려주는 dict 는 새로 만든 것이라 부르는 쪽이 고쳐도 된다.
    """
    value = data if isinstance(data, dict) else {}
    policy_id = value.get("policy_id")
    wants_version = _has_policy_id(policy_id)
    fixed = _config_template_id(block)
    template_id = None
    try:
        if wants_version:
            row = _policy_row(app_db, policy_id)
            if fixed and row is not None:
                template_id = _resolve_template_id(app_db, fixed)
        else:
            template_id = _resolve_template_id(app_db, fixed or _text(value.get("template_id")))
            row = _active_policy_row(app_db, template_id)
    except Exception as exc:  # noqa: BLE001 — 읽지 못하면 가장 엄격한 한 벌
        logger.warning("평가 양식을 읽지 못해 선택 항목을 전부 켠 한 벌로 본다: %s", exc)
        return copy.deepcopy(FULL_OPTIONS)
    if wants_version:
        if row is None:
            raise EvaluationVersionNotFound()
        if template_id is not None and (_text(row.get("template_id")) or LEGACY_TEMPLATE_ID) != template_id:
            raise EvaluationVersionMismatch()
    if row is None:
        return copy.deepcopy(FULL_OPTIONS)
    return normalize_options(_parse_policy_data(row.get("policy_data")))
