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

from xgen_sdk.approval import blocks as blocks_mod
from xgen_sdk.approval import engine as E
from xgen_sdk.approval import notifier
from xgen_sdk.approval import templates
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
        SELECT l.id, l.name, l.description, l.owner_id, l.is_shared, l.is_active,
               l.form_id, f.name AS form_name
          FROM approval_lines l
          LEFT JOIN approval_forms f ON f.id = l.form_id
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
              "SELECT l.id, l.name, l.description, l.owner_id, l.is_shared, l.is_active, "
              "       l.form_id, f.name AS form_name "
              "  FROM approval_lines l "
              "  LEFT JOIN approval_forms f ON f.id = l.form_id "
              " WHERE l.id = %s", (int(line_id),))
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

    양식이 붙어 있으면 **줄을 그 양식보다 짧게 만들 수 없다**
    (:func:`_guard_form_still_fits`).
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
        _guard_form_still_fits(app_db, line, len(planned))
        _q(app_db, "DELETE FROM approval_line_steps WHERE line_id = %s", (int(line_id),))
        for st in planned:
            _q(app_db,
               "INSERT INTO approval_line_steps (line_id, step_order, approver_id) "
               "VALUES (%s, %s, %s)",
               (int(line_id), st["step_order"], st["approver_id"]))

    return get_line(app_db, line_id)


def _guard_form_still_fits(app_db, line: Dict[str, Any], approver_count: int) -> None:
    """줄을 **붙어 있는 양식보다 짧게** 만들지 못하게 한다.

    반대 방향(양식을 늘리는 것)은 :func:`_guard_lines_still_fit` 이 이미 막는다.
    한쪽만 막으면 같은 불변을 뒷문으로 깰 수 있다: 3차에 할 일이 있는 양식을
    3명 줄에 붙여 두고 줄을 2명으로 줄이면, 그 줄로 올라간 결재는 **3차 칸을
    지닌 채 3차가 없는** 문서가 된다. 그러면 필수 칸이 아무에게도 안 가고,
    마지막 결재자는 자기 단계까지만 검사받으므로 **그 칸을 건너뛴 채 승인이
    끝난다** — 요구했던 서류 한 장이 조용히 사라지는 셈이다.

    줄이려면 양식을 먼저 떼라고 말한다(그 편이 무슨 일이 일어나는지 분명하다).
    """
    form_id = line.get("form_id")
    if not form_id:
        return
    rows = _q(app_db, """
        SELECT COALESCE(MAX(step_index), 0) AS need, (SELECT name FROM approval_forms WHERE id = %s) AS name
          FROM approval_form_steps WHERE form_id = %s
    """, (int(form_id), int(form_id)))
    if not rows:
        return
    need = int(rows[0].get("need") or 0)
    if approver_count >= need:
        return
    name = rows[0].get("name") or f"#{form_id}"
    raise E.ApprovalError(
        f"이 결재선에 붙은 양식 [{name}] 은 결재자 {need}명이 필요합니다 "
        f"({approver_count}명으로 줄일 수 없습니다). 양식을 먼저 떼세요")


def list_all_shared_lines(app_db) -> List[Dict[str, Any]]:
    """**공용 결재선 전부** — 관리자 화면이 보는 목록.

    :func:`list_lines` 는 "내가 쓸 수 있는 것"(공용 + 내 것)이라 관리자에게도
    남이 만든 개인 결재선은 안 보인다. 여기는 소유자와 무관하게 공용 줄만
    본다 — 관리자가 손대는 대상이 딱 그것이기 때문이다.
    """
    lines = _q(app_db,
               "SELECT l.id, l.name, l.description, l.owner_id, l.is_shared, l.is_active, "
               "       l.form_id, f.name AS form_name, "
               "       COALESCE(u.full_name, u.username) AS owner_name "
               "  FROM approval_lines l "
               "  LEFT JOIN users u ON u.id = l.owner_id "
               "  LEFT JOIN approval_forms f ON f.id = l.form_id "
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
           "UPDATE approval_action_policies SET default_line_id = NULL, line_locked = FALSE "
           "WHERE default_line_id = %s", (int(line_id),))

    _q(app_db, "UPDATE approval_lines SET is_active = FALSE WHERE id = %s", (line_id,))


# ── 결재 문서 ─────────────────────────────────────────────────────────


