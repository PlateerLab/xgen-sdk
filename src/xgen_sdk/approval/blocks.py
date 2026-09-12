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


def _has_assessment(data: Any) -> bool:
    """위험도 평가는 **등급이 정해져야** 채운 것이다.

    항목 점수만 있고 등급이 없으면 읽는 사람은 결론을 모른다 — 결재는 결론을
    보고 결정하는 자리다.
    """
    if not isinstance(data, dict):
        return False
    return bool(str(data.get("risk_level") or "").strip())


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
        description="평가 템플릿을 채우고 등급을 정한다.",
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


def unfilled_required(blocks: Any, step_order: int) -> List[Dict[str, Any]]:
    """그 단계에서 **아직 안 채운 필수 칸**들."""
    out: List[Dict[str, Any]] = []
    for b in blocks or []:
        if int(b.get("step_order", -1)) != int(step_order):
            continue
        if not b.get("required"):
            continue
        if not is_filled(b):
            out.append(b)
    return out
