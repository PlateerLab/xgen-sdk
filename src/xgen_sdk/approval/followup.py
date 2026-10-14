"""An optional, manually selected second approval segment on the same document.

Only the source line's allowed destination IDs are fixed at submission.
Destination forms and steps are read live and snapshotted at connection.
The final first-segment decision and second-segment creation commit together.
"""
from __future__ import annotations

import json
import hashlib
from contextlib import contextmanager

from xgen_sdk.approval import engine as E
from xgen_sdk.approval.sql import q, transaction


class FollowupChanged(E.ApprovalError):
    """The reviewer must inspect the latest configuration and choose again."""

    def __init__(self):
        super().__init__('후속 결재선이 변경되었습니다. 최신 구성을 다시 확인하고 선택해 주세요.')


@contextmanager
def locked_configuration(app_db):
    """One atomic configuration view shared by edits, previews and connection.

    These small configuration tables are locked, not the approval ledger.
    Table locks also serialize legacy single-statement writers that do not
    participate in the advisory-lock protocol.
    """
    with transaction(app_db) as tx:
        q(tx, 'SELECT pg_advisory_xact_lock(718201014)')
        q(tx, '''LOCK TABLE approval_lines, approval_line_steps, approval_forms,
                 approval_form_steps, approval_form_blocks IN SHARE ROW EXCLUSIVE MODE''')
        yield tx


def decode(value, fallback=None):
    if value is None or value == "":
        return fallback
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def candidate_error(line, source_id=None):
    if source_id is not None and int(line["id"]) == int(source_id):
        return "자기 결재선을 후속 결재선으로 선택할 수 없습니다."
    if not line.get("is_active") or not line.get("is_shared"):
        return "활성 공용 결재선만 후속으로 연결할 수 있습니다."
    if line.get("followup_required"):
        return "후속 결재를 사용하는 결재선은 다시 연결할 수 없습니다. (최대 2개 결재선)"
    if not line.get("steps"):
        return "결재 차례가 없는 결재선입니다."
    form = line.get("form") or {}
    if form and not form.get("is_active"):
        return "결재 양식이 비활성 상태입니다."
    if int(form.get("approver_steps") or 0) > len(line["steps"]):
        return "양식에 필요한 결재 차례가 부족합니다."
    if any(int(b.get("step_index") or 0) == 0 and b.get("required")
           for b in form.get("blocks") or []):
        return "필수 기안 항목이 있는 양식은 후속으로 연결할 수 없습니다. 후속 검토용 양식을 사용하세요."
    return None


def load_candidate(app_db, line_id):
    from xgen_sdk.approval import store
    line = store.get_line(app_db, int(line_id))
    if line is None:
        raise E.ApprovalError("후속 결재선을 찾을 수 없습니다.")
    line["form"] = store.get_form(app_db, line["form_id"]) if line.get("form_id") else None
    return line


def validate_config(app_db, source_id, required, ids, kept=()):
    """Check a source line's followup configuration when it is saved.

    ``kept`` are candidates already allowed before this edit. They are not
    re-checked: a candidate that later became unusable (deactivated, unshared,
    form changed) is skipped when the final reviewer chooses, and must not
    stop an administrator from renaming or re-saving the source line.
    """
    if not required:
        return []
    allowed = list(dict.fromkeys(int(i) for i in (ids or [])))
    if not allowed:
        raise E.ApprovalError("후속 결재선을 하나 이상 선택하세요.")
    kept = {int(i) for i in (kept or ())}
    for line_id in allowed:
        if source_id is not None and int(line_id) == int(source_id):
            raise E.ApprovalError("자기 결재선을 후속 결재선으로 선택할 수 없습니다.")
        if line_id in kept:
            continue
        error = candidate_error(load_candidate(app_db, line_id), source_id)
        if error:
            raise E.ApprovalError(error)
    # A destination already used by a source cannot itself become a source.
    if source_id is not None and q(app_db, """
        SELECT id FROM approval_lines
         WHERE followup_required = TRUE AND id <> %s
           AND followup_line_ids::jsonb @> %s::jsonb
    """, (int(source_id), json.dumps([int(source_id)]))):
        raise E.ApprovalError("다른 결재선의 후속 후보로 사용 중입니다. 해당 연결을 먼저 해제하세요.")
    return allowed


