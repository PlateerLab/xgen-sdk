"""XGEN 이 기본으로 제공하는 **결재 양식 템플릿**.

왜 데이터인가
-------------
템플릿은 "이런 절차가 흔하다" 는 제안이지 규칙이 아니다. 사용자는 이것을
복사해 자기 조직에 맞게 고친다 — 4차·5차를 더하거나, 기획서를 빼거나,
위험도 평가를 빼거나, 전부 빼고 기획서 하나만 남겨 2차에서 끝내거나.

그래서 템플릿은 **코드가 아니라 데이터**다. 새 템플릿을 더하는 일이 이 목록에
한 항목을 적는 일이어야, 늘리는 것이 부담이 되지 않는다.

심는 쪽(core)은 :data:`BUILTIN_TEMPLATES` 를 그대로 읽어 만든다. 이미 같은
이름이 있으면 건드리지 않는다 — 심기는 **한 번**이고, 그 뒤로는 사용자 것이다.
"""

from __future__ import annotations

from typing import Any, Dict, List

from xgen_sdk.approval import blocks as B

#: 내장 템플릿. 각 항목은 :func:`xgen_sdk.approval.store.create_form` 의 인자 모양이다.
#:
#: ``steps`` 의 ``step_index`` 0 은 **기안(1차)**, 1 이 2차, 2 가 3차다.
#: 화면은 ``step_index + 1`` 을 "N차" 로 보여 준다.
#:
#: ``description`` 과 ``notice`` 는 **듣는 사람이 다르다**: description 은 양식을
#: 고르는 관리자에게, notice 는 그 양식으로 결재를 올리고 받는 사람에게 하는
#: 말이다. notice 는 결재 문서 아래쪽에 그대로 실린다 — 사내 품의서의 "■ …"
#: 유의사항 자리다.
BUILTIN_TEMPLATES: List[Dict[str, Any]] = [
    {
        "name": "AI Agent 배포 결재",
        "description": (
            "기안자가 Agent 기획서를 붙이고, 2차가 그것을 보고 AI 위험도를 평가하고, "
            "3차가 최종 결정한다. 복사해서 단계를 더하거나 뺄 수 있다."
        ),
        "notice": (
            "■ [Agent 기획서] 는 [마이페이지 > Agent 개발 기획서] 에서 미리 작성해 주세요.\n"
            "■ 위험도 평가는 2차 결재자가 수행합니다 — 등급이 정해져야 3차로 넘어갑니다.\n"
            "■ 승인이 나기 전까지 배포는 열리지 않습니다. 반려되면 사유를 확인하고 "
            "기획서를 고쳐 다시 올려 주세요.\n"
            "■ 누군가 승인한 뒤에는 기안 칸을 고칠 수 없습니다 — 바꿔야 하면 반려받고 "
            "다시 올려 주세요."
        ),
        "steps": [
            {"step_index": 0, "title": "1차 기안",
             "guide": "배포하려는 Agent 의 기획서를 붙입니다. 참고 자료가 있으면 함께 올립니다."},
            {"step_index": 1, "title": "2차 AI 위험도 심사",
             "guide": "올라온 기획서를 보고 AI 위험도를 평가합니다. 등급을 정해야 다음으로 넘어갑니다."},
            {"step_index": 2, "title": "3차 최종 결정",
             "guide": "기획서와 위험도 평가를 보고 승인하거나 반려합니다."},
        ],
        "blocks": [
            {"step_index": 0, "block_type": B.AGENT_DEV_PLAN, "label": "Agent 기획서",
             "required": True, "sort_order": 1},
            {"step_index": 0, "block_type": B.ATTACHMENTS, "label": "참고 자료",
             "required": False, "sort_order": 2},
            {"step_index": 1, "block_type": B.RISK_ASSESSMENT, "label": "AI 위험도 평가",
             "required": True, "sort_order": 1},
        ],
    },
    {
        "name": "기본 결재 (2단계)",
        "description": "기안자가 내용을 적고 한 명이 결정한다. 가장 단순한 형태.",
        "notice": (
            "■ [기안 내용] 에는 무엇을, 왜 하려는지 결재자가 판단할 수 있을 만큼 적어 주세요.\n"
            "■ 승인이 나야 실제로 반영됩니다."
        ),
        "steps": [
            {"step_index": 0, "title": "1차 기안", "guide": "무엇을, 왜 하려는지 적습니다."},
            {"step_index": 1, "title": "2차 결재", "guide": "내용을 보고 승인하거나 반려합니다."},
        ],
        "blocks": [
            {"step_index": 0, "block_type": B.TEXT, "label": "기안 내용",
             "required": True, "sort_order": 1},
        ],
    },
    {
        "name": "문서 검토 결재 (3단계)",
        "description": "기안자가 문서를 붙이고, 2차가 검토 의견을 적고, 3차가 결정한다.",
        "notice": (
            "■ 검토받을 문서는 최종본을 붙여 주세요 — 검토 뒤 문서가 바뀌면 처음부터 "
            "다시 올려야 합니다.\n"
            "■ 2차 결재자는 [검토 의견] 을 적어야 승인할 수 있습니다. 반려는 언제든 "
            "할 수 있습니다."
        ),
        "steps": [
            {"step_index": 0, "title": "1차 기안", "guide": "검토받을 문서를 붙입니다."},
            {"step_index": 1, "title": "2차 검토", "guide": "문서를 읽고 검토 의견을 적습니다."},
            {"step_index": 2, "title": "3차 최종 결정", "guide": "검토 의견을 보고 결정합니다."},
        ],
        "blocks": [
            {"step_index": 0, "block_type": B.ATTACHMENTS, "label": "검토 문서",
             "required": True, "sort_order": 1},
            {"step_index": 0, "block_type": B.TEXT, "label": "기안 사유",
             "required": False, "sort_order": 2},
            {"step_index": 1, "block_type": B.TEXT, "label": "검토 의견",
             "required": True, "sort_order": 1},
        ],
    },
]


def by_name(name: str) -> Dict[str, Any] | None:
    for t in BUILTIN_TEMPLATES:
        if t["name"] == name:
            return t
    return None