def submit(app_db, *, requester_id: int, title: str, reason: str = "",
           action_type: str = GENERIC, payload: Optional[Dict[str, Any]] = None,
           line_id: Optional[int] = None,
           steps: Optional[Sequence[Dict[str, Any]]] = None,
           target_ref: Optional[str] = None,
           block_values: Sequence[Dict[str, Any]] = (),
           via_user_api: bool = False) -> Dict[str, Any]:
    """결재를 올린다. 템플릿(``line_id``) 또는 직접 지정(``steps``) 중 하나.

    템플릿을 썼더라도 단계는 **복사**한다 — 나중에 그 템플릿이 바뀌어도 이미
    올라간 결재의 결재선은 그대로여야 한다.

    ``target_ref``
        이 결재가 **무엇에 대한** 것인가 (``workflow:abc``, ``collection:사규``).
        같은 대상에 진행 중인 결재가 있으면 거절한다 — 두 건이 떠 있으면 어느
        쪽 승인이 그 대상을 바꾼 것인지 아무도 답할 수 없다.

    ``block_values``
        결재선에 양식이 붙어 있을 때 **기안 칸을 상신과 한 번에** 채운다.
        각 항목은 ``{"sort_order": 1, "data": {...}}`` — 양식에서 본 기안
        단계의 순번이 그대로 이름이다(``step_index`` 를 주면 0 이어야 한다).

        상신을 먼저 하고 칸을 따로 채워도 된다(:func:`fill_block`). 그래서
        여기서 안 채웠다고 막지 않는다 — 옛 상신 경로(배포·지식 등 기능 쪽
        코드)가 이 인자를 모르는 채로도 계속 올라가야 하기 때문이다. 대신
        빈 필수 칸이 있으면 :func:`engine.decide` 가 **승인을 막는다**.

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

    form = _line_form(app_db, line_id)
    _guard_steps_cover_form(app_db, form, len(planned))

    rows = _q(
        app_db,
        """
        INSERT INTO approval_requests
            (title, reason, action_type, payload, requester_id, line_id,
             form_id, form_name, status, current_step_order, target_ref)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s) RETURNING id
        """,
        (str(title).strip()[:200], (reason or "").strip() or None, action_type,
         json.dumps(payload or {}, ensure_ascii=False), requester_id, line_id,
         (form or {}).get("id"), (form or {}).get("name"),
         min(s["step_order"] for s in planned), ref),
    )
    request_id = rows[0]["id"]
    for s in planned:
        _q(app_db,
           """INSERT INTO approval_request_steps
                  (request_id, step_order, approver_id, status)
              VALUES (%s, %s, %s, %s)""",
           (request_id, s["step_order"], s["approver_id"], s["status"]))
    # 결재선에 양식이 붙어 있으면 **복사해 넣는다.** 단계와 같은 규칙이다 —
    # 심사 중에 요구 서류가 바뀌면 이미 승인한 사람의 판단 근거가 뒤바뀐다.
    snapshot_form_blocks(app_db, request_id=request_id, line_id=line_id,
                         form_id=(form or {}).get("id"))
    if block_values:
        _apply_draft_values(app_db, request_id=request_id,
                            requester_id=requester_id, values=block_values)
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
               r.line_id, r.form_id, r.form_name,
               r.status, r.current_step_order, r.decided_at,
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
    # 양식의 칸도 함께 준다. 없으면 빈 목록 — 화면은 그것으로 [기본 결재] 를
    # 판정한다(따로 묻지 않아도 되게).
    req["blocks"] = list_request_blocks(app_db, request_id)
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
    #     양식이 붙은 결재는 **내 단계의 필수 칸**까지 본다(거절은 막지 않는다).
    E.decide(current, current["steps"], actor_id, action, note, now,
             blocks=list_request_blocks(app_db, request_id))

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
        # 칸의 값부터 제자리로 보낸다 — 2차가 적은 위험도 평가가 거버넌스
        # 원장에 남는 것이 그 예다. 행위 적용(배포 등)보다 먼저 하는 이유는,
        # 적용이 실패해 사람이 다시 시도할 때 **판단의 근거는 이미 남아 있어야**
        # 하기 때문이다.
        err = _join(_apply_blocks(app_db, loaded), run_apply(action_type, payload, loaded))
    else:
        err = run_reject(action_type, payload, loaded)
    _q(app_db,
       "UPDATE approval_requests SET applied_at = %s, apply_error = %s "
       " WHERE id = %s AND applied_at IS NULL",
       (_now(), err or None, request_id))
    if err:
        logger.error("결재 #%s 뒤처리 실패(%s): %s", request_id, status, err)
    return err


def _join(*errs: Optional[str]) -> str:
    return " / ".join(e for e in errs if e)


def _apply_blocks(app_db, loaded: Dict[str, Any]) -> str:
    """승인된 결재의 **칸 값을 제자리로** 보낸다. 사유 문자열(빈 문자열 = 성공).

    칸 종류마다 갈 곳이 다르고 그 표를 아는 것은 SDK 가 아니라 각 서비스라,
    무엇을 할지는 :func:`blocks.register_applier` 로 꽂힌 것만 한다. 아무것도
    안 꽂혀 있으면 아무 일도 하지 않는다 — 그것이 정상이다(대부분의 칸은 읽히는
    것으로 제 몫을 다한다).

    한 칸이 실패해도 **나머지는 계속** 보낸다. 첫 실패에서 멈추면 뒤 칸들은
    시도조차 되지 않은 채 "적용 실패" 하나로 뭉뚱그려진다.
    """
    errs: List[str] = []
    for b in loaded.get("blocks") or []:
        fn = blocks_mod.applier(b.get("block_type"))
        if fn is None:
            continue
        data = blocks_mod.parse_data(b.get("data"))
        if data is None:
            continue                      # 안 채운 칸(선택 칸)은 보낼 것이 없다
        try:
            fn(app_db, loaded, b, data)
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{b.get('label') or b.get('block_type')}: {exc}")
            logger.exception("결재 #%s 칸 적용 실패 (%s)", loaded.get("id"), b.get("block_type"))
    return " / ".join(errs)


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


# ── 결재 양식 ─────────────────────────────────────────────────────────
#
# 양식은 **결재선에 붙는다.** 붙지 않은 결재선은 [기본 결재] — 결재선과 결재
# 내용만 적는다(지금까지의 동작 그대로).


def list_forms(app_db, *, include_inactive: bool = False) -> List[Dict[str, Any]]:
    """양식 목록. 각 양식의 칸 수와 쓰이는 결재선 수를 함께 준다 —
    지우기 전에 무엇이 영향을 받는지 보여야 한다."""
    where = "" if include_inactive else "WHERE f.is_active = TRUE"
    rows = _q(app_db, f"""
        SELECT f.id, f.name, f.description, f.is_builtin, f.owner_id,
               f.is_active, f.created_at, f.updated_at,
               (SELECT COALESCE(MAX(s.step_index), 0) FROM approval_form_steps s
                 WHERE s.form_id = f.id) AS approver_steps,
               (SELECT COUNT(*) FROM approval_form_blocks b WHERE b.form_id = f.id) AS block_count,
               (SELECT COUNT(*) FROM approval_lines l WHERE l.form_id = f.id) AS line_count
        FROM approval_forms f {where} ORDER BY f.is_builtin DESC, f.id
    """)
    return [dict(r) for r in rows]


def get_form(app_db, form_id: int) -> Optional[Dict[str, Any]]:
    rows = _q(app_db, """
        SELECT id, name, description, notice, is_builtin, owner_id, is_active
        FROM approval_forms WHERE id = %s
    """, (form_id,))
    if not rows:
        return None
    form = dict(rows[0])
    form["steps"] = [dict(r) for r in _q(app_db, """
        SELECT id, step_index, title, guide FROM approval_form_steps
        WHERE form_id = %s ORDER BY step_index
    """, (form_id,))]
    form["blocks"] = [dict(b) for b in _q(app_db, """
        SELECT id, step_index, block_type, label, config, required, sort_order
        FROM approval_form_blocks WHERE form_id = %s
        ORDER BY step_index, sort_order, id
    """, (form_id,))]
    #: 결재자 수 = 가장 뒤 단계 번호(0 은 기안). 컬럼으로 따로 들고 있으면
    #: 단계를 고칠 때마다 두 곳이 어긋날 수 있어 **파생**으로 둔다.
    form["approver_steps"] = max([int(st["step_index"]) for st in form["steps"]] or [0])
    return form


def validate_form_shape(steps: Sequence[Dict[str, Any]],
                        blocks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """양식의 모양이 성립하는지 본다. 어긋나면 **왜** 인지 말하고 던진다.

    규칙
    ----
      · 단계 번호는 0부터 **끊기지 않고** 이어져야 한다. 2차를 지우고 3차를
        남기면 화면은 그 사이를 그릴 수 없고, 결재선 대조도 뜻을 잃는다.
      · 결재자가 **한 명 이상**이어야 한다. 기안만 있는 결재는 결재가 아니다.
      · 칸은 **있는 단계에만** 놓인다. 3차를 2차로 줄이면서 3차의 칸을 그대로
        두면 그 칸은 영영 아무도 못 채운다 — 조용히 버리지 않고 막는다.
      · 칸 종류는 레지스트리에 있어야 한다.
    """
    idx = sorted({int(st.get("step_index", 0) or 0) for st in steps or []})
    if not idx:
        raise E.ApprovalError("단계가 없습니다 — 기안(1차)과 결재자가 최소 한 명 필요합니다")
    if idx[0] != 0:
        raise E.ApprovalError("1차(기안) 단계가 없습니다")
    if idx != list(range(len(idx))):
        missing = [i for i in range(idx[-1] + 1) if i not in idx]
        raise E.ApprovalError(
            "중간 단계가 비어 있습니다: " + ", ".join(f"{i + 1}차" for i in missing))
    if idx[-1] < 1:
        raise E.ApprovalError("결재자가 없습니다 — 2차 이상을 한 단계 이상 두세요")

    checked: List[Dict[str, Any]] = []
    for i, b in enumerate(blocks or []):
        btype = str(b.get("block_type") or "")
        if not blocks_mod.is_known(btype):
            raise E.ApprovalError(f"모르는 칸 종류입니다: {btype}")
        step = int(b.get("step_index") or 0)
        if step not in idx:
            raise E.ApprovalError(
                f"{step + 1}차 단계가 없는데 그 단계에 칸이 있습니다: "
                f"{b.get('label') or btype}")
        checked.append({**b, "block_type": btype, "step_index": step,
                        "_given": (int(b.get("sort_order") or 0), i)})

    #: ``sort_order`` 는 **단계 안에서 1부터 빈틈없이** 다시 매긴다.
    #:
    #: 보기 순서를 맞추려는 게 아니다. 상신하며 칸 값을 함께 보낼 때 화면이
    #: "양식의 이 칸" 을 가리키는 이름이 ``(단계, 순번)`` 이기 때문이다
    #: (스냅샷 행은 상신 전에는 id 가 없다). 관리자가 순번을 겹쳐 적어 두면
    #: 그 이름이 두 칸을 가리켜 값이 엉뚱한 칸에 들어간다.
    out: List[Dict[str, Any]] = []
    for step in idx:
        mine = [b for b in checked if b["step_index"] == step]
        mine.sort(key=lambda b: b["_given"])
        for n, b in enumerate(mine, start=1):
            b.pop("_given", None)
            out.append({**b, "sort_order": n})
    return out


def create_form(app_db, *, name: str, description: str = "", owner_id: Optional[int] = None,
                steps: Sequence[Dict[str, Any]] = (),
                blocks: Sequence[Dict[str, Any]] = (),
                notice: str = "",
                is_builtin: bool = False) -> int:
    """양식을 만든다. 템플릿에서 복사하든 처음부터 만들든 여기로 온다.

    ``notice`` 는 **결재 문서에 그대로 실리는 안내**다 — 기안자와 결재자가 문서를
    보면서 읽는다. 관리자끼리 보는 한 줄은 ``description`` 이다.
    """
    name = str(name or "").strip()
    if not name:
        raise E.ApprovalError("양식 이름이 필요합니다")
    checked = validate_form_shape(steps, blocks)
    rows = _q(app_db, """
        INSERT INTO approval_forms (name, description, notice, is_builtin, owner_id, is_active)
        VALUES (%s, %s, %s, %s, %s, TRUE) RETURNING id
    """, (name[:100], (description or "").strip()[:500] or None,
          (notice or "").strip() or None, bool(is_builtin), owner_id))
    form_id = rows[0]["id"]
    _write_steps(app_db, form_id, steps)
    _write_blocks(app_db, form_id, checked)
    return form_id


def _write_steps(app_db, form_id: int, steps: Sequence[Dict[str, Any]]) -> None:
    _q(app_db, "DELETE FROM approval_form_steps WHERE form_id = %s", (form_id,))
    for st in sorted(steps or [], key=lambda x: int(x.get("step_index") or 0)):
        i = int(st.get("step_index") or 0)
        _q(app_db, """
            INSERT INTO approval_form_steps (form_id, step_index, title, guide)
            VALUES (%s, %s, %s, %s)
        """, (form_id, i, str(st.get("title") or default_step_title(i))[:100],
              str(st.get("guide") or "")[:1000] or None))


def _write_blocks(app_db, form_id: int, blocks: Sequence[Dict[str, Any]]) -> None:
    _q(app_db, "DELETE FROM approval_form_blocks WHERE form_id = %s", (form_id,))
    for b in blocks or []:
        cfg = b.get("config")
        _q(app_db, """
            INSERT INTO approval_form_blocks
                (form_id, step_index, block_type, label, config, required, sort_order)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (form_id, b["step_index"], b["block_type"],
              str(b.get("label") or blocks_mod.spec(b["block_type"]).label)[:200],
              json.dumps(cfg, ensure_ascii=False) if cfg is not None else None,
              bool(b.get("required", True)), b["sort_order"]))


