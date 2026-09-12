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
from xgen_sdk.approval.registry import (
    GENERIC, has_apply, has_reject, is_registered, is_user_submittable,
    owner_of, run_apply, run_reject,
)
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


def get_line(app_db, line_id: int) -> Optional[Dict[str, Any]]:
    """결재선 한 줄 + 결재자 순서. 내려간(비활성) 줄도 돌려준다 —
    "그때 어느 결재선으로 올렸나" 를 답해야 하기 때문이다."""
    rows = _q(app_db,
              "SELECT id, name, description, owner_id, is_shared, is_active "
              "FROM approval_lines WHERE id = %s", (int(line_id),))
    if not rows:
        return None
    line = dict(rows[0])
    line["steps"] = _q(
        app_db,
        """
        SELECT s.step_order, s.approver_id, u.username, u.full_name
          FROM approval_line_steps s
          LEFT JOIN users u ON u.id = s.approver_id
         WHERE s.line_id = %s
         ORDER BY s.step_order, s.id
        """,
        (int(line_id),),
    )
    return line


def update_line(app_db, line_id: int, *, name: Optional[str] = None,
                description: Optional[str] = None,
                is_shared: Optional[bool] = None,
                steps: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """결재선을 고친다 — 이름·설명·공용 여부·결재자 순서.

    ⚠ **이미 올라간 결재는 따라 바뀌지 않는다.** 상신 시점에 결재선을 건마다
    복제(스냅샷)해 두기 때문이다. 결재가 도는 중에 템플릿을 고쳤다고 결재자가
    바뀌면, 이미 누른 사람의 승인이 무슨 뜻인지 알 수 없게 된다.

    결재자를 주면 **통째로 갈아 끼운다**(부분 수정이 아니다). 순서는
    :func:`engine.plan_steps` 가 검증한다 — 같은 사람이 두 번 서거나 빈 줄이면
    거기서 걸린다.
    """
    line = get_line(app_db, line_id)
    if not line:
        raise E.ApprovalError("결재선을 찾을 수 없습니다")

    sets, params = [], []
    if name is not None:
        if not str(name).strip():
            raise E.ApprovalError("결재선 이름이 필요합니다")
        sets.append("name = %s")
        params.append(str(name).strip()[:100])
    if description is not None:
        sets.append("description = %s")
        params.append((str(description).strip()[:500] or None))
    if is_shared is not None:
        sets.append("is_shared = %s")
        params.append(bool(is_shared))
    if sets:
        params.append(int(line_id))
        _q(app_db, f"UPDATE approval_lines SET {', '.join(sets)} WHERE id = %s", tuple(params))

    if steps is not None:
        planned = E.plan_steps(steps)   # 규칙 검증은 engine 이 한다
        _q(app_db, "DELETE FROM approval_line_steps WHERE line_id = %s", (int(line_id),))
        for st in planned:
            _q(app_db,
               "INSERT INTO approval_line_steps (line_id, step_order, approver_id) "
               "VALUES (%s, %s, %s)",
               (int(line_id), st["step_order"], st["approver_id"]))

    return get_line(app_db, line_id)


def list_all_shared_lines(app_db) -> List[Dict[str, Any]]:
    """**공용 결재선 전부** — 관리자 화면이 보는 목록.

    :func:`list_lines` 는 "내가 쓸 수 있는 것"(공용 + 내 것)이라 관리자에게도
    남이 만든 개인 결재선은 안 보인다. 여기는 소유자와 무관하게 공용 줄만
    본다 — 관리자가 손대는 대상이 딱 그것이기 때문이다.
    """
    lines = _q(app_db,
               "SELECT l.id, l.name, l.description, l.owner_id, l.is_shared, l.is_active, "
               "       COALESCE(u.full_name, u.username) AS owner_name "
               "  FROM approval_lines l "
               "  LEFT JOIN users u ON u.id = l.owner_id "
               " WHERE l.is_active = TRUE AND l.is_shared = TRUE "
               " ORDER BY l.name")
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


def default_policies_using_line(app_db, line_id: int) -> List[str]:
    """이 결재선을 **기본 결재선으로 쓰고 있는** 행위들.

    내리기 전에 물어봐야 한다. 그냥 내리면 그 행위의 기본 결재선이 죽은 줄을
    가리키고, 사용자는 모달이 빈 채로 뜨는 것을 본다 — 화면 어디에도 이유가
    안 나온다.
    """
    rows = _q(app_db,
              "SELECT action_type FROM approval_action_policies WHERE default_line_id = %s",
              (int(line_id),))
    return [str(r["action_type"]) for r in rows]


def delete_line(app_db, line_id: int, actor_id: int, is_superuser: bool,
                *, force: bool = False) -> None:
    """**지우지 않고 내린다.** 이 줄을 쓴 결재 기록의 line_id 가 가리킬 곳이
    사라지면, 나중에 "어느 결재선으로 올렸는가" 를 답할 수 없다.

    어떤 행위의 **기본 결재선**으로 쓰이는 줄은 그냥 내리지 않는다. 내리면 그
    행위의 모달이 빈 채로 뜨는데 화면 어디에도 이유가 안 나온다. ``force`` 를
    주면 그 행위들의 기본 결재선을 **함께 비우고** 내린다 — 호출부가 사용자에게
    무엇이 비는지 보여 준 뒤에 쓰라는 뜻이다.
    """
    owner = _q(app_db, "SELECT owner_id FROM approval_lines WHERE id = %s", (line_id,))
    if not owner:
        raise E.ApprovalError("결재선을 찾을 수 없습니다")
    if not is_superuser and int(owner[0]["owner_id"] or 0) != int(actor_id):
        raise E.ApprovalError("내가 만든 결재선만 지울 수 있습니다")

    used_by = default_policies_using_line(app_db, line_id)
    if used_by and not force:
        raise E.ApprovalError(
            "이 결재선을 기본 결재선으로 쓰는 행위가 있습니다: " + ", ".join(used_by))
    if used_by:
        _q(app_db,
           "UPDATE approval_action_policies SET default_line_id = NULL "
           "WHERE default_line_id = %s", (int(line_id),))

    _q(app_db, "UPDATE approval_lines SET is_active = FALSE WHERE id = %s", (line_id,))


# ── 결재 문서 ─────────────────────────────────────────────────────────


def submit(app_db, *, requester_id: int, title: str, reason: str = "",
           action_type: str = GENERIC, payload: Optional[Dict[str, Any]] = None,
           line_id: Optional[int] = None,
           steps: Optional[Sequence[Dict[str, Any]]] = None,
           target_ref: Optional[str] = None,
           via_user_api: bool = False) -> Dict[str, Any]:
    """결재를 올린다. 템플릿(``line_id``) 또는 직접 지정(``steps``) 중 하나.

    템플릿을 썼더라도 단계는 **복사**한다 — 나중에 그 템플릿이 바뀌어도 이미
    올라간 결재의 결재선은 그대로여야 한다.

    ``target_ref``
        이 결재가 **무엇에 대한** 것인가 (``workflow:abc``, ``collection:사규``).
        같은 대상에 진행 중인 결재가 있으면 거절한다 — 두 건이 떠 있으면 어느
        쪽 승인이 그 대상을 바꾼 것인지 아무도 답할 수 없다.

    ``via_user_api``
        사람이 화면에서 직접 올린 것인가. 게이트 행위(배포·컬렉션 생성·도구
        게시…)는 **여기로 들어올 수 없다** — payload 를 손으로 적어 올릴 수
        있으면 남의 워크플로우를 배포시키는 길이 된다. 그 행위들은 기능 쪽
        코드가 자기 맥락에서 올린다.
    """
    if not str(title or "").strip():
        raise E.ApprovalError("제목이 필요합니다")
    if not is_registered(action_type):
        raise E.ApprovalError(f"등록되지 않은 결재 종류입니다: {action_type}")
    if via_user_api and not is_user_submittable(action_type):
        raise E.ApprovalError(
            "이 종류는 직접 올릴 수 없습니다 — 해당 기능 화면에서 올라갑니다")

    ref = str(target_ref).strip()[:200] if target_ref else None
    if ref:
        dup = find_pending_for_target(app_db, action_type, ref)
        if dup:
            raise E.ApprovalError(
                f"이미 진행 중인 결재가 있습니다 (#{dup['id']}) — 그 건이 끝난 뒤에 다시 올려 주세요")

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
             status, current_step_order, target_ref)
        VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s) RETURNING id
        """,
        (str(title).strip()[:200], (reason or "").strip() or None, action_type,
         json.dumps(payload or {}, ensure_ascii=False), requester_id, line_id,
         min(s["step_order"] for s in planned), ref),
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
               r.target_ref, r.canceled_by, r.cancel_note,
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
        if status in (E.APPROVED, E.REJECTED):
            # 승인이면 적용하고, 거절이면 올려 둔 것을 치운다. **원장을 다시
            # 읽어서** 넘긴다 — 방금 전이시킨 상태를 훅이 봐야 한다(옛 스냅샷을
            # 주면 승인된 건에 거절 훅이 도는 종류의 사고가 난다).
            _settle_hooks(app_db, request_id, get(app_db, request_id))

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


def _settle_hooks(app_db, request_id: int, loaded: Dict[str, Any]) -> None:
    """결정이 난 뒤의 **뒤처리** — 적용(승인) 또는 치우기(거절·회수).

    여기서 바로 할지, 남겨 둘지를 가른다
    ------------------------------------
    결정은 core 한 곳에서 나지만 **행위는 남의 파드에 있다**. 지식 컬렉션을
    실제로 여는 코드는 xgen-documents 에, 클라우드·도구는 xgen-workflow 에
    있다. core 가 그 함수를 부를 수는 없으므로, 소유가 다른 건은 훅을 돌리지
    않고 ``applied_at`` 을 비워 둔다 — 소유 서비스의 워커가 그것을 표식 삼아
    가져간다(:func:`claim_settlements`).

    **실패해도 결정은 그대로 둔다.** 사람의 결재는 이미 일어난 사실이다.
    적용이 실패했다고 그 사실을 지우면, 화면은 "승인되지 않음" 이라 말하는데
    실제로는 세 사람이 승인한 상태가 된다. 실패는 숨기지 않고 ``apply_error``
    로 남긴다 — 그래야 누가 다시 시도한다.
    """
    action_type = loaded.get("action_type") or GENERIC
    status = str(loaded.get("status") or E.APPROVED)
    mine = has_apply(action_type) or has_reject(action_type) or owner_of(action_type) == "core"
    if not mine:
        return                      # 소유 서비스의 워커가 가져간다
    finish(app_db, request_id, action_type, loaded, status)


def finish(app_db, request_id: int, action_type: str,
           loaded: Dict[str, Any], status: str) -> str:
    """훅을 돌리고 ``applied_at`` 을 찍는다. 사유 문자열을 돌려준다(빈 문자열 = 성공).

    core 가 직접 부르기도 하고(자기 소유 행위), 위성 서비스의 워커가 부르기도
    한다. 어느 쪽이든 **한 건에 한 번**이다 — ``applied_at IS NULL`` 조건부
    쓰기가 그것을 지킨다.
    """
    payload = loaded.get("payload") or {}
    if status == E.APPROVED:
        err = run_apply(action_type, payload, loaded)
    else:
        err = run_reject(action_type, payload, loaded)
    _q(app_db,
       "UPDATE approval_requests SET applied_at = %s, apply_error = %s "
       " WHERE id = %s AND applied_at IS NULL",
       (_now(), err or None, request_id))
    if err:
        logger.error("결재 #%s 뒤처리 실패(%s): %s", request_id, status, err)
    return err


def cancel(app_db, request_id: int, actor_id: int) -> Dict[str, Any]:
    """기안자의 회수 — **아무도 승인하지 않았을 때만**(engine 이 판정한다)."""
    current = get(app_db, request_id)
    now = _now()
    E.cancel(current, current["steps"], actor_id, now)
    return _close_as_canceled(app_db, request_id, now, by="requester", note="")


def system_cancel(app_db, request_id: int, note: str) -> Dict[str, Any]:
    """**시스템의 회수** — 결재의 전제가 사라졌을 때.

    사람의 회수와 무엇이 다른가: 사람은 누군가 승인한 뒤에는 회수할 수 없다
    (남의 승인을 없던 일로 만드는 길을 열지 않는다). 시스템은 할 수 있어야
    한다 — 워크플로우가 수정되면 결재자들이 본 그 정의가 더는 존재하지 않고,
    그 상태로 승인이 이어지면 **아무도 본 적 없는 것이 배포된다**.

    그래서 사유를 반드시 남기고(``cancel_note``), 이미 처리한 결재자에게도
    알린다 — 내가 승인한 건이 왜 사라졌는지는 알아야 한다.
    """
    current = get(app_db, request_id)
    if str(current.get("status") or "") != E.PENDING:
        return current              # 이미 끝난 건은 건드리지 않는다
    now = _now()
    out = _close_as_canceled(app_db, request_id, now, by="system",
                             note=str(note or "")[:500])
    logger.info("결재 #%s 시스템 회수: %s", request_id, note)
    try:
        told = [int(s["approver_id"]) for s in out.get("steps") or []
                if s.get("status") in (E.APPROVED, E.SKIPPED)]
        notifier.notify_system_canceled(app_db, out, told, note=str(note or ""))
    except Exception as exc:  # noqa: BLE001 — 알림 실패가 회수를 막지 않는다
        logger.warning("결재 #%s 회수 알림 실패: %s", request_id, exc)
    return out


def _close_as_canceled(app_db, request_id: int, now, *, by: str, note: str) -> Dict[str, Any]:
    """아직 차례가 오지 않은 단계를 ``skipped`` 로 덮고 건을 닫는다.

    ``pending``/``waiting`` 만 덮는다 — 이미 승인한 단계는 **일어난 일**이라
    그대로 남는다(그래야 "회수 전에 누가 승인했었나" 를 나중에 답할 수 있다).
    """
    _q(app_db,
       "UPDATE approval_request_steps SET status = %s "
       " WHERE request_id = %s AND status IN (%s, %s)",
       (E.SKIPPED, request_id, E.WAITING, E.PENDING))
    _q(app_db,
       "UPDATE approval_requests SET status = %s, decided_at = %s, "
       "       canceled_by = %s, cancel_note = %s "
       " WHERE id = %s AND status = %s",
       (E.CANCELED, now, by, note or None, request_id, E.PENDING))
    out = get(app_db, request_id)
    _settle_hooks(app_db, request_id, out)     # 올려 둔 것이 있으면 치운다
    return get(app_db, request_id)


def find_pending_for_target(app_db, action_type: str, target_ref: str) -> Optional[Dict[str, Any]]:
    """이 대상에 **떠 있는** 결재. 화면이 "결재 진행 중" 을 보여 줄 때도 쓴다."""
    if not target_ref:
        return None
    rows = _q(app_db,
              "SELECT id, title, status, current_step_order, requester_id, created_at "
              "  FROM approval_requests "
              " WHERE action_type = %s AND target_ref = %s AND status = %s "
              " ORDER BY id DESC LIMIT 1",
              (str(action_type), str(target_ref), E.PENDING))
    return rows[0] if rows else None


# ── 소유 서비스의 뒤처리 워커 ─────────────────────────────────────────


def claim_settlements(app_db, action_types: Sequence[str], limit: int = 50) -> List[Dict[str, Any]]:
    """**뒤처리가 남은 건들** — 결정은 났는데 훅이 아직 안 돈 것.

    core 가 결정하고 소유 서비스가 적용하는 구조라, 그 사이를 잇는 것이 이
    질의다. 결정 순간 Redis 로 신호를 보내지만 그것만 믿지 않는다 — 신호를
    놓친 건이 영영 안 걸리면 "승인은 됐는데 아무 일도 안 일어나는" 상태가
    되고, 그건 사용자 눈에 **승인이 안 된 것과 구별되지 않는다**.
    """
    if not action_types:
        return []
    marks = ", ".join(["%s"] * len(action_types))
    return _q(app_db,
              f"""SELECT id, action_type, status
                    FROM approval_requests
                   WHERE applied_at IS NULL
                     AND status IN (%s, %s, %s)
                     AND action_type IN ({marks})
                   ORDER BY id
                   LIMIT %s""",
              (E.APPROVED, E.REJECTED, E.CANCELED, *action_types,
               max(1, min(int(limit), 200))))


def settle(app_db, request_id: int) -> Dict[str, Any]:
    """워커가 한 건을 처리한다 — 훅을 돌리고 결과를 원장에 적는다.

    실패는 ``apply_error`` 로 남고 기안자에게 알린다. 이 알림이 없으면
    "승인됐는데 아무 일도 안 일어난" 상태를 **아무도 모른다**.
    """
    loaded = get(app_db, request_id)
    status = str(loaded.get("status") or "")
    if loaded.get("applied_at") or status == E.PENDING:
        return loaded                                   # 이미 끝났거나 아직 이르다
    err = finish(app_db, request_id, loaded.get("action_type") or GENERIC, loaded, status)
    out = get(app_db, request_id)
    if err and status == E.APPROVED:
        try:
            notifier.notify_apply_failed(app_db, out, err)
        except Exception as exc:  # noqa: BLE001
            logger.warning("결재 #%s 적용 실패 알림 실패: %s", request_id, exc)
    return out


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


# ── 관리자 열람 — [결재 로그] ─────────────────────────────────────────


def search(app_db, *, statuses: Optional[Sequence[str]] = None,
           action_types: Optional[Sequence[str]] = None,
           query: str = "", since: Any = None, until: Any = None,
           limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """**전 사용자의 결재**를 훑는다 — 관리 화면 전용.

    :func:`involved` 와 무엇이 다른가: 저쪽은 "내가 걸려 있는 것" 이라 사용자
    자신의 축으로 잘린다. 이쪽은 자르지 않는다. 그래서 **부르는 쪽이 권한을
    확인해야 한다**(core 의 ``/api/admin/approval`` 이 ``require_perm`` 으로
    막는다). 결재 제목은 그 자체로 조직의 정보다 — 무슨 계약을, 누구를 뽑는지.

    ``query`` 는 제목과 기안자(이름·아이디)를 함께 훑는다. 결재를 찾는 사람이
    기억하는 것은 보통 둘 중 하나다.

    반환: ``{"rows": [...], "total": N}`` — 화면이 쪽수를 매길 수 있게.
    """
    where: List[str] = []
    params: List[Any] = []

    if statuses:
        where.append("r.status IN (" + ", ".join(["%s"] * len(statuses)) + ")")
        params += list(statuses)
    if action_types:
        where.append("r.action_type IN (" + ", ".join(["%s"] * len(action_types)) + ")")
        params += list(action_types)
    q = str(query or "").strip()
    if q:
        like = f"%{q}%"
        where.append("(r.title ILIKE %s OR u.full_name ILIKE %s OR u.username ILIKE %s "
                     "OR r.target_ref ILIKE %s)")
        params += [like, like, like, like]
    if since:
        where.append("r.created_at >= %s")
        params.append(since)
    if until:
        where.append("r.created_at <= %s")
        params.append(until)

    cond = ("WHERE " + " AND ".join(where)) if where else ""
    total_rows = _q(
        app_db,
        f"""SELECT COUNT(*) AS n
              FROM approval_requests r
              LEFT JOIN users u ON u.id = r.requester_id
             {cond}""",
        params,
    )
    total = int((total_rows[0] if total_rows else {}).get("n") or 0)

    rows = _q(
        app_db,
        f"""
        SELECT r.id, r.title, r.action_type, r.status, r.created_at, r.decided_at,
               r.applied_at, r.apply_error, r.target_ref, r.requester_id,
               r.canceled_by, r.cancel_note,
               u.username AS requester_username, u.full_name AS requester_name,
               (SELECT COALESCE(cu.full_name, cu.username)
                  FROM approval_request_steps c
                  LEFT JOIN users cu ON cu.id = c.approver_id
                 WHERE c.request_id = r.id AND c.status = 'pending'
                 LIMIT 1) AS waiting_on,
               (SELECT COUNT(*) FROM approval_request_steps t WHERE t.request_id = r.id) AS step_count
          FROM approval_requests r
          LEFT JOIN users u ON u.id = r.requester_id
         {cond}
         ORDER BY r.id DESC
         LIMIT %s OFFSET %s
        """,
        [*params, max(1, min(int(limit), 500)), max(0, int(offset))],
    )
    return {"rows": rows, "total": total}
