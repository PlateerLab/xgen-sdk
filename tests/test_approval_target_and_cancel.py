"""대상·중복·시스템 회수 — **결재가 실제 행위에 붙을 때** 생기는 것들.

지금까지 결재는 "승인 자체가 결론" 인 자유 결재만 다뤘다. 배포·컬렉션 생성
같은 실제 행위에 붙으면 세 가지가 새로 생긴다:

  1. **대상** — 이 결재는 무엇에 대한 것인가. 같은 대상에 두 건이 떠 있으면
     어느 쪽 승인이 그 대상을 바꾼 것인지 아무도 답할 수 없다.
  2. **올릴 수 있는 사람** — 사람이 payload 를 손으로 적어 올릴 수 있으면
     남의 워크플로우를 배포시키는 길이 된다.
  3. **전제가 사라지는 일** — 결재 도중 대상이 수정되면, 결재자들이 본 그
     정의는 더 이상 존재하지 않는다. 그대로 승인되면 **아무도 본 적 없는
     것이 배포된다.**
"""
from __future__ import annotations

import pytest

from xgen_sdk.approval import catalog, engine as E, registry, store
from test_approval_notifications import NotifyDB


@pytest.fixture
def db():
    d = NotifyDB()
    for uid, name in ((1, "기안자"), (3, "팀장"), (4, "부장"), (9, "제삼자")):
        d.add_user(uid, name, name)
    return d


def _submit(db, **kw):
    kw.setdefault("requester_id", 1)
    kw.setdefault("title", "배포 요청")
    kw.setdefault("steps", [{"approver_id": 3, "step_order": 1}])
    return store.submit(db, **kw)


# ── 1. 한 대상에 한 건 ────────────────────────────────────────────────


def test_a_second_request_for_the_same_target_is_refused(db):
    _submit(db, action_type="agent.deploy", target_ref="workflow:abc")
    with pytest.raises(E.ApprovalError, match="진행 중인 결재"):
        _submit(db, action_type="agent.deploy", target_ref="workflow:abc")


def test_the_refusal_names_the_request_so_the_user_can_open_it(db):
    r = _submit(db, action_type="agent.deploy", target_ref="workflow:abc")
    with pytest.raises(E.ApprovalError) as err:
        _submit(db, action_type="agent.deploy", target_ref="workflow:abc")
    assert f"#{r['id']}" in str(err.value), "어느 건인지 모르면 사용자가 할 수 있는 게 없다"


def test_a_different_target_is_fine(db):
    _submit(db, action_type="agent.deploy", target_ref="workflow:abc")
    _submit(db, action_type="agent.deploy", target_ref="workflow:def")


def test_the_same_target_under_a_different_action_is_fine(db):
    """같은 워크플로우라도 '배포' 와 '공유' 는 다른 결정이다."""
    _submit(db, action_type="agent.deploy", target_ref="workflow:abc")
    _submit(db, action_type="cloud.share", target_ref="workflow:abc")


def test_once_the_first_one_is_settled_a_new_one_can_go_up(db):
    """거절됐으면 고쳐서 다시 올릴 수 있어야 한다 — 아니면 영영 못 올린다."""
    r = _submit(db, action_type="agent.deploy", target_ref="workflow:abc")
    store.decide(db, r["id"], actor_id=3, action="rejected", note="사유")
    _submit(db, action_type="agent.deploy", target_ref="workflow:abc")


def test_requests_without_a_target_do_not_collide(db):
    """자유 결재는 대상이 없다 — 몇 건이든 올라간다."""
    _submit(db, title="증설 1")
    _submit(db, title="증설 2")


def test_the_pending_one_can_be_found_for_the_screen(db):
    """화면이 "결재 진행 중" 을 보여 주려면 그 건을 찾을 수 있어야 한다."""
    r = _submit(db, action_type="agent.deploy", target_ref="workflow:abc")
    found = store.find_pending_for_target(db, "agent.deploy", "workflow:abc")
    assert found and found["id"] == r["id"]
    assert store.find_pending_for_target(db, "agent.deploy", "workflow:zzz") is None


def test_a_settled_request_is_not_found_as_pending(db):
    r = _submit(db, action_type="agent.deploy", target_ref="workflow:abc")
    store.decide(db, r["id"], actor_id=3, action="approved")
    assert store.find_pending_for_target(db, "agent.deploy", "workflow:abc") is None


# ── 2. 사람이 직접 올릴 수 있는 것 ────────────────────────────────────


def test_a_gated_action_cannot_be_raised_from_the_user_api(db):
    """**남의 워크플로우를 배포시키는 길을 막는다.**

    화면에서 결재를 올릴 때 action_type 과 payload 를 그대로 받는다. 게이트
    행위를 그 문으로 들일 수 있으면, 아무나 '배포' 결재에 남의 워크플로우 id 를
    적어 올리고 승인 한 번으로 그것을 외부에 열 수 있다.
    """
    with pytest.raises(E.ApprovalError, match="직접 올릴 수 없습니다"):
        _submit(db, action_type="agent.deploy", target_ref="workflow:abc",
                via_user_api=True)