def default_step_title(step_index: int) -> str:
    """이름을 안 준 단계의 기본 이름. 화면은 사람이 세는 방식(N차)으로 보여 준다."""
    return "1차 기안" if int(step_index) == 0 else f"{int(step_index) + 1}차 결재"


def update_form(app_db, form_id: int, *, name: Optional[str] = None,
                description: Optional[str] = None, notice: Optional[str] = None,
                is_active: Optional[bool] = None,
                steps: Optional[Sequence[Dict[str, Any]]] = None,
                blocks: Optional[Sequence[Dict[str, Any]]] = None) -> None:
    """양식을 고친다.

    단계와 칸은 **함께** 바꾼다. 단계만 줄이면 남은 칸이 갈 곳을 잃고, 칸만
    옮기면 없는 단계를 가리킨다 — 둘을 따로 고치는 API 를 두면 그 사이 상태가
    반드시 생긴다.
    """
    form = get_form(app_db, form_id)
    if form is None:
        raise E.ApprovalError("양식을 찾을 수 없습니다")
    if form.get("is_builtin"):
        # 기본 양식을 고치면 다음 사람은 무엇이 기본이었는지 알 수 없다.
        raise E.ApprovalError("기본 양식은 고칠 수 없습니다 — 복사해서 쓰세요")

    if is_active is False:
        # **내리는 것은 지우는 것과 같은 무게다.** 목록에서만 사라지고 결재선은
        # 계속 그 양식을 가리키면, 관리자는 내렸다고 믿는데 새 결재는 그대로
        # 그 칸을 받는다 — 효력은 남고 보이지만 않는, 가장 나쁜 종류의 상태다.
        used = _q(app_db, "SELECT name FROM approval_lines WHERE form_id = %s", (form_id,))
        if used:
            names = ", ".join(str(r["name"]) for r in used[:5])
            raise E.ApprovalError(
                f"이 양식을 쓰는 결재선이 있어 내릴 수 없습니다: {names} — 먼저 그 결재선에서 떼세요")

    if steps is not None or blocks is not None:
        next_steps = steps if steps is not None else form["steps"]
        next_blocks = blocks if blocks is not None else [{
            "step_index": b["step_index"], "block_type": b["block_type"],
            "label": b["label"], "config": blocks_mod.parse_data(b.get("config")),
            "required": b["required"], "sort_order": b["sort_order"],
        } for b in form["blocks"]]
        checked = validate_form_shape(next_steps, next_blocks)
        _guard_lines_still_fit(app_db, form_id, next_steps)
        _write_steps(app_db, form_id, next_steps)
        _write_blocks(app_db, form_id, checked)

    sets, args = [], []
    if name is not None:
        if not str(name).strip():
            raise E.ApprovalError("양식 이름이 필요합니다")
        sets.append("name = %s"); args.append(str(name).strip()[:100])
    if description is not None:
        sets.append("description = %s"); args.append(str(description).strip()[:500] or None)
    if notice is not None:
        # 빈 문자열은 **지우라는 뜻**이다 — 안내를 없애는 길이 있어야 한다.
        sets.append("notice = %s"); args.append(str(notice).strip() or None)
    if is_active is not None:
        sets.append("is_active = %s"); args.append(bool(is_active))
    if sets:
        sets.append("updated_at = %s"); args.append(_now())
        _q(app_db, f"UPDATE approval_forms SET {', '.join(sets)} WHERE id = %s", (*args, form_id))


