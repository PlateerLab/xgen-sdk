"""결재 뒤처리는 파드가 여럿이어도 **한 번** 돈다.

2026-09-23 안정성 감사 F15.

무엇이 있었나
-------------
결정은 core 에서 나고 적용은 소유 서비스(workflow·documents)의 워커가 한다. 그 워커는
파드마다 돈다. ``finish`` 는 **훅을 먼저 돌리고** ``applied_at IS NULL`` 조건부로 찍기만
했다 — 두 파드의 워커가 같은 건을 동시에 집으면 둘 다 훅을 돌리고, 찍기만 하나가 이긴다.
레지스트리 계약이 멱등을 요구하긴 하지만, 멱등하지 않은 훅을 누가 꽂는 순간 조용히 두 번
적용된다.

고친 뒤
-------
훅을 돌리기 **전에** 조건부 쓰기로 선점한다(``apply_claimed_at``). 이긴 쪽만 훅을 돌린다.
맡은 쪽이 훅 도중 죽으면 선점이 낡은 뒤 다른 워커가 잇는다(승인됐는데 아무 일도 안 일어난
채로 남지 않는다).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from xgen_sdk.approval import registry, store
from xgen_sdk.approval.models import ApprovalRequest
from test_approval_store import FakeDB


@pytest.fixture
def db():
    d = FakeDB()
    for uid, name in ((1, "기안자"), (3, "팀장")):
        d.add_user(uid, name, name)
    return d


def _decided_elsewhere(db, action_type: str) -> int:
    """core 가 승인만 하고 적용은 소유 서비스에 남긴 건 — 워커가 집을 상태."""
    r = store.submit(db, requester_id=1, title="증설", action_type=action_type,
                     steps=[{"approver_id": 3, "step_order": 1}])
    row = next(x for x in db.t["approval_requests"] if x["id"] == r["id"])
    row.update(status="approved", decided_at="T1", applied_at=None)
    return r["id"]


def test_a_second_worker_does_not_apply_while_the_first_is_applying(db):
    """두 파드의 워커가 같은 건을 **같은 스냅샷으로** 집었다 — 하나만 적용한다."""
    calls = []
    rid = None

    def _apply(payload, req):
        calls.append("A")
        # A 가 적용하는 도중에 B 가 같은 건을 집는다(같은 낡은 스냅샷).
        err = store.finish(db, rid, "t.pods.once", stale_b, "approved")
        assert err == ""

    registry.register_action("t.pods.once", _apply, label="파드 둘")
    rid = _decided_elsewhere(db, "t.pods.once")
    stale_a = store.get(db, rid)
    stale_b = store.get(db, rid)
    assert stale_a["applied_at"] is None and stale_b["applied_at"] is None

    assert store.finish(db, rid, "t.pods.once", stale_a, "approved") == ""
    assert calls == ["A"], f"훅이 {len(calls)}번 돌았다"
    assert store.get(db, rid)["applied_at"] is not None


def test_a_worker_that_died_mid_apply_is_taken_over_after_the_lease(db):
    calls = []
    registry.register_action("t.pods.crash", lambda p, r: calls.append(1), label="죽은 워커")
    rid = _decided_elsewhere(db, "t.pods.crash")

    assert store._claim_apply(db, rid) is True      # A 가 맡고 — 훅 도중 죽었다
    loaded = store.get(db, rid)
    store.finish(db, rid, "t.pods.crash", loaded, "approved")
    assert calls == [], "맡은 워커가 살아 있을 수 있는데 끼어들었다"

    row = next(x for x in db.t["approval_requests"] if x["id"] == rid)
    row["apply_claimed_at"] = store._now() - timedelta(seconds=store.APPLY_LEASE_S + 1)
    store.finish(db, rid, "t.pods.crash", store.get(db, rid), "approved")
    assert calls == [1], "선점이 낡았는데도 아무도 잇지 않는다 — 승인이 적용되지 않은 채 남는다"
    assert store.get(db, rid)["applied_at"] is not None


def test_a_finished_request_is_never_claimed_again(db):
    registry.register_action("t.pods.done", lambda p, r: None, label="끝난 건")
    rid = _decided_elsewhere(db, "t.pods.done")
    store.finish(db, rid, "t.pods.done", store.get(db, rid), "approved")
    assert store._claim_apply(db, rid) is False


def test_settle_goes_through_the_claim(db):
    calls = []
    registry.register_action("t.pods.settle", lambda p, r: calls.append(1), label="settle")
    rid = _decided_elsewhere(db, "t.pods.settle")
    store._claim_apply(db, rid)                      # 다른 파드가 맡고 있다
    store.settle(db, rid)
    assert calls == []


def test_before_the_column_exists_it_still_applies(db, caplog):
    """위성 서비스가 core 보다 먼저 새 SDK 로 떠서 컬럼이 아직 없을 때 — 예전처럼 적용한다."""

    class NoColumn(FakeDB):
        def _run(self, s, p):
            if s.startswith("UPDATE approval_requests SET apply_claimed_at"):
                raise RuntimeError('column "apply_claimed_at" does not exist')
            return super()._run(s, p)

    d = NoColumn()
    for uid, name in ((1, "기안자"), (3, "팀장")):
        d.add_user(uid, name, name)
    calls = []
    registry.register_action("t.pods.nocol", lambda p, r: calls.append(1), label="컬럼 없음")
    rid = _decided_elsewhere(d, "t.pods.nocol")
    assert store._claim_apply(d, rid) is None
    store.finish(d, rid, "t.pods.nocol", store.get(d, rid), "approved")
    assert calls == [1]
    assert store.get(d, rid)["applied_at"] is not None


def test_the_schema_carries_the_claim():
    assert ApprovalRequest().get_schema().get("apply_claimed_at") == "TIMESTAMP"