def test_a_free_action_can_be_raised_from_the_user_api(db):
    out = _submit(db, action_type=catalog.GENERIC, via_user_api=True)
    assert out["status"] == "pending"


def test_the_feature_code_can_still_raise_a_gated_action(db):
    """기능 쪽 코드는 자기 맥락에서 올린다 — 그쪽은 대상을 알고 있다."""
    out = _submit(db, action_type="agent.deploy", target_ref="workflow:abc")
    assert out["action_type"] == "agent.deploy"
    assert out["target_ref"] == "workflow:abc"


# ── 3. 시스템 회수 ────────────────────────────────────────────────────


def test_the_system_can_withdraw_even_after_someone_approved(db):
    """사람은 못 하지만 시스템은 해야 한다.

    결재 도중 워크플로우가 수정되면 결재자들이 본 정의는 사라진다. 그대로
    승인이 이어지면 아무도 본 적 없는 것이 배포된다.
    """
    r = _submit(db, action_type="agent.deploy", target_ref="workflow:abc",
                steps=[{"approver_id": 3, "step_order": 1},
                       {"approver_id": 4, "step_order": 2}])
    store.decide(db, r["id"], actor_id=3, action="approved")

    with pytest.raises(E.ApprovalError, match="회수할 수 없습니다"):
        store.cancel(db, r["id"], actor_id=1)          # 사람은 못 한다

    out = store.system_cancel(db, r["id"], note="워크플로우가 수정되었습니다")
    assert out["status"] == "canceled"


def test_a_system_withdrawal_says_who_and_why(db):
    r = _submit(db, action_type="agent.deploy", target_ref="workflow:abc")
    out = store.system_cancel(db, r["id"], note="워크플로우가 수정되었습니다")
    assert out["canceled_by"] == "system"
    assert "수정" in out["cancel_note"]


def test_a_human_withdrawal_is_marked_as_the_requester(db):
    r = _submit(db)
    out = store.cancel(db, r["id"], actor_id=1)
    assert out["canceled_by"] == "requester"


def test_an_approval_already_given_is_not_erased_by_the_withdrawal(db):
    """회수 전에 누가 승인했었나 — 나중에 답할 수 있어야 한다."""
    r = _submit(db, steps=[{"approver_id": 3, "step_order": 1},
                           {"approver_id": 4, "step_order": 2}])
    store.decide(db, r["id"], actor_id=3, action="approved")
    out = store.system_cancel(db, r["id"], note="대상이 사라졌습니다")

    by = {s["approver_id"]: s["status"] for s in out["steps"]}
    assert by[3] == "approved", "이미 일어난 승인이 지워졌다"
    assert by[4] == "skipped"


def test_the_people_who_already_acted_are_told(db):
    """내가 누른 승인이 **말없이 사라지면** 다음에 같은 제목이 올라왔을 때
    "아까 그거 아닌가" 를 묻게 된다."""
    from xgen_sdk.approval import notifier

    r = _submit(db, steps=[{"approver_id": 3, "step_order": 1},
                           {"approver_id": 4, "step_order": 2}])
    store.decide(db, r["id"], actor_id=3, action="approved")
    db.notifications.clear()
    store.system_cancel(db, r["id"], note="워크플로우가 수정되었습니다")

    told = {n["user_id"] for n in db.notifications
            if n["template_id"] == notifier.TEMPLATE_CANCELED}
    assert 1 in told, "기안자가 모른다"
    assert 3 in told, "이미 승인한 사람이 모른다"
    assert "수정" in next(n["message"] for n in db.notifications
                        if n["template_id"] == notifier.TEMPLATE_CANCELED)


def test_a_finished_request_is_left_alone(db):
    """이미 승인 완료된 건을 뒤늦게 회수하면 **적용된 것이 기록만 사라진다.**"""
    r = _submit(db)
    store.decide(db, r["id"], actor_id=3, action="approved")
    out = store.system_cancel(db, r["id"], note="늦게 도착한 회수")
    assert out["status"] == "approved"
    assert out["canceled_by"] is None


def test_withdrawing_frees_the_target_for_a_new_request(db):
    r = _submit(db, action_type="agent.deploy", target_ref="workflow:abc")
    store.system_cancel(db, r["id"], note="수정됨")
    _submit(db, action_type="agent.deploy", target_ref="workflow:abc")