def _guard_lines_still_fit(app_db, form_id: int, steps: Sequence[Dict[str, Any]]) -> None:
    """단계를 늘렸는데 이미 붙어 있는 결재선이 짧으면 막는다.

    통과시키면 그 결재선으로 올라간 결재는 **채울 사람이 없는 칸**을 안고
    영원히 멈춘다. 늘리기 전에 결재선을 먼저 늘리라고 말한다.
    """
    need = max([int(st.get("step_index") or 0) for st in steps or []] or [0])
    rows = _q(app_db, """
        SELECT l.id, l.name,
               (SELECT COUNT(*) FROM approval_line_steps s WHERE s.line_id = l.id) AS n
        FROM approval_lines l WHERE l.form_id = %s
    """, (form_id,))
    short = [r for r in rows if int(r["n"]) < need]
    if short:
        names = ", ".join(f"{r['name']}({r['n']}명)" for r in short[:5])
        raise E.ApprovalError(
            f"이 양식은 결재자 {need}명이 필요한데 더 짧은 결재선이 쓰고 있습니다: {names}")


def copy_form(app_db, form_id: int, *, name: str, owner_id: Optional[int] = None) -> int:
    """양식을 복사한다. **기본 템플릿을 쓰는 길**이자 커스텀의 출발점이다."""
    src = get_form(app_db, form_id)
    if src is None:
        raise E.ApprovalError("양식을 찾을 수 없습니다")
    return create_form(
        app_db, name=name, description=src.get("description") or "",
        # 안내까지 따라와야 복사본이 **같은 양식**이다 — 이름만 같고 문서에
        # 적힌 규칙이 빠진 복사본은 쓰는 사람을 속인다.
        notice=src.get("notice") or "",
        owner_id=owner_id,
        steps=[{"step_index": st["step_index"], "title": st["title"], "guide": st.get("guide")}
               for st in src.get("steps") or []],
        blocks=[{
            "step_index": b["step_index"], "block_type": b["block_type"],
            "label": b["label"], "config": blocks_mod.parse_data(b.get("config")),
            "required": b["required"], "sort_order": b["sort_order"],
        } for b in src.get("blocks") or []],
        is_builtin=False,
    )