def save_line(app_db, *, line_id=None, followup_required=None, followup_line_ids=None, **kwargs):
    from xgen_sdk.approval import store
    with locked_configuration(app_db) as tx:
        # Configuration edits are uncommon. Serialize source/destination changes
        # so two administrators cannot create A->B->A concurrently.
        old = store.get_line(tx, line_id) if line_id is not None else None
        required = bool(followup_required if followup_required is not None
                        else (old or {}).get("followup_required", False))
        ids = (followup_line_ids if followup_line_ids is not None
               else (old or {}).get("followup_line_ids", []))
        kept = (old or {}).get("followup_line_ids", []) if (old or {}).get("followup_required") else []
        ids = validate_config(tx, line_id, required, ids, kept)
        if line_id is None:
            made = store.create_line(tx, **kwargs)
            line_id = made["id"]
        else:
            store.update_line(tx, line_id, **kwargs)
        q(tx, "UPDATE approval_lines SET followup_required = %s, followup_line_ids = %s WHERE id = %s",
          (required, json.dumps(ids), line_id))
        return store.get_line(tx, line_id)


def snapshot(app_db, line_id):
    """Fix the allowed destination IDs at submission. Nothing else is checked here.

    Candidates are validated live when the final reviewer chooses
    (:func:`candidates` and :func:`decision` skip or refuse an unusable one).
    Re-validating them here would make one deactivated or edited destination
    line refuse every new submission on the source line, including approvals
    submitted by other services (agent deployment, RAG). Reading one row needs
    no configuration lock, so submission never waits on configuration edits.
    """
    from xgen_sdk.approval import store
    if not line_id:
        return None
    source = store.get_line(app_db, line_id)
    if not source or not source.get("followup_required"):
        return None
    ids = [i for i in dict.fromkeys(int(i) for i in (source.get("followup_line_ids") or []))
           if i != int(source["id"])]
    if not ids:
        return None
    # Keep old requests' immutable required=True snapshots intact. The
    # legacy line column now means enabled, not mandatory for new requests.
    return {"version": 2, "enabled": True, "required": False, "source_line_id": source["id"],
            "source_line_name": source["name"], "line_ids": ids}


def allowed_ids(config):
    # Existing local requests retain their immutable old config. Use only its
    # IDs; the old candidate bodies no longer determine a new connection.
    return [int(i) for i in config.get('line_ids', [])] if 'line_ids' in config else [
        int(c['id']) for c in config.get('candidates', [])]


def revision(candidate):
    body = {key: value for key, value in candidate.items() if key not in ('revision', 'unavailable_reason')}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def needs_selection(req):
    config = req.get('followup_config') or {}
    return bool(req.get('status') == E.PENDING and (config.get('enabled') or config.get('required'))
                and not req.get('followup_selection') and req.get('steps')
                and req['current_step_order'] == max(s['step_order'] for s in req['steps']))


def can_finalize(config):
    """Only explicitly versioned new submissions may end without a followup."""
    return config.get('version') == 2 and config.get('enabled') is True and config.get('required') is False


def candidates(app_db, request_id, actor_id):
    """Only the acting final reviewer can query the live allowlisted choices."""
    from xgen_sdk.approval import store
    with locked_request(app_db, request_id) as tx:
        req = store.get(tx, request_id)
        if not needs_selection(req) or not any(
                s['approver_id'] == actor_id and s['status'] == E.PENDING for s in req['steps']):
            raise E.ApprovalError('선행 결재선의 마지막 결재자만 후속 결재선을 선택할 수 있습니다.')
        config = req['followup_config']
        result = []
        with locked_configuration(tx):
            for line_id in allowed_ids(config):
                try:
                    candidate = load_candidate(tx, line_id)
                except E.ApprovalError:
                    continue  # A removed candidate is never offered.
                error = candidate_error(candidate, config.get('source_line_id')) or availability(tx, req, candidate)
                if error:
                    continue
                result.append({**candidate, 'revision': revision(candidate)})
        return result


def prepare_steps(request, candidate, picks=None):
    slots = E.plan_line_slots(candidate["steps"])
    chosen = E.fill_open_slots(slots, picks) if picks is not None else E.plan_steps(slots)
    if any(s["approver_id"] == request.get("requester_id") for s in chosen):
        raise E.ApprovalError("기안자를 후속 결재자로 지정할 수 없습니다.")
    return chosen