def test_a_withdrawal_runs_the_clean_up_hook(db):
    """올려 두고 승인 뒤에 보이게 하는 행위는, 회수되면 치워야 한다."""
    cleaned = []
    registry.register_action("t.staged", lambda p, q: None,
                             reject=lambda p, q: cleaned.append(q["id"]), label="스테이징")
    r = _submit(db, action_type="t.staged")
    store.system_cancel(db, r["id"], note="대상이 사라졌습니다")
    assert cleaned == [r["id"]], "치우기 훅이 안 돌았다 — 올려 둔 것이 남는다"


def test_a_rejection_runs_the_clean_up_hook_too(db):
    cleaned = []
    registry.register_action("t.staged2", lambda p, q: None,
                             reject=lambda p, q: cleaned.append(q["id"]), label="스테이징")
    r = _submit(db, action_type="t.staged2")
    store.decide(db, r["id"], actor_id=3, action="rejected", note="반려")
    assert cleaned == [r["id"]]


def test_an_approval_does_not_run_the_clean_up_hook(db):
    applied, cleaned = [], []
    registry.register_action("t.staged3", lambda p, q: applied.append(q["id"]),
                             reject=lambda p, q: cleaned.append(q["id"]), label="스테이징")
    r = _submit(db, action_type="t.staged3")
    store.decide(db, r["id"], actor_id=3, action="approved")
    assert applied == [r["id"]] and cleaned == []


# ── 4. 적용은 **소유 서비스**가 한다 ──────────────────────────────────


def test_core_does_not_run_a_hook_it_does_not_own(db):
    """지식 컬렉션을 실제로 여는 코드는 documents 에 있다. core 가 그것을
    부를 수는 없으므로, 훅을 안 돌리고 표식을 남긴다."""
    r = _submit(db, action_type="collection.create", target_ref="collection:사규")
    store.decide(db, r["id"], actor_id=3, action="approved")

    out = store.get(db, r["id"])
    assert out["status"] == "approved"
    assert out["applied_at"] is None, "훅도 없이 '적용됨' 으로 찍혔다"


def test_the_owning_service_picks_it_up(db):
    r = _submit(db, action_type="collection.create", target_ref="collection:사규")
    store.decide(db, r["id"], actor_id=3, action="approved")

    waiting = store.claim_settlements(db, catalog.owned_by(catalog.OWNER_WORKFLOW))
    assert [w["id"] for w in waiting] == [r["id"]]

    done = []
    registry.register_action("collection.create", lambda p, q: done.append(q["id"]))
    store.settle(db, r["id"])

    assert done == [r["id"]]
    assert store.get(db, r["id"])["applied_at"] is not None


def test_a_settled_request_is_not_claimed_again(db):
    r = _submit(db, action_type="collection.create", target_ref="collection:사규")
    store.decide(db, r["id"], actor_id=3, action="approved")
    registry.register_action("collection.create", lambda p, q: None)
    store.settle(db, r["id"])
    assert store.claim_settlements(db, catalog.owned_by(catalog.OWNER_WORKFLOW)) == []


def test_a_still_pending_request_is_not_claimed(db):
    _submit(db, action_type="collection.create", target_ref="collection:사규",
            steps=[{"approver_id": 3, "step_order": 1},
                   {"approver_id": 4, "step_order": 2}])
    assert store.claim_settlements(db, catalog.owned_by(catalog.OWNER_WORKFLOW)) == []


def test_a_worker_only_claims_its_own_actions(db):
    """남의 것을 가져가면 훅이 없어서 '적용됨' 으로 찍고 아무 일도 안 일어난다."""
    r = _submit(db, action_type="agent.deploy", target_ref="workflow:abc")
    store.decide(db, r["id"], actor_id=3, action="approved")
    assert store.claim_settlements(db, catalog.owned_by(catalog.OWNER_WORKFLOW)) == []


def test_a_rejected_request_is_claimed_for_clean_up(db):
    r = _submit(db, action_type="collection.upload", target_ref="doc:1")
    store.decide(db, r["id"], actor_id=3, action="rejected", note="반려")
    waiting = store.claim_settlements(db, catalog.owned_by(catalog.OWNER_WORKFLOW))
    assert [w["status"] for w in waiting] == ["rejected"]


def test_the_worker_tells_the_requester_when_applying_fails(db):
    """"승인은 됐는데 아무 일도 안 일어난" 상태를 아무도 모르면 안 된다."""
    from xgen_sdk.approval import notifier

    r = _submit(db, action_type="collection.create", target_ref="collection:사규")
    store.decide(db, r["id"], actor_id=3, action="approved")
    registry.register_action(
        "collection.create",
        lambda p, q: (_ for _ in ()).throw(RuntimeError("컬렉션 서비스 없음")))
    db.notifications.clear()
    store.settle(db, r["id"])

    out = store.get(db, r["id"])
    assert "컬렉션 서비스 없음" in (out["apply_error"] or "")
    fail = [n for n in db.notifications if n["template_id"] == notifier.TEMPLATE_APPLY_FAIL]
    assert len(fail) == 1 and fail[0]["user_id"] == 1