def delete_form(app_db, form_id: int) -> None:
    form = get_form(app_db, form_id)
    if form is None:
        return
    if form.get("is_builtin"):
        raise E.ApprovalError("기본 양식은 지울 수 없습니다")
    used = _q(app_db, "SELECT name FROM approval_lines WHERE form_id = %s", (form_id,))
    if used:
        names = ", ".join(str(r["name"]) for r in used[:5])
        raise E.ApprovalError(f"이 양식을 쓰는 결재선이 있습니다: {names}")
    _q(app_db, "DELETE FROM approval_forms WHERE id = %s", (form_id,))


def seed_builtin_forms(app_db) -> List[str]:
    """XGEN 이 제공하는 템플릿을 **없는 것만** 심는다. 심은 이름들을 돌려준다.

    이미 있는 것은 **손대지 않는다.** 같은 이름을 덮어쓰면, 그 양식을 쓰던
    결재선이 어느 날 배포만으로 요구 서류가 달라진다 — 조직이 고쳐 쓰라고 준
    것을 우리가 도로 바꾸는 셈이다. 템플릿 내용이 달라지면 **새 이름**으로
    항목을 하나 더 둔다(옛 이름을 쓰던 조직은 그대로 간다).

    기동 때마다 부를 수 있게 멱등이다.
    """
    have = {str(r["name"]): r for r in _q(
        app_db, "SELECT id, name, description, notice, is_builtin FROM approval_forms")}
    made: List[str] = []
    for t in templates.BUILTIN_TEMPLATES:
        row = have.get(t["name"])
        if row is not None:
            _backfill_builtin_notice(app_db, row, t)
            _refresh_builtin_texts(app_db, row, t)
            continue
        create_form(app_db, name=t["name"], description=t.get("description") or "",
                    notice=t.get("notice") or "",
                    owner_id=None, steps=t["steps"], blocks=t["blocks"], is_builtin=True)
        made.append(t["name"])
    if made:
        logger.info("내장 결재 양식 %d벌 심음: %s", len(made), ", ".join(made))
    return made