def availability(app_db, request, candidate):
    """Check that current users can act on the configuration being connected."""
    users = [int(s["approver_id"]) for s in candidate["steps"] if s.get("approver_id")]
    if request.get("requester_id") in users:
        return "기안자가 포함된 후속 결재선입니다."
    if users:
        # users.status is CHAR(1), matching the login/directory active rule.
        active = {int(r["id"]) for r in q(app_db,
            "SELECT id FROM users WHERE id = ANY(%s) AND status = '1' AND is_active = TRUE", (users,))}
        if active != set(users):
            return "퇴사·비활성 결재자가 있습니다."
    return None


@contextmanager
def locked_request(app_db, request_id):
    with transaction(app_db) as tx:
        q(tx, "SELECT id FROM approval_requests WHERE id = %s FOR UPDATE", (request_id,))
        q(tx, "SELECT id FROM approval_request_steps WHERE request_id = %s ORDER BY step_order FOR UPDATE", (request_id,))
        yield tx


def cancel(app_db, request_id, *, actor_id=None, note=""):
    """Use the same lock order as approve, reassign and form editing."""
    from xgen_sdk.approval import store, notifier
    with locked_request(app_db, request_id) as tx:
        req = store.get(tx, request_id)
        now = store._now()
        if actor_id is not None:
            E.cancel(req, req["steps"], actor_id, now)
        elif req["status"] != E.PENDING:
            return req
        q(tx, "UPDATE approval_request_steps SET status = %s WHERE request_id = %s AND status IN (%s, %s)",
          (E.SKIPPED, request_id, E.WAITING, E.PENDING))
        q(tx, """UPDATE approval_requests SET status = %s, decided_at = %s, canceled_by = %s, cancel_note = %s
                 WHERE id = %s""", (E.CANCELED, now, "requester" if actor_id is not None else "system", note[:500] or None, request_id))
    out = store.get(app_db, request_id)
    store._settle_hooks(app_db, request_id, out)
    if actor_id is None:
        try:
            notifier.notify_system_canceled(app_db, out, list({s["approver_id"] for s in out["steps"]}), note=note)
        except Exception:
            store.logger.exception("후속 결재 회수 알림 실패 #%s", request_id)
    return store.get(app_db, request_id)


