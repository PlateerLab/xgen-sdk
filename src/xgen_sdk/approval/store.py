"""결재 원장의 DB 면 — 규칙은 :mod:`xgen_sdk.approval.engine` 이 갖고,
여기는 **읽고 쓰기만** 한다.

이 파일에 조건문이 늘어나기 시작하면 규칙이 두 곳에 살게 된 것이다.
그때는 여기가 아니라 engine 으로 옮겨야 한다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from xgen_sdk.approval import engine as E
from xgen_sdk.approval import notifier
from xgen_sdk.approval.registry import GENERIC, is_registered, run_apply
from xgen_sdk.approval.sql import q as _q, rows as _rows  # noqa: F401 — 기존 이름 보존

logger = logging.getLogger("approval-store")


def _now():
    return datetime.now(timezone.utc)


def _notify_turn(app_db, req: Dict[str, Any]) -> None:
    """지금 차례인 사람들에게 알린다 — 단계 상태를 그대로 읽는다.

    "몇 번째 차례인가" 를 여기서 다시 계산하지 않는다. engine 이 이미 정해
    ``pending`` 으로 적어 둔 것을 읽기만 하면, 알림 대상과 결재함 목록이
    **같은 근거**를 쓰게 된다(어긋나면 알림은 갔는데 결재함에는 없는 일이 생긴다).
    """
    try:
        ids = [int(s["approver_id"]) for s in req.get("steps") or []
               if s.get("status") == E.PENDING]
        notifier.notify_turn(app_db, req, ids)
    except Exception as exc:  # noqa: BLE001 — 알림 실패가 결재를 막지 않는다
        logger.warning("결재 차례 알림 실패(결재는 그대로): %s", exc)




# ── 결재선 템플릿 ─────────────────────────────────────────────────────


def list_lines(app_db, user_id: int, include_private: bool = True) -> List[Dict[str, Any]]:
    """쓸 수 있는 결재선 — 공용 + 내가 만든 것."""
    sql = """
        SELECT l.id, l.name, l.description, l.owner_id, l.is_shared, l.is_active
          FROM approval_lines l
         WHERE l.is_active = TRUE
           AND (l.is_shared = TRUE %s)
         ORDER BY l.is_shared DESC, l.name
    """ % ("OR l.owner_id = %s" if include_private else "")
    lines = _q(app_db, sql, (user_id,) if include_private else ())
    for ln in lines:
        ln["steps"] = _q(
            app_db,
            """
            SELECT s.step_order, s.approver_id, u.username, u.full_name
              FROM approval_line_steps s
              LEFT JOIN users u ON u.id = s.approver_id
             WHERE s.line_id = %s
             ORDER BY s.step_order, s.id
            """,
            (ln["id"],),
        )
    return lines


def create_line(app_db, *, name: str, description: str, owner_id: int,
                is_shared: bool, steps: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    planned = E.plan_steps(steps)          # 규칙 검증은 engine 이 한다
    rows = _q(
        app_db,
        """
        INSERT INTO approval_lines (name, description, owner_id, is_shared, is_active)
        VALUES (%s, %s, %s, %s, TRUE) RETURNING id
        """,
        (name.strip()[:100], (description or "").strip()[:500] or None, owner_id, bool(is_shared)),
    )
    line_id = rows[0]["id"]
    for s in planned:
        _q(app_db,
           "INSERT INTO approval_line_steps (line_id, step_order, approver_id) VALUES (%s, %s, %s)",
           (line_id, s["step_order"], s["approver_id"]))
    return {"id": line_id, "name": name, "steps": planned}


def delete_line(app_db, line_id: int, actor_id: int, is_superuser: bool) -> None:
    """**지우지 않고 내린다.** 이 줄을 쓴 결재 기록의 line_id 가 가리킬 곳이
    사라지면, 나중에 "어느 결재선으로 올렸는가" 를 답할 수 없다."""
    owner = _q(app_db, "SELECT owner_id FROM approval_lines WHERE id = %s", (line_id,))
    if not owner:
        raise E.ApprovalError("결재선을 찾을 수 없습니다")
    if not is_superuser and int(owner[0]["owner_id"] or 0) != int(actor_id):
        raise E.ApprovalError("내가 만든 결재선만 지울 수 있습니다")
    _q(app_db, "UPDATE approval_lines SET is_active = FALSE WHERE id = %s", (line_id,))


# ── 결재 문서 ─────────────────────────────────────────────────────────


def submit(app_db, *, requester_id: int, title: str, reason: str = "",
           action_type: str = GENERIC, payload: Optional[Dict[str, Any]] = None,
           line_id: Optional[int] = None,
           steps: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """결재를 올린다. 템플릿(``line_id``) 또는 직접 지정(``steps``) 중 하나.

    템플릿을 썼더라도 단계는 **복사**한다 — 나중에 그 템플릿이 바뀌어도 이미
    올라간 결재의 결재선은 그대로여야 한다.
    """
    if not str(title or "").strip():
        raise E.ApprovalError("제목이 필요합니다")
    if not is_registered(action_type):
        raise E.ApprovalError(f"등록되지 않은 결재 종류입니다: {action_type}")

    if steps:
        specs = list(steps)
    elif line_id:
        specs = _q(app_db,
                   "SELECT step_order, approver_id FROM approval_line_steps WHERE line_id = %s",
                   (line_id,))
        if not specs:
            raise E.ApprovalError("결재선이 비어 있습니다")
    else:
        raise E.ApprovalError("결재선을 지정하세요")

    planned = E.open_steps(E.plan_steps(specs))
    if any(int(s["approver_id"]) == int(requester_id) for s in planned):
        # 자기 결재를 자기가 승인하면 결재가 아니다.
        raise E.ApprovalError("자기 자신을 결재선에 넣을 수 없습니다")

    rows = _q(
        app_db,
        """
        INSERT INTO approval_requests
            (title, reason, action_type, payload, requester_id, line_id,
             status, current_step_order)
        VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s) RETURNING id
        """,
        (str(title).strip()[:200], (reason or "").strip() or None, action_type,
         json.dumps(payload or {}, ensure_ascii=False), requester_id, line_id,
         min(s["step_order"] for s in planned)),
    )
    request_id = rows[0]["id"]
    for s in planned:
        _q(app_db,
           """INSERT INTO approval_request_steps
                  (request_id, step_order, approver_id, status)
              VALUES (%s, %s, %s, %s)""",
           (request_id, s["step_order"], s["approver_id"], s["status"]))
    logger.info("결재 상신 #%s '%s' (기안 %s, 단계 %s)", request_id, title, requester_id, len(planned))
    out = get(app_db, request_id)
    # 첫 차례에게 알린다. 결재함을 주기적으로 열어 보는 사람은 없다 — 알림이
    # 없으면 결재는 "아무도 반대해서" 가 아니라 "아무도 몰라서" 멈춘다.
    _notify_turn(app_db, out)
    return out


def get(app_db, request_id: int) -> Dict[str, Any]:
    rows = _q(
        app_db,
        """
        SELECT r.id, r.title, r.reason, r.action_type, r.payload, r.requester_id,
               r.line_id, r.status, r.current_step_order, r.decided_at,
               r.applied_at, r.apply_error, r.created_at,
               u.username AS requester_username, u.full_name AS requester_name
          FROM approval_requests r
          LEFT JOIN users u ON u.id = r.requester_id
         WHERE r.id = %s
        """,
        (request_id,),
    )
    if not rows:
        raise E.ApprovalError("결재를 찾을 수 없습니다")
    req = rows[0]
    try:
        req["payload"] = json.loads(req.get("payload") or "{}")
    except (TypeError, ValueError):
        req["payload"] = {}
    req["steps"] = _q(
        app_db,
        """
        SELECT s.step_order, s.approver_id, s.status, s.acted_at, s.note,
               u.username, u.full_name
          FROM approval_request_steps s
          LEFT JOIN users u ON u.id = s.approver_id
         WHERE s.request_id = %s
         ORDER BY s.step_order, s.id
        """,
        (request_id,),
    )
    return req


def decide(app_db, request_id: int, actor_id: int, action: str, note: str = "") -> Dict[str, Any]:
    """승인/거절.

    쓰기가 **조건부**인 이유 (동시성)
    ---------------------------------
    예전에는 읽고·계산하고·그대로 덮어썼다. 같은 차례에 두 사람이 동시에 누르면
    둘 다 "나 말고 아직 안 누른 사람이 있다" 고 읽고 각자 자기 칸만 적어서,
    **둘 다 승인했는데 건은 영영 pending 에 멈췄다.** 마지막 한 명이 두 번
    누르면(더블클릭·재전송) applier 가 **두 번** 돌았다.

    그래서 두 곳을 compare-and-set 으로 바꿨다:

      1. 내 칸은 ``status='pending'`` 일 때만 적힌다. 0행이면 누가 먼저 처리한
         것이므로 거기서 끝낸다.
      2. 건의 전이는 **원장을 다시 읽어** 계산하고, ``status`` 와
         ``current_step_order`` 가 내가 본 그대로일 때만 적는다. 이긴 쪽만
         적용(applier)과 알림을 한다 — 둘 다 하면 두 번 도는 그 버그다.
    """
    current = get(app_db, request_id)
    now = _now()
    current_order = int(current.get("current_step_order") or 1)

    # (1) 규칙 판정 — 내가 지금 누를 수 있는가. 위반이면 여기서 끝난다.
    E.decide(current, current["steps"], actor_id, action, note, now)

    # (2) 내 칸 — pending 일 때만.
    won_step = _q(
        app_db,
        """UPDATE approval_request_steps
              SET status = %s, acted_at = %s, note = %s
            WHERE request_id = %s AND approver_id = %s AND status = %s
        RETURNING id""",
        (action, now, (note or "")[:500] or None, request_id, actor_id, E.PENDING),
    )
    if not won_step:
        # engine 이 통과시켰는데 행이 안 잡혔다 = 그 찰나에 누가 먼저 눌렀다.
        raise E.ApprovalError("이미 처리한 결재입니다")

    # (3) 원장을 다시 읽어 건의 상태를 계산한다.
    fresh = get(app_db, request_id)
    status, next_order, decided_at = E.advance(fresh["steps"], current_order, now)

    moved = (status != E.PENDING) or (next_order != current_order)
    i_own_it = False
    if moved:
        # (4) 내가 본 상태 그대로일 때만 전이시킨다.
        i_own_it = bool(_q(
            app_db,
            """UPDATE approval_requests
                  SET status = %s, current_step_order = %s, decided_at = %s
                WHERE id = %s AND status = %s AND current_step_order = %s
            RETURNING id""",
            (status, next_order, decided_at, request_id, E.PENDING, current_order),
        ))

    if i_own_it:
        if status == E.REJECTED:
            # 차례가 오지 않은 사람들 — '처리 안 함' 이 아니라 '차례가 안 옴'.
            _q(app_db,
               """UPDATE approval_request_steps SET status = %s
                   WHERE request_id = %s AND status IN (%s, %s)""",
               (E.SKIPPED, request_id, E.WAITING, E.PENDING))
        elif status == E.PENDING:
            _q(app_db,
               """UPDATE approval_request_steps SET status = %s
                   WHERE request_id = %s AND step_order = %s AND status = %s""",
               (E.PENDING, request_id, next_order, E.WAITING))
        elif status == E.APPROVED:
            _apply(app_db, request_id, {"action_type": current.get("action_type")}, current)

    out = get(app_db, request_id)
    # 알림은 결재의 **부산물**이다. 여기서 예외가 새면 사람이 이미 누른 승인이
    # 기록된 뒤에 500 이 나가고, 화면은 실패로 읽는다 — 그래서 통째로 감싼다.
    # 전이를 **이긴 쪽만** 알린다. 둘 다 알리면 같은 알림이 두 번 간다.
    if i_own_it:
        try:
            if status == E.PENDING:
                _notify_turn(app_db, out)          # 다음 차례가 열렸다
            else:
                actor = next((st.get("full_name") or st.get("username") or ""
                              for st in out["steps"]
                              if int(st.get("approver_id") or 0) == int(actor_id)), "")
                notifier.notify_settled(app_db, out, actor_name=actor, note=note)
                if out.get("apply_error"):
                    notifier.notify_apply_failed(app_db, out, str(out["apply_error"]))
        except Exception as exc:  # noqa: BLE001
            logger.warning("결재 #%s 알림 실패(결재는 그대로): %s", request_id, exc)
    return out


def _apply(app_db, request_id: int, req: Dict[str, Any], loaded: Dict[str, Any]) -> None:
    """최종 승인 뒤 실제 동작. **실패해도 승인은 그대로 둔다.**

    사람의 결재는 이미 일어난 사실이다. 적용이 실패했다고 그 사실을 지우면,
    화면은 "승인되지 않음" 이라 말하는데 실제로는 세 사람이 승인한 상태가 된다.
    실패는 숨기지 않고 ``apply_error`` 로 남긴다 — 그래야 누가 다시 시도한다.
    """
    err = run_apply(req.get("action_type") or GENERIC, loaded.get("payload") or {}, loaded)
    _q(app_db,
       "UPDATE approval_requests SET applied_at = %s, apply_error = %s WHERE id = %s",
       (_now(), err or None, request_id))
    if err:
        logger.error("결재 #%s 승인됐으나 적용 실패: %s", request_id, err)


def cancel(app_db, request_id: int, actor_id: int) -> Dict[str, Any]:
    current = get(app_db, request_id)
    now = _now()
    new_req, new_steps = E.cancel(current, current["steps"], actor_id, now)
    for s in new_steps:
        _q(app_db,
           "UPDATE approval_request_steps SET status = %s WHERE request_id = %s AND approver_id = %s",
           (s["status"], request_id, s["approver_id"]))
    _q(app_db,
       "UPDATE approval_requests SET status = %s, decided_at = %s WHERE id = %s",
       (new_req["status"], now, request_id))
    return get(app_db, request_id)


# ── 목록 ──────────────────────────────────────────────────────────────


def inbox(app_db, user_id: int, *, only_pending: bool = True, limit: int = 100) -> List[Dict[str, Any]]:
    """**내게 올라온 결재.** 받은 결재함의 질의는 이 한 줄이 전부다."""
    cond = "AND s.status = 'pending'" if only_pending else ""
    return _q(
        app_db,
        f"""
        SELECT r.id, r.title, r.action_type, r.status, r.created_at,
               r.requester_id, u.username AS requester_username, u.full_name AS requester_name,
               s.status AS my_status, s.step_order AS my_step_order
          FROM approval_request_steps s
          JOIN approval_requests r ON r.id = s.request_id
          LEFT JOIN users u ON u.id = r.requester_id
         WHERE s.approver_id = %s {cond}
         ORDER BY r.created_at DESC
         LIMIT %s
        """,
        (user_id, max(1, min(int(limit), 500))),
    )


def outbox(app_db, user_id: int, *, limit: int = 100) -> List[Dict[str, Any]]:
    """내가 올린 결재."""
    return _q(
        app_db,
        """
        SELECT r.id, r.title, r.action_type, r.status, r.created_at,
               r.current_step_order, r.decided_at, r.apply_error
          FROM approval_requests r
         WHERE r.requester_id = %s
         ORDER BY r.created_at DESC
         LIMIT %s
        """,
        (user_id, max(1, min(int(limit), 500))),
    )


#: 진행 중 / 끝난 것. 화면의 두 탭이 이 두 묶음이다.
IN_PROGRESS = (E.PENDING,)
DONE = (E.APPROVED, E.REJECTED, E.CANCELED)


def involved(app_db, user_id: int, states: Sequence[str], *, limit: int = 200) -> List[Dict[str, Any]]:
    """**내가 관련된** 결재 — 내가 올렸거나, 결재선에 내 이름이 있는 것.

    받은 결재함(:func:`inbox`)과 다르다. 저쪽은 "지금 내가 눌러야 하는 것" 이고
    이쪽은 "내가 걸려 있는 것 전부" 다 — 아직 차례가 오지 않은 건, 내가 이미
    처리하고 뒷사람을 기다리는 건이 여기 보인다. 그 둘이 안 보이면 사람은
    자기가 올린 결재가 지금 누구 손에 있는지 알 방법이 없다.
    """
    if not states:
        return []
    marks = ", ".join(["%s"] * len(states))
    return _q(
        app_db,
        f"""
        SELECT r.id, r.title, r.action_type, r.status, r.created_at, r.decided_at,
               r.current_step_order, r.apply_error, r.requester_id,
               u.username AS requester_username, u.full_name AS requester_name,
               (r.requester_id = %s) AS i_requested,
               EXISTS (SELECT 1 FROM approval_request_steps x
                        WHERE x.request_id = r.id AND x.approver_id = %s) AS i_approve,
               (SELECT x.status FROM approval_request_steps x
                 WHERE x.request_id = r.id AND x.approver_id = %s) AS my_status,
               (SELECT COALESCE(cu.full_name, cu.username)
                  FROM approval_request_steps c
                  LEFT JOIN users cu ON cu.id = c.approver_id
                 WHERE c.request_id = r.id AND c.status = 'pending'
                 LIMIT 1) AS waiting_on
          FROM approval_requests r
          LEFT JOIN users u ON u.id = r.requester_id
         WHERE r.status IN ({marks})
           AND (r.requester_id = %s
                OR EXISTS (SELECT 1 FROM approval_request_steps y
                            WHERE y.request_id = r.id AND y.approver_id = %s))
         ORDER BY r.created_at DESC
         LIMIT %s
        """,
        (user_id, user_id, user_id, *states, user_id, user_id,
         max(1, min(int(limit), 500))),
    )


def can_read(req: Dict[str, Any], user_id: int, is_admin: bool) -> bool:
    """이 건을 볼 수 있는가 — 기안자, 결재선에 선 사람, 그리고 관리자.

    결재 내용은 남의 일이다. 목록에 없는 건의 id 를 찍어 넣어 읽는 길을
    열어 두면 결재함을 나눈 의미가 없다.
    """
    if is_admin:
        return True
    uid = int(user_id)
    if int(req.get("requester_id") or 0) == uid:
        return True
    return any(int(s.get("approver_id") or 0) == uid for s in req.get("steps") or [])