def _backfill_builtin_notice(app_db, row: Dict[str, Any], template: Dict[str, Any]) -> None:
    """우리가 심은 양식의 **비어 있는** 안내만 채운다.

    안내(``notice``)는 템플릿보다 늦게 생겼다. 이미 돌아가는 조직의 내장 양식은
    이름이 같다는 이유로 시딩에서 건너뛰어, 우리가 함께 주기로 한 안내를 영영
    받지 못한다 — 새로 설치한 곳에만 있는 안내는 제품이 주는 것이 아니다.

    그래서 여기서만 예외를 둔다. 다만 조건이 둘이다:

      * **우리가 심은 양식**(``is_builtin``)만. 사용자가 만든 양식은 이름이
        같아도 그의 것이다.
      * **비어 있을 때만**. 채워져 있으면 건드리지 않는다 — 덮어쓰면 배포가
        조직의 문서를 바꾸는 것이고, 그것이 시딩을 "없는 이름만" 으로 묶어 둔
        이유다. 내장 양식은 사용자가 고칠 수 없으니 여기 값이 있다면 그것은
        우리가 넣은 것이고, 우리 것끼리 조용히 덮어쓸 이유도 없다.
    """
    if not row.get("is_builtin"):
        return
    if str(row.get("notice") or "").strip():
        return
    notice = str(template.get("notice") or "").strip()
    if not notice:
        return
    _q(app_db, "UPDATE approval_forms SET notice = %s, updated_at = %s WHERE id = %s",
       (notice, _now(), row["id"]))
    logger.info("내장 결재 양식 '%s' 의 빈 문서 안내를 채웠다", row.get("name"))


def _refresh_builtin_texts(app_db, row: Dict[str, Any], template: Dict[str, Any]) -> None:
    """우리가 심은 양식에 **예전 글이 글자 그대로** 남아 있으면 지금 글로 바꾼다.

    절차가 바뀌었는데(위험도 평가를 거버넌스와 같은 기준으로) 이미 돌아가는
    조직의 문서에는 "등급만 정하면 된다" 가 남으면, 문서가 사람에게 틀린 절차를
    가르친다. 바꾸는 것은 **글**(설명·안내·단계 안내문)뿐이다 — 단계·칸·필수 여부는
    결재선이 붙잡고 있는 절차라 건드리지 않는다.

    조건은 :func:`_backfill_builtin_notice` 와 같다: 우리가 심은 양식만, 그리고
    :data:`templates.LEGACY_TEXTS` 에 적힌 **예전 글과 정확히 같을 때만**.
    """
    if not row.get("is_builtin"):
        return
    legacy = templates.LEGACY_TEXTS.get(str(row.get("name") or ""))
    if not legacy:
        return
    changed: List[str] = []
    for field in ("description", "notice"):
        now = str(row.get(field) or "")
        new = str(template.get(field) or "").strip()
        if new and now != new and now in (legacy.get(field) or []):
            # field 는 위 튜플의 두 이름뿐이다 — 사용자 입력이 SQL 에 들어가지 않는다.
            _q(app_db, f"UPDATE approval_forms SET {field} = %s, updated_at = %s WHERE id = %s",
               (new, _now(), row["id"]))
            changed.append(field)
    old_guides = legacy.get("guides") or {}
    if old_guides:
        new_by_index = {int(s["step_index"]): str(s.get("guide") or "") for s in template["steps"]}
        for step in _q(app_db, """
            SELECT id, step_index, title, guide FROM approval_form_steps
             WHERE form_id = %s ORDER BY step_index
        """, (row["id"],)):
            idx = int(step["step_index"])
            now = str(step.get("guide") or "")
            new = new_by_index.get(idx, "")
            if new and now != new and now in (old_guides.get(idx) or []):
                _q(app_db, "UPDATE approval_form_steps SET guide = %s WHERE id = %s",
                   (new, step["id"]))
                changed.append(f"{idx + 1}차 안내")
    if changed:
        logger.info("내장 결재 양식 '%s' 의 예전 글을 바꿨다: %s", row.get("name"), ", ".join(changed))


