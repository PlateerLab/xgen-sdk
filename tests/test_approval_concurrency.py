"""동시성 — **같은 사람이 두 번 누르면.**

결재선은 한 줄이라([A] → [B] → [C]) "두 사람이 같은 차례에 동시에" 는 애초에
없다. 남는 경합은 하나다: **같은 사람의 중복 요청** — 더블클릭, 재전송,
네트워크 재시도.

실측으로 잡은 것(2026-09-11 검토): 마지막 사람이 두 번 누르면 읽고·계산하고·
덮어쓰는 구조라 **applier 가 두 번 돌았다.** 알림도 두 번 갔다.

그래서 두 쓰기를 compare-and-set 으로 바꿨다. 여기서는 두 요청이 **같은
스냅샷을 읽은 뒤** 차례로 쓰는 상황을 재현한다 — 읽기와 쓰기 사이에 상대가
끼어든 그 순간이다.
"""
from __future__ import annotations

import pytest

from xgen_sdk.approval import engine as E, registry, store
from test_approval_store import FakeDB


class RacyDB(FakeDB):
    """``get`` 한 번을 **낡은 스냅샷**으로 돌려주는 DB.

    두 번째 결재자가 `get` 할 때 첫 번째 결재자가 아직 쓰기 전인 상태를 보게
    만든다 — 실제 동시 요청에서 일어나는 바로 그 인터리빙이다.
    """

    def __init__(self):
        super().__init__()
        self.freeze_steps = None     # 이 스냅샷을 대신 돌려준다 (1회)

    def _run(self, s, p):
        if (self.freeze_steps is not None
                and s.startswith("SELECT s.step_order, s.approver_id, s.status")):
            frozen, self.freeze_steps = self.freeze_steps, None
            return frozen
        return super()._run(s, p)


@pytest.fixture
def db():
    d = RacyDB()
    for uid, name in ((1, "기안자"), (3, "팀장"), (4, "부장"), (9, "이사")):
        d.add_user(uid, name, name)
    return d


def _steps_now(db, request_id):
    import copy
    return copy.deepcopy([r for r in db.t["approval_request_steps"]
                          if r["request_id"] == request_id])




def test_double_clicking_the_last_approval_applies_only_once(db):
    """**옛 버그.** 같은 사람이 두 번 누르면 applier 가 두 번 돌았다.

    레지스트리 계약이 멱등을 요구하긴 하지만, 멱등하지 않은 applier 를 누가
    꽂는 순간 조용히 두 번 실행된다 — 계약에 기대지 않고 구조로 막는다.
    """
    calls = []
    registry.register_action("t.once", lambda payload, req: calls.append(1), label="한 번만")

    r = store.submit(db, requester_id=1, title="증설", action_type="t.once",
                     steps=[{"approver_id": 3, "step_order": 1}])
    rid = r["id"]
    before = _steps_now(db, rid)

    store.decide(db, rid, actor_id=3, action="approved")

    # 두 번째 요청이 첫 번째가 쓰기 전 상태를 읽는다
    db.freeze_steps = [{**s, "username": "x", "full_name": "x"} for s in before]
    # 두 겹이 막는다: 건이 이미 종결됐다는 판정(앞선 겹), 그리고 그걸 통과해도
    # 내 칸이 더는 pending 이 아니라는 조건부 쓰기(뒷 겹). 어느 쪽이 먼저
    # 잡든 **applier 가 두 번 돌지 않는 것**이 요점이다.
    with pytest.raises(E.ApprovalError, match="이미"):
        store.decide(db, rid, actor_id=3, action="approved")

    assert calls == [1], f"applier 가 {len(calls)}번 돌았다"


def test_a_racing_double_click_does_not_send_the_notification_twice(db):
    """알림도 마찬가지다 — 같은 결재로 두 번 울리면 사람은 두 건인 줄 안다."""
    sent = []

    class Counting(RacyDB):
        def _run(self, s, p):
            if s.startswith("INSERT INTO user_notifications"):
                sent.append(p[1])          # template_id
                return []
            if s.startswith("SELECT link_url FROM notification_message_templates"):
                return []                  # 템플릿 표가 비어 있다 → 코드 기본값
            return super()._run(s, p)

    d = Counting()
    for uid, name in ((1, "기안자"), (3, "팀장")):
        d.add_user(uid, name, name)

    r = store.submit(d, requester_id=1, title="증설",
                     steps=[{"approver_id": 3, "step_order": 1}])
    rid = r["id"]
    before = _steps_now(d, rid)
    sent.clear()

    store.decide(d, rid, actor_id=3, action="approved")
    d.freeze_steps = [{**s, "username": "x", "full_name": "x"} for s in before]
    with pytest.raises(E.ApprovalError):
        store.decide(d, rid, actor_id=3, action="approved")

    assert sent.count("APV-OK") == 1, f"승인 완료 알림이 {sent.count('APV-OK')}번 갔다"


def test_a_late_request_cannot_overwrite_a_finished_outcome(db):
    """이미 끝난 건에 늦게 도착한 요청이 결론을 덮으면 안 된다.

    1차가 거절해 종결된 뒤, 그 사실을 못 본 낡은 요청이 도착하는 상황이다
    (느린 네트워크·재시도). 결론은 '거절' 하나로 남아야 한다.
    """
    r = store.submit(db, requester_id=1, title="증설",
                     steps=[{"approver_id": 3, "step_order": 1},
                            {"approver_id": 4, "step_order": 2}])
    rid = r["id"]
    before = _steps_now(db, rid)

    store.decide(db, rid, actor_id=3, action="rejected", note="반대")

    db.freeze_steps = [{**s, "username": "x", "full_name": "x"} for s in before]
    with pytest.raises(E.ApprovalError):
        store.decide(db, rid, actor_id=4, action="approved")

    out = store.get(db, rid)
    assert out["status"] == "rejected", f"거절이 승인에 덮였다 ({out['status']})"


# ── engine.advance 자체 ───────────────────────────────────────────────


class TestAdvance:
    def test_it_waits_while_this_person_has_not_pressed(self):
        steps = [{"step_order": 1, "status": "pending", "approver_id": 3},
                 {"step_order": 2, "status": "waiting", "approver_id": 4}]
        assert E.advance(steps, 1, "T") == ("pending", 1, None)

    def test_it_moves_when_the_order_is_complete(self):
        steps = [{"step_order": 1, "status": "approved", "approver_id": 3},
                 {"step_order": 2, "status": "waiting", "approver_id": 4}]
        assert E.advance(steps, 1, "T") == ("pending", 2, None)

    def test_the_last_order_finishes_it(self):
        steps = [{"step_order": 1, "status": "approved", "approver_id": 3}]
        assert E.advance(steps, 1, "T") == ("approved", 1, "T")

    def test_any_rejection_wins(self):
        steps = [{"step_order": 1, "status": "approved", "approver_id": 3},
                 {"step_order": 2, "status": "rejected", "approver_id": 4},
                 {"step_order": 3, "status": "waiting", "approver_id": 9}]
        assert E.advance(steps, 2, "T") == ("rejected", 2, "T")