def decision(app_db, request_id, actor_id, action, note, line_id, picks, expected_step_order, expected_revision=None, followup_action=None):
    from xgen_sdk.approval import store, notifier, blocks
    now = store._now()
    with locked_request(app_db, request_id) as tx:
        req = store.get(tx, request_id)
        if expected_step_order is None or int(expected_step_order) != int(req["current_step_order"]):
            raise E.ApprovalError("결재 차례가 변경되었습니다. 새로고침 후 다시 확인하세요.")
        # Check actor, status and all required fields BEFORE appending anything.
        E.decide(req, req["steps"], actor_id, action, note, now, blocks=req["blocks"])
        config = req.get("followup_config") or {}
        needs = needs_selection(req)
        if followup_action not in (None, 'connect', 'finalize'):
            raise E.ApprovalError("올바른 후속 결재 처리를 선택하세요.")
        has_choice = line_id is not None or followup_action is not None or picks is not None or expected_revision is not None
        if has_choice and (not needs or action != E.APPROVED):
            raise E.ApprovalError("선행 결재선의 마지막 승인에서만 후속 결재선을 선택할 수 있습니다.")
        if needs and action == E.APPROVED:
            if followup_action == 'finalize':
                if not can_finalize(config):
                    raise E.ApprovalError("상신 당시 후속 결재가 필수인 문서입니다. 후속 결재선을 선택하세요.")
                if line_id is not None or picks is not None or expected_revision is not None:
                    raise E.ApprovalError("최종 승인과 후속 결재 요청을 동시에 선택할 수 없습니다.")
                actor = next(s for s in req['steps'] if s['step_order'] == req['current_step_order'])
                completion = {'decision': 'finalize', 'step_order': req['current_step_order'],
                              'decided_by': actor_id, 'decided_by_name': actor.get('full_name') or actor.get('username') or str(actor_id),
                              'decided_at': now.isoformat(), 'note': (note or '')[:500]}
                q(tx, 'UPDATE approval_requests SET followup_completion = %s WHERE id = %s',
                  (json.dumps(completion, ensure_ascii=False), request_id))
                # One append-only decision record, whether connected or ended.
                # The UI treats this as completion, never as connection history.
                q(tx, 'INSERT INTO approval_request_followups (request_id, transition) VALUES (%s, %s)',
                  (request_id, json.dumps(completion, ensure_ascii=False)))
            elif line_id is None:
                raise E.ApprovalError("최종 승인 또는 후속 결재 요청을 선택하세요." if can_finalize(config)
                                      else "허용된 후속 결재선을 선택하세요.")
        if needs and action == E.APPROVED and followup_action != 'finalize':
            if line_id not in allowed_ids(config):
                raise E.ApprovalError("허용된 후속 결재선을 선택하세요.")
            # The enclosing request transaction retains these locks until the
            # approval, appended steps, form snapshot and audit row all commit.
            with locked_configuration(tx):
                try:
                    candidate = load_candidate(tx, line_id)
                except E.ApprovalError:
                    raise FollowupChanged() from None
                if (not expected_revision or revision(candidate) != expected_revision
                        or candidate_error(candidate, config.get('source_line_id'))):
                    raise FollowupChanged()
            chosen = prepare_steps(req, candidate, picks)
            error = availability(tx, req, {**candidate, "steps": chosen})
            if error:
                raise E.ApprovalError(error)
            offset = max(s["step_order"] for s in req["steps"])
            for step in chosen:
                q(tx, """INSERT INTO approval_request_steps
                    (request_id, step_order, approver_id, status, segment_order)
                    VALUES (%s, %s, %s, 'waiting', 2)""",
                  (request_id, offset + step["step_order"], step["approver_id"]))
            # Draft stays the original draft; only followup reviewers' fields
            # are copied. Required draft fields were refused at configuration.
            for block in (candidate.get("form") or {}).get("blocks") or []:
                if int(block["step_index"]) == 0:
                    continue
                cfg = block.get("config")
                q(tx, """INSERT INTO approval_request_blocks
                    (request_id, step_order, block_type, label, config, required, sort_order)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                  (request_id, offset + int(block["step_index"]), blocks.normalize_block_type(block["block_type"]),
                   block["label"], json.dumps(cfg, ensure_ascii=False) if isinstance(cfg, dict) else cfg,
                   block["required"], block["sort_order"]))
            actor = next(s for s in req["steps"] if s["step_order"] == req["current_step_order"])
            selection = {"line_id": candidate["id"], "line_name": candidate["name"],
                         "start_step_order": offset + 1, "selected_by": actor_id,
                         "selected_by_name": actor.get("full_name") or actor.get("username") or str(actor_id),
                         "selected_at": now.isoformat(), "note": (note or "")[:500],
                         "form": candidate.get("form"), "steps": chosen,
                         "revision": expected_revision, "template_steps": candidate['steps']}
            q(tx, "UPDATE approval_requests SET followup_selection = %s WHERE id = %s",
              (json.dumps(selection, ensure_ascii=False, default=str), request_id))
            q(tx, "INSERT INTO approval_request_followups (request_id, transition) VALUES (%s, %s)",
              (request_id, json.dumps(selection, ensure_ascii=False, default=str)))
            req = store.get(tx, request_id)
        next_req, next_steps = E.decide(req, req["steps"], actor_id, action, note, now, blocks=req["blocks"])
        old_steps = {step["step_order"]: step for step in req["steps"]}
        for step in next_steps:
            if all(step.get(key) == old_steps[step["step_order"]].get(key) for key in ("status", "acted_at", "note")):
                continue  # Never rewrite a completed step.
            q(tx, """UPDATE approval_request_steps SET status = %s, acted_at = %s, note = %s
                WHERE request_id = %s AND step_order = %s""",
              (step["status"], step.get("acted_at"), step.get("note"), request_id, step["step_order"]))
        q(tx, """UPDATE approval_requests SET status = %s, current_step_order = %s, decided_at = %s
            WHERE id = %s""", (next_req["status"], next_req["current_step_order"], next_req.get("decided_at"), request_id))
    out = store.get(app_db, request_id)
    if out["status"] in (E.APPROVED, E.REJECTED):
        store._settle_hooks(app_db, request_id, out)
        out = store.get(app_db, request_id)
        try:
            notifier.notify_settled(app_db, out, note=note)
            if out.get("apply_error"):
                notifier.notify_apply_failed(app_db, out, str(out["apply_error"]))
        except Exception:
            store.logger.exception("후속 결재 완료 알림 실패 #%s", request_id)
    else:
        store._notify_turn(app_db, out)
    return out