def set_line_form(app_db, line_id: int, form_id: Optional[int]) -> None:
    """결재선에 양식을 붙이거나 뗀다.

    양식이 3차에 할 일을 적어 뒀는데 결재선에 두 명뿐이면 그 일은 영원히
    아무도 하지 않는다 — 붙이기 전에 대조한다.
    """
    if form_id is not None:
        form = get_form(app_db, form_id)
        if form is None:
            raise E.ApprovalError("양식을 찾을 수 없습니다")
        rows = _q(app_db, "SELECT COUNT(*) AS n FROM approval_line_steps WHERE line_id = %s",
                  (line_id,))
        have = int(rows[0]["n"]) if rows else 0
        need = int(form.get("approver_steps") or 0)
        if have < need:
            raise E.ApprovalError(
                f"이 양식은 결재자 {need}명이 필요합니다 (이 결재선은 {have}명)")
    _q(app_db, "UPDATE approval_lines SET form_id = %s, updated_at = %s WHERE id = %s",
       (form_id, _now(), line_id))


def _line_form(app_db, line_id: Optional[int]) -> Optional[Dict[str, Any]]:
    """결재선에 붙은 양식 ``{"id", "name"}``. 없으면 ``None`` — [기본 결재]."""
    if not line_id:
        return None
    rows = _q(app_db, """
        SELECT f.id, f.name FROM approval_lines l
          JOIN approval_forms f ON f.id = l.form_id
         WHERE l.id = %s
    """, (int(line_id),))
    return dict(rows[0]) if rows else None


def _guard_steps_cover_form(app_db, form: Optional[Dict[str, Any]], approver_count: int) -> None:
    """올리는 결재선이 **양식을 덮는지** 본다. 모자라면 올리지 못한다.

    결재선 관리 쪽에서 이미 두 방향을 막아 뒀지만(양식을 늘릴 때 / 줄을 줄일 때),
    **상신 화면은 그 줄을 불러온 뒤 결재자를 뺄 수 있다.** 그 길로 빠져나가면
    3차에 할 일이 있는 양식인데 결재자가 둘뿐인 결재가 만들어지고, 그 칸은
    아무에게도 가지 않는다 — 마지막 사람은 자기 단계까지만 검사받으므로
    **필수 칸이 빈 채로 승인이 끝난다.** 실측으로 재현한 구멍이다.

    막는 자리를 여기로 둔 이유는 여기가 **모든 상신이 지나는 한 곳**이기 때문이다
    (사람이 올리든 기능 코드가 올리든).
    """
    if not form:
        return
    form_id = form.get("id")
    rows = _q(app_db,
              "SELECT COALESCE(MAX(step_index), 0) AS need FROM approval_form_steps WHERE form_id = %s",
              (int(form_id),))
    need = int(rows[0]["need"]) if rows else 0
    if approver_count >= need:
        return
    raise E.ApprovalError(
        f"이 결재선의 양식 [{form.get('name') or form_id}] 은 결재자 {need}명이 필요합니다 "
        f"(지금 {approver_count}명) — 그만큼 세우거나 다른 결재선으로 올려 주세요")


def _apply_draft_values(app_db, *, request_id: int, requester_id: int,
                        values: Sequence[Dict[str, Any]]) -> None:
    """상신과 함께 온 **기안 칸** 값을 넣는다.

    이름은 ``(0단계, 순번)`` 이다 — 스냅샷 행의 id 는 상신 전에는 없으므로
    화면이 가리킬 수 있는 이름이 그것뿐이다(:func:`validate_form_shape` 가
    순번을 단계마다 1부터 다시 매겨 그 이름이 한 칸만 가리키게 한다).

    모르는 순번은 **조용히 버리지 않고** 막는다. 기획서를 붙였다고 믿은 채
    상신됐는데 실제로는 빈 칸이면, 그 사실은 2차가 승인을 눌러 거절당할 때에야
    드러난다.
    """
    snapped = {int(b["sort_order"]): b for b in list_request_blocks(app_db, request_id)
               if int(b["step_order"]) == 0}
    now = _now()
    for v in values or []:
        step = int(v.get("step_index") or 0)
        if step != 0:
            raise E.ApprovalError("상신할 때는 기안(1차) 칸만 채울 수 있습니다")
        key = int(v.get("sort_order") or 0)
        block = snapped.get(key)
        if block is None:
            raise E.ApprovalError(f"이 양식에 없는 칸입니다 (기안 {key}번)")
        data = v.get("data")
        _check_block_value(block, data)
        _q(app_db, """
            UPDATE approval_request_blocks
               SET data = %s, filled_by = %s, filled_at = %s WHERE id = %s
        """, (json.dumps(data, ensure_ascii=False) if data is not None else None,
              requester_id, now, block["id"]))


