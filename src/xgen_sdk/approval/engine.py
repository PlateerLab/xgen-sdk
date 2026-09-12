"""결재 상태기계 — **DB 를 모른다.**

여기 있는 함수들은 dict 를 받아 dict 를 돌려준다. 저장은 바깥
(:mod:`xgen_sdk.approval.store`)가 한다. 이렇게 가른 이유는 하나다:
결재에서 틀리면 안 되는 것은 전부 *규칙*이고, 규칙은 DB 없이 전부 시험할
수 있어야 한다.

결재선은 **한 줄이다**

    [A] → [B] → [C]

A 가 승인해야 B 에게 가고, B 가 승인해야 C 에게 간다. 한 차례에 한 사람이고,
그 사람이 처리하기 전에는 다음 사람에게 아무것도 가지 않는다. 마지막 사람의
승인이 곧 최종 승인이다.

(한때 "같은 번호는 병렬" 이라는 여지를 뒀다가 걷어냈다 — 요청에 없던 개념이고,
그것 하나 때문에 '누가 먼저 눌렀나' 를 따지는 동시성 문제가 통째로 생겼다.
결재선이 한 줄이면 그 질문 자체가 없다.)

규칙 (이 파일이 지키는 전부)
----------------------------
1. 자기 차례(``pending``)인 단계에서만, 본인만 처리할 수 있다.
2. 한 번 처리한 단계는 다시 처리할 수 없다.
3. 거절 하나로 건 전체가 즉시 종결된다. 차례가 오지 않은 단계는 ``skipped``.
4. 한 사람이 승인하면 **바로 다음 한 사람**에게 넘어간다.
5. 종결된 건(approved/rejected/canceled)은 어떤 결정도 받지 않는다.
6. 회수는 기안자만, 그리고 **아무도 아직 승인하지 않았을 때만** 할 수 있다 —
   남의 승인을 없던 일로 만드는 길을 열지 않는다.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

# ── 상태 어휘 ─────────────────────────────────────────────────────────

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
CANCELED = "canceled"

WAITING = "waiting"
SKIPPED = "skipped"

#: 더 이상 움직이지 않는 건.
TERMINAL = (APPROVED, REJECTED, CANCELED)


class ApprovalError(Exception):
    """규칙 위반. 메시지는 **사람이 읽고 다음에 무엇을 할지 아는** 문장이다."""


class ApprovalLineRequired(ApprovalError):
    """**결재선을 아직 안 골랐다** — 사용자가 고르면 그대로 진행되는 상태.

    다른 ApprovalError 와 구분하는 이유: 이건 실패가 아니라 **한 단계 덜 온
    것**이다. 화면은 이 오류를 받으면 오류창이 아니라 **결재선 모달**을 띄우고,
    사용자가 결재자를 고른 뒤 같은 요청을 다시 보낸다.

    한 문장으로 뭉뚱그리면(그냥 400 "결재선을 지정하세요") 화면은 그것이
    "사용자가 할 수 있는 일" 인지 "관리자에게 문의할 일" 인지 알 수 없다.
    """

    def __init__(self, action_type: str, action_label: str = "",
                 default_line_id: Any = None):
        self.action_type = action_type
        self.action_label = action_label or action_type
        #: 관리자가 [결재 목록 설정] 에 정해 둔 결재선 — 모달을 미리 채운다.
        self.default_line_id = default_line_id
        super().__init__(f"{self.action_label} 은(는) 결재를 거쳐야 합니다 — 결재선을 지정해 주세요")


# ── 결재선 만들기 ─────────────────────────────────────────────────────


def plan_steps(specs: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """결재자 목록 → **한 줄로 선** 단계들.

    차례는 1 부터 빈틈없이, **한 차례에 한 사람**이다. 들어온 ``step_order`` 는
    줄 세우는 순서로만 쓰고 번호 자체는 다시 매긴다 — 사람이 1,5,9 를 넣었다면
    뜻은 "셋을 이 순서대로" 이지 "5,9 번 자리는 비운다" 가 아니다.
    """
    if not specs:
        raise ApprovalError("결재선에 최소 한 명이 필요합니다")

    ordered: List[Tuple[int, int, int]] = []   # (준 순서, 들어온 차례, 결재자)
    seen: set = set()
    for idx, raw in enumerate(specs):
        try:
            approver = int(raw.get("approver_id"))
        except (TypeError, ValueError):
            raise ApprovalError("결재자가 올바르지 않습니다") from None
        if approver in seen:
            # 한 사람이 같은 줄에 두 번 서면 "두 번 승인하라" 가 되는데,
            # 그런 결재선은 실수이지 의도인 적이 없다.
            raise ApprovalError("같은 사람을 결재선에 두 번 넣을 수 없습니다")
        seen.add(approver)
        try:
            given = int(raw.get("step_order", idx + 1))
        except (TypeError, ValueError):
            given = idx + 1
        ordered.append((max(1, given), idx, approver))

    # 같은 번호를 주더라도 **줄은 하나다** — 들어온 순서로 뒤에 세운다.
    ordered.sort(key=lambda x: (x[0], x[1]))
    return [{"approver_id": approver, "step_order": n}
            for n, (_given, _idx, approver) in enumerate(ordered, start=1)]


def open_steps(steps: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """결재를 올리는 순간의 단계 상태 — **첫 사람만** ``pending``."""
    planned = list(steps)
    if not planned:
        raise ApprovalError("결재선에 최소 한 명이 필요합니다")
    first = min(s["step_order"] for s in planned)
    return [
        {**s, "status": PENDING if s["step_order"] == first else WAITING,
         "acted_at": None, "note": None}
        for s in planned
    ]


# ── 결정 ──────────────────────────────────────────────────────────────


def _step_of(steps: Sequence[Dict[str, Any]], actor_id: int) -> Optional[Dict[str, Any]]:
    for s in steps:
        if int(s.get("approver_id") or 0) == int(actor_id):
            return s
    return None


def decide(
    request: Dict[str, Any],
    steps: Sequence[Dict[str, Any]],
    actor_id: int,
    action: str,
    note: str = "",
    now: Any = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """승인/거절을 적용한 뒤 **(새 request, 새 steps)** 를 돌려준다.

    규칙을 어기면 :class:`ApprovalError` 를 던진다 — 조용히 무시하면 화면은
    처리된 줄 알고 넘어가고, 결재함에는 그대로 남는다.
    """
    if action not in (APPROVED, REJECTED):
        raise ApprovalError("승인 또는 거절만 할 수 있습니다")

    status = str(request.get("status") or PENDING)
    if status in TERMINAL:
        raise ApprovalError(_terminal_reason(status))

    mine = _step_of(steps, actor_id)
    if mine is None:
        raise ApprovalError("이 결재의 결재자가 아닙니다")
    if mine["status"] in (APPROVED, REJECTED):
        raise ApprovalError("이미 처리한 결재입니다")
    if mine["status"] != PENDING:
        raise ApprovalError("아직 차례가 아닙니다")

    out_steps = [dict(s) for s in steps]
    target = _step_of(out_steps, actor_id)
    assert target is not None
    target["status"] = action
    target["acted_at"] = now
    target["note"] = (note or "")[:500] or None

    out_req = dict(request)

    if action == REJECTED:
        # 거절 하나로 끝난다. 차례가 오지 않은 사람들은 **처리하지 않은 것**이
        # 아니라 **차례가 오지 않은 것**이다 — 그 구분을 기록에 남긴다.
        for s in out_steps:
            if s["status"] in (WAITING, PENDING):
                s["status"] = SKIPPED
        out_req["status"] = REJECTED
        out_req["decided_at"] = now
        return out_req, out_steps

    current = int(request.get("current_step_order") or 1)
    later = sorted({s["step_order"] for s in out_steps if s["step_order"] > current})
    if not later:
        out_req["status"] = APPROVED
        out_req["decided_at"] = now
        return out_req, out_steps

    # 바로 다음 한 사람에게 넘어간다.
    nxt = later[0]
    out_req["current_step_order"] = nxt
    for s in out_steps:
        if s["step_order"] == nxt:
            s["status"] = PENDING
    return out_req, out_steps


def advance(
    steps: Sequence[Dict[str, Any]],
    current_order: int,
    now: Any = None,
) -> Tuple[str, int, Any]:
    """**이미 적힌 단계들**만 보고 건의 상태를 다시 계산한다.

    :func:`decide` 와 무엇이 다른가: 저쪽은 "이 사람이 지금 누를 수 있는가" 를
    판정하고 그 한 칸을 채운다. 이쪽은 **누가 눌렀는지 모른 채** 이미 저장된
    단계들만 보고 "그래서 이 건은 지금 어떤 상태인가" 를 답한다.

    왜 따로 두나: 같은 사람이 두 번 누르는 경우(더블클릭·재전송·네트워크 재시도)
    를 조건부 쓰기로 막은 뒤, 원장을 **다시 읽어** 여기로 판정하기 위해서다.
    그래야 이긴 요청 하나만 다음 사람에게 넘기고 적용·알림을 한 번만 한다.

    반환: ``(status, next_order, decided_at)``
    """
    if any(s.get("status") == REJECTED for s in steps):
        return REJECTED, current_order, now

    here = [s for s in steps if s.get("step_order") == current_order]
    if not here or any(s.get("status") != APPROVED for s in here):
        return PENDING, current_order, None          # 이 사람이 아직 안 눌렀다

    later = sorted({s["step_order"] for s in steps if s["step_order"] > current_order})
    if not later:
        return APPROVED, current_order, now          # 마지막 사람이 눌렀다 = 최종 승인
    return PENDING, later[0], None


def cancel(
    request: Dict[str, Any],
    steps: Sequence[Dict[str, Any]],
    actor_id: int,
    now: Any = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """기안자의 회수.

    **아무도 승인하지 않았을 때만** 된다. 한 명이라도 승인한 뒤에 회수할 수
    있으면, 기안자가 남의 승인을 없던 일로 만들 수 있다. 그 뒤에 무슨 일이
    있었는지는 아무도 증명하지 못한다.
    """
    if int(request.get("requester_id") or 0) != int(actor_id):
        raise ApprovalError("기안자만 회수할 수 있습니다")
    status = str(request.get("status") or PENDING)
    if status in TERMINAL:
        raise ApprovalError(_terminal_reason(status))
    if any(s.get("status") == APPROVED for s in steps):
        raise ApprovalError("이미 승인한 결재자가 있어 회수할 수 없습니다")

    out_steps = [dict(s) for s in steps]
    for s in out_steps:
        if s["status"] in (WAITING, PENDING):
            s["status"] = SKIPPED
    out_req = dict(request)
    out_req["status"] = CANCELED
    out_req["decided_at"] = now
    return out_req, out_steps


def _terminal_reason(status: str) -> str:
    return {
        APPROVED: "이미 승인 완료된 결재입니다",
        REJECTED: "이미 거절된 결재입니다",
        CANCELED: "기안자가 회수한 결재입니다",
    }.get(status, "이미 종결된 결재입니다")
