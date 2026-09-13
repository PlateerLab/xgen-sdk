"""결재 양식의 **칸 종류** — 무엇을 채우는가.

왜 레지스트리인가
-----------------
칸을 자유 JSON 스키마로 두면 관리자가 무엇이든 정의할 수 있지만, **화면이
그것을 그릴 수 없다.** 그리지 못하는 칸은 아무도 못 채우고, 못 채우는 칸이
필수면 결재가 통째로 멈춘다.

그래서 종류는 코드로 더한다. 대신 종류마다 설정(``config``)을 열어 둬서,
같은 종류를 다르게 쓰는 것은 관리자가 정한다 — 첨부는 확장자와 개수를,
위험도 평가는 어느 평가 템플릿을 쓸지를.

채움 판정
---------
"채워졌는가" 는 **종류가 안다.** 빈 문자열도 채운 것으로 볼지, 파일이 한 개는
있어야 하는지는 종류마다 다르다. 여기서 한 자리로 정해 두면 서버와 화면이
같은 답을 쓴다 — 두 곳이 다르게 판정하면 화면은 채웠다는데 서버가 거절한다.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

#: 자유 서술. 결재 내용 칸을 양식 안에서 다시 쓸 때.
TEXT = "text"
#: 파일 첨부. ``attachments`` 표의 ``parent_type='approval_block'``.
ATTACHMENTS = "attachments"
#: Agent 기획서 — 고르거나 새로 쓴다.
AGENT_DEV_PLAN = "agent_dev_plan"
#: AI 위험도 평가 — 평가 템플릿 한 벌을 채운다.
RISK_ASSESSMENT = "risk_assessment"


class BlockSpec:
    """한 종류가 지켜야 할 계약."""

    def __init__(
        self,
        block_type: str,
        label: str,
        *,
        is_filled: Callable[[Any], bool],
        description: str = "",
        default_config: Optional[Dict[str, Any]] = None,
        actor: str = "any",
    ) -> None:
        self.block_type = block_type
        self.label = label
        self.description = description
        self.is_filled = is_filled
        self.default_config = default_config or {}
        #: 누가 채우는 것이 자연스러운가 — ``drafter`` / ``approver`` / ``any``.
        #: 관리 화면이 칸을 엉뚱한 단계에 놓지 않도록 돕는 **안내**이지,
        #: 서버가 막는 규칙이 아니다. 조직마다 절차가 다르다.
        self.actor = actor


def _nonempty_text(data: Any) -> bool:
    if isinstance(data, dict):
        data = data.get("text")
    return bool(str(data or "").strip())


def _has_files(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    files = data.get("attachment_ids")
    return isinstance(files, list) and len(files) > 0


def _has_plan(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    return data.get("plan_id") is not None


def _is_score(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def assessment_gaps(data: Any) -> List[str]:
    """위험도 평가에서 **아직 채우지 않은 것** — 비어 있으면 다 채운 평가다.

    거버넌스의 [AI 위험도 평가] 가 저장 직전에 막던 것과 **같은 기준**이다:
    평가 템플릿의 모든 항목에 사전 위험 점수, 영향 범위, 판단 근거. 여기에
    점수로 정해진 등급이 더해진다. 평가가 결재로 옮겨 오면서 기준이 "등급만
    고르면 된다" 로 느슨해지면, 같은 원장에 서로 다른 무게의 평가가 섞인다 —
    대시보드는 둘을 구별하지 못한다.

    값의 모양(``item_scores``)은 거버넌스가 원장에 쓰던 ``policy_data`` 그대로다::

        {"categories": [{"name", "weight", "items": [{"id", "name", "score", "risk_mitigation"}]}],
         "risk_traits": [...]}
    """
    if not isinstance(data, dict):
        return ["평가"]
    gaps: List[str] = []
    snapshot = data.get("item_scores")
    categories = snapshot.get("categories") if isinstance(snapshot, dict) else None
    items = [
        it
        for cat in (categories if isinstance(categories, list) else [])
        if isinstance(cat, dict)
        for it in (cat.get("items") if isinstance(cat.get("items"), list) else [])
        if isinstance(it, dict)
    ]
    if not items:
        gaps.append("항목 점수")
    else:
        empty = sum(1 for it in items if not _is_score(it.get("score")))
        if empty:
            gaps.append(f"항목 점수 {empty}개")
    if not str(data.get("impact_scope") or "").strip():
        gaps.append("영향 범위")
    if not str(data.get("rationale") or "").strip():
        gaps.append("판단 근거")
    if not str(data.get("risk_level") or "").strip():
        gaps.append("위험 등급")
    return gaps


def _has_assessment(data: Any) -> bool:
    """위험도 평가는 **거버넌스 기준을 다 채워야** 채운 것이다 — :func:`assessment_gaps`."""
    return isinstance(data, dict) and not assessment_gaps(data)


REGISTRY: Dict[str, BlockSpec] = {
    TEXT: BlockSpec(
        TEXT, "서술",
        description="자유롭게 적는 칸.",
        is_filled=_nonempty_text,
        default_config={"placeholder": "", "max_length": 4000},
    ),
    ATTACHMENTS: BlockSpec(
        ATTACHMENTS, "파일 첨부",
        description="파일을 붙인다.",
        is_filled=_has_files,
        default_config={"extensions": [], "max_count": 10, "max_bytes": 52428800},
    ),
    AGENT_DEV_PLAN: BlockSpec(
        AGENT_DEV_PLAN, "Agent 기획서",
        description="Agent 개발 기획서를 고르거나 새로 쓴다.",
        is_filled=_has_plan,
        default_config={"allow_create": True},
        actor="drafter",
    ),
    RISK_ASSESSMENT: BlockSpec(
        RISK_ASSESSMENT, "AI 위험도 평가",
        description=(
            "거버넌스 [AI 위험도 평가] 와 같은 평가 — 항목별 사전 위험·위험 경감, "
            "영향 범위, 위험 특성, 판단 근거. 등급은 점수로 정해진다."
        ),
        is_filled=_has_assessment,
        default_config={"template_id": None},
        actor="approver",
    ),
}


def known_types() -> List[str]:
    return list(REGISTRY.keys())


def spec(block_type: str) -> Optional[BlockSpec]:
    return REGISTRY.get(str(block_type or ""))


def is_known(block_type: str) -> bool:
    return str(block_type or "") in REGISTRY


#: 칸 종류별 **적용 함수** — 최종 승인이 났을 때 그 칸의 값으로 무엇을 할 것인가.
#:
#: 왜 레지스트리인가: 값이 갈 곳을 아는 것은 그 표를 가진 서비스다. AI 위험도
#: 평가는 ``governance_risk_assessments`` 로 가야 하는데 그 표는 core 의 것이고,
#: SDK 는 그 표의 모양을 모른다. 그래서 SDK 는 **부를 자리**만 정해 두고, 무엇을
#: 할지는 표를 가진 쪽이 기동할 때 꽂는다(``registry.register_action`` 과 같은
#: 방식이다).
_APPLIERS: Dict[str, Callable[..., None]] = {}


def register_applier(block_type: str, fn: Callable[..., None]) -> None:
    """``fn(app_db, request, block, data)`` — 최종 승인 시 이 종류의 칸마다 불린다.

    두 번 불릴 수 있다고 보고 **멱등하게** 써라. 뒤처리는 한 건에 한 번이
    원칙이지만, 워커 둘이 같은 찰나에 집으면 훅이 두 번 돌 수 있다.
    """
    _APPLIERS[str(block_type)] = fn


def has_applier(block_type: str) -> bool:
    return str(block_type or "") in _APPLIERS


def applier(block_type: str) -> Optional[Callable[..., None]]:
    return _APPLIERS.get(str(block_type or ""))


def catalog() -> List[Dict[str, Any]]:
    """화면이 [칸 추가] 목록을 그릴 재료.

    레지스트리를 그대로 내보내는 이유는 관리 화면이 **코드가 아는 것만**
    보여 주게 하기 위해서다. 화면에 따로 적어 두면 SDK 가 종류를 더할 때
    화면이 모르고, 뺄 때는 그릴 수 없는 칸을 권한다.
    """
    return [{
        "block_type": s.block_type,
        "label": s.label,
        "description": s.description,
        "default_config": dict(s.default_config),
        "actor": s.actor,
    } for s in REGISTRY.values()]


def parse_data(raw: Any) -> Any:
    """저장된 값을 파이썬으로. 깨진 JSON 은 **빈 값**으로 본다.

    깨진 값을 예외로 올리면 결재 한 건이 목록 전체를 못 읽게 만든다.
    """
    if raw in (None, ""):
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def is_filled(block: Dict[str, Any]) -> bool:
    """이 칸이 채워졌는가 — 종류가 정한 규칙으로."""
    s = spec(block.get("block_type"))
    if s is None:
        # 모르는 종류는 **막지 않는다.** 옛 결재가 새 코드에서 영영 멈추면
        # 그 결재는 아무도 끝낼 수 없다.
        return True
    return bool(s.is_filled(parse_data(block.get("data"))))


def validate_data(block: Dict[str, Any], data: Any) -> None:
    """칸에 들어온 값이 **그 칸이 내건 조건**을 지키는지 본다. 어기면 던진다.

    왜 필요한가
    -----------
    양식이 "첨부는 3개까지" 라고 적어 뒀는데 아무도 그것을 보지 않으면, 그 설정은
    적어 둔 사람에게 거짓말을 한다 — 화면은 제한이 걸린 줄 알고, 실제로는 무엇이든
    들어간다. 지키지 않을 값이면 애초에 적게 두지 말아야 하고, 적게 뒀으면 지켜야
    한다.

    파일 확장자는 **올리는 문**이 이미 한 번 본다(그쪽이 진짜 관문이다). 여기서
    보는 것은 그 위에 양식이 더 좁힌 조건이라, 값에 실린 이름으로 판단한다.
    """
    cfg = parse_data(block.get("config")) or {}
    if not isinstance(cfg, dict):
        return
    if str(block.get("block_type") or "") != ATTACHMENTS:
        return
    if not isinstance(data, dict):
        return

    ids = data.get("attachment_ids")
    ids = ids if isinstance(ids, list) else []
    max_count = cfg.get("max_count")
    try:
        max_count = int(max_count) if max_count is not None else None
    except (TypeError, ValueError):
        max_count = None
    if max_count is not None and max_count > 0 and len(ids) > max_count:
        raise ValueError(
            f"{block.get('label') or '첨부'} 는 {max_count}개까지 붙일 수 있습니다 (지금 {len(ids)}개)")

    exts = cfg.get("extensions")
    exts = [str(e).lower().lstrip(".") for e in exts] if isinstance(exts, list) else []
    if not exts:
        return
    files = data.get("files")
    files = files if isinstance(files, list) else []
    bad = []
    for f in files:
        if not isinstance(f, dict):
            continue
        name = str(f.get("original_name") or f.get("file_name") or "")
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in exts:
            bad.append(name or "이름 없는 파일")
    if bad:
        raise ValueError(
            f"{block.get('label') or '첨부'} 에 허용되지 않은 파일이 있습니다: "
            f"{', '.join(bad[:3])} (허용: {', '.join(exts)})")


def unfilled_required(blocks: Any, step_order: int) -> List[Dict[str, Any]]:
    """그 단계에서 **아직 안 채운 필수 칸**들.

    화면이 "지금 내가 뭘 더 써야 하나" 를 물을 때 쓴다 — 내 칸만 본다.
    """
    return _unfilled(blocks, lambda s: s == int(step_order))


def unfilled_upto(blocks: Any, step_order: int) -> List[Dict[str, Any]]:
    """그 단계 **까지**(0..step_order) 안 채운 필수 칸들 — 승인 직전의 판정.

    왜 제 단계만 보지 않는가
    ------------------------
    기안(0단계)의 칸은 **아무도 승인하지 않는다.** 기안자는 상신을 할 뿐이라
    승인 판정을 거치지 않으므로, 제 단계만 보는 규칙에서는 기획서가 비어 있어도
    2차가 승인해 버린다 — "기획서를 보고 위험도를 평가한다" 는 절차가 통째로
    빈다.

    앞 단계까지 함께 보는 것은 공짜다. 이미 승인한 단계는 그때 이 판정을
    통과했으므로 여기서 다시 걸릴 일이 없고, 걸린다면 그건 누군가 승인 뒤에
    칸을 비웠다는 뜻이라 어차피 막아야 한다.
    """
    return _unfilled(blocks, lambda s: s <= int(step_order))


def _unfilled(blocks: Any, want) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for b in blocks or []:
        try:
            step = int(b.get("step_order", -1))
        except (TypeError, ValueError):
            continue
        if not want(step):
            continue
        if not b.get("required"):
            continue
        if not is_filled(b):
            out.append(b)
    return out