def _check_block_value(block: Dict[str, Any], data: Any) -> None:
    """양식이 내건 조건을 어기면 **사람이 읽는 오류**로 바꿔 던진다."""
    try:
        blocks_mod.validate_data(block, data)
    except ValueError as exc:
        raise E.ApprovalError(str(exc)) from None


def snapshot_form_blocks(app_db, *, request_id: int, line_id: Optional[int] = None,
                         form_id: Optional[int] = None) -> None:
    """상신 시 양식을 복사해 넣는다. 양식이 없으면 아무것도 하지 않는다."""
    if not form_id:
        form = _line_form(app_db, line_id)
        form_id = (form or {}).get("id")
    if not form_id:
        return
    for b in _q(app_db, """
        SELECT step_index, block_type, label, config, required, sort_order
        FROM approval_form_blocks WHERE form_id = %s ORDER BY step_index, sort_order, id
    """, (form_id,)):
        _q(app_db, """
            INSERT INTO approval_request_blocks
                (request_id, step_order, block_type, label, config, required, sort_order)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (request_id, b["step_index"], b["block_type"], b["label"],
              b["config"], b["required"], b["sort_order"]))


def list_request_blocks(app_db, request_id: int) -> List[Dict[str, Any]]:
    return [dict(r) for r in _q(app_db, """
        SELECT id, step_order, block_type, label, config, required, sort_order,
               data, filled_by, filled_at
        FROM approval_request_blocks WHERE request_id = %s
        ORDER BY step_order, sort_order, id
    """, (request_id,))]


def fill_block(app_db, *, request_id: int, block_id: int, actor_id: int,
               data: Any) -> Dict[str, Any]:
    """칸을 채운다.

    **자기 단계의 칸만** 채울 수 있다. 남의 단계를 대신 채우면 그 단계의 판단이
    누구 것인지 알 수 없게 된다. 기안(0단계)은 기안자만.
    """
    rows = _q(app_db, """
        SELECT b.id, b.step_order, b.block_type, r.requester_id, r.status
        FROM approval_request_blocks b
        JOIN approval_requests r ON r.id = b.request_id
        WHERE b.id = %s AND b.request_id = %s
    """, (block_id, request_id))
    if not rows:
        raise E.ApprovalError("칸을 찾을 수 없습니다")
    row = rows[0]
    if str(row["status"]) != "pending":
        raise E.ApprovalError("이미 끝난 결재입니다")
    step = int(row["step_order"] or 0)
    if step == 0:
        if int(row["requester_id"] or 0) != int(actor_id):
            raise E.ApprovalError("기안 칸은 기안자만 작성합니다")
        # **누군가 승인한 뒤에는 기안 칸을 고칠 수 없다.**
        #
        # 2차가 기획서 A 를 보고 "저위험" 을 매겼는데 기안자가 B 로 바꿔치기하면,
        # 3차는 B 를 보면서 A 에 대한 승인을 근거로 결정한다 — 2차의 승인이 하지
        # 않은 말을 하게 되는 것이다. 회수를 "아무도 승인하기 전" 으로 묶어 둔
        # 것과 같은 이유다(승인 뒤 회수는 남의 승인을 없던 일로 만든다).
        #
        # 고쳐야 한다면 길은 하나다: 반려받고 다시 올린다. 그래야 바뀐 내용을
        # 모두가 **처음부터 다시** 본다.
        acted = _q(app_db, """
            SELECT 1 FROM approval_request_steps
             WHERE request_id = %s AND status = %s LIMIT 1
        """, (request_id, E.APPROVED))
        if acted:
            raise E.ApprovalError(
                "이미 승인한 결재자가 있어 기안 칸을 고칠 수 없습니다 — "
                "반려받은 뒤 다시 올려 주세요")
    else:
        mine = _q(app_db, """
            SELECT status FROM approval_request_steps
            WHERE request_id = %s AND step_order = %s AND approver_id = %s
        """, (request_id, step, actor_id))
        if not mine:
            raise E.ApprovalError("이 단계의 결재자가 아닙니다")
        if str(mine[0]["status"]) != "pending":
            raise E.ApprovalError("아직 차례가 아닙니다")
    full = next((b for b in list_request_blocks(app_db, request_id)
                 if int(b["id"]) == int(block_id)), None)
    if full is not None:
        _check_block_value(full, data)
    _q(app_db, """
        UPDATE approval_request_blocks
        SET data = %s, filled_by = %s, filled_at = %s WHERE id = %s
    """, (json.dumps(data, ensure_ascii=False) if data is not None else None,
          actor_id, _now(), block_id))
    return {"id": block_id, "step_order": step, "block_type": row["block_type"]}
