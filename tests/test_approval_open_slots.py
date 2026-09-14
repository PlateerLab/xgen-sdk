"""결재선의 **임의 차례** — 사람을 박지 않고 "올리는 사람이 고른다" 로 두는 자리.

양식이 결재자 둘을 요구해도, 두 번째 사람이 건마다 다른 조직이 있다. 그 자리를
특정인으로 박으면 결재선을 사람 수만큼 만들어야 한다. 그래서 줄은 이렇게 설 수 있다
(2026-09-14 사용자 지시):

    A → B        사람이 모두 정해진 줄
    임의 → B     첫 차례는 올리는 사람이 고른다
    B → 임의     마지막 차례는 올리는 사람이 고른다

지키는 것:
  1. 템플릿은 임의 차례를 가질 수 있다 — 여럿이어도 된다. 사람은 여전히 두 번 못 선다.
  2. **올라가는 결재에는 임의 차례가 없다** — 줄만 들고 오면 거절하고(빈 차례로 올라가면
     거기서 영영 멈춘다), 결재선 모달이 그 줄로 열리게 ApprovalLineRequired 를 낸다.
  3. **고정된 줄**에서도 임의 차례는 요청자가 고른다. 사람이 정해진 차례와 차례 수는
     못 바꾼다.
"""
from __future__ import annotations

import pytest

from xgen_sdk.approval import engine as E, policy, store
from xgen_sdk.approval.engine import ApprovalError, ApprovalLineRequired
from test_approval_line_management import LineDB
from test_approval_policy import PolicyDB
from test_approval_store import FakeDB

OPEN = None


def _slots(*approvers):
    return [{"approver_id": a, "step_order": i + 1} for i, a in enumerate(approvers)]


# ── 1. 템플릿 ─────────────────────────────────────────────────────────


class TestLineSlots:
    @pytest.mark.parametrize("line", [(1, 2), (OPEN, 2), (2, OPEN), (OPEN, OPEN), (1, OPEN, 3)])
    def test_every_shape_the_user_asked_for_is_a_line(self, line):
        planned = E.plan_line_slots(_slots(*line))
        assert [s["approver_id"] for s in planned] == list(line)
        assert [s["step_order"] for s in planned] == list(range(1, len(line) + 1))

    def test_an_empty_string_is_an_open_slot_too(self):
        """화면이 빈 선택을 문자열로 보내도 임의 차례다 — 거절하면 이유를 알 수 없다."""
        assert E.plan_line_slots([{"approver_id": "", "step_order": 1}])[0]["approver_id"] is None

    def test_a_person_still_cannot_stand_twice(self):
        with pytest.raises(ApprovalError, match="두 번"):
            E.plan_line_slots(_slots(1, OPEN, 1))

    def test_an_empty_line_is_not_a_line(self):
        with pytest.raises(ApprovalError):
            E.plan_line_slots([])

    def test_a_request_line_never_accepts_an_open_slot(self):
        """올라가는 결재의 규칙(plan_steps)은 그대로 사람만 받는다."""
        with pytest.raises(ApprovalError):
            E.plan_steps(_slots(1, OPEN))

    def test_open_slot_orders(self):
        assert E.open_slot_orders(_slots(OPEN, 2, OPEN)) == [1, 3]
        assert E.open_slot_orders(_slots(1, 2)) == []


class TestFillOpenSlots:
    def test_the_open_slot_takes_the_chosen_person(self):
        assert E.fill_open_slots(_slots(1, OPEN), _slots(1, 5)) == _slots(1, 5)
        assert E.fill_open_slots(_slots(OPEN, 2), _slots(7, 2)) == _slots(7, 2)

    def test_a_fixed_person_cannot_be_swapped(self):
        with pytest.raises(ApprovalError, match="관리자가 정한 결재자"):
            E.fill_open_slots(_slots(1, OPEN), _slots(5, 1))

    def test_the_number_of_turns_is_the_admins(self):
        with pytest.raises(ApprovalError, match="2차례"):
            E.fill_open_slots(_slots(1, OPEN), _slots(1, 5, 6))
        with pytest.raises(ApprovalError, match="2차례"):
            E.fill_open_slots(_slots(1, OPEN), _slots(1))

    def test_every_open_slot_needs_a_person(self):
        with pytest.raises(ApprovalError, match="모두 지정"):
            E.fill_open_slots(_slots(1, OPEN), _slots(1, OPEN))

    def test_the_chosen_person_cannot_be_someone_already_on_the_line(self):
        with pytest.raises(ApprovalError, match="두 번"):
            E.fill_open_slots(_slots(1, OPEN), _slots(1, 1))


# ── 템플릿 저장 ───────────────────────────────────────────────────────


@pytest.fixture()
def line_db():
    d = LineDB()
    for uid, name in ((1, "kim"), (2, "lee"), (3, "park"), (9, "admin")):
        d.add_user(uid, name, name.upper())
    return d


class TestStore:
    def test_an_open_slot_is_saved_and_read_back_as_empty(self, line_db):
        made = store.create_line(line_db, name="임의 → 이", description="", owner_id=9,
                                 is_shared=True, steps=_slots(OPEN, 2))
        line = store.get_line(line_db, made["id"])
        assert [(s["step_order"], s["approver_id"]) for s in line["steps"]] == [(1, None), (2, 2)]
        assert line["steps"][0]["username"] is None

    def test_editing_can_turn_a_person_into_an_open_slot(self, line_db):
        made = store.create_line(line_db, name="김 → 이", description="", owner_id=9,
                                 is_shared=True, steps=_slots(1, 2))
        out = store.update_line(line_db, made["id"], steps=_slots(1, OPEN))
        assert [s["approver_id"] for s in out["steps"]] == [1, None]


# ── 2. 올리기 ─────────────────────────────────────────────────────────


@pytest.fixture()
def fake():
    d = FakeDB()
    for uid, name in ((1, "kim"), (2, "lee"), (5, "choi"), (9, "admin")):
        d.add_user(uid, name)
    return d


def _line(db, approvers):
    return store.create_line(db, name="L", description="", owner_id=9, is_shared=True,
                             steps=_slots(*approvers))["id"]


class TestSubmit:
    def test_a_line_with_an_open_slot_cannot_be_submitted_as_is(self, fake):
        lid = _line(fake, (1, OPEN))
        with pytest.raises(ApprovalError, match="임의"):
            store.submit(fake, requester_id=9, title="t", line_id=lid)

    def test_the_line_goes_up_once_the_open_slot_has_a_person(self, fake):
        lid = _line(fake, (1, OPEN))
        out = store.submit(fake, requester_id=9, title="t", line_id=lid, steps=_slots(1, 5))
        assert [s["approver_id"] for s in out["steps"]] == [1, 5]
        assert out["line_id"] == lid

    def test_a_line_of_people_still_goes_up_by_itself(self, fake):
        lid = _line(fake, (1, 2))
        out = store.submit(fake, requester_id=9, title="t", line_id=lid)
        assert [s["approver_id"] for s in out["steps"]] == [1, 2]


# ── 3. 정책 — 고정된 줄, 기본 결재선 ───────────────────────────────────


@pytest.fixture()
def pdb():
    d = PolicyDB()
    for uid, name in ((1, "kim"), (2, "lee"), (5, "choi"), (9, "admin")):
        d.add_user(uid, name)
    return d


class TestResolveLine:
    def test_a_locked_line_with_an_open_slot_asks_for_the_person(self, pdb):
        """아무것도 안 들고 왔으면 실패가 아니라 한 단계 덜 온 것 — 모달이 그 줄로 열린다."""
        lid = _line(pdb, (1, OPEN))
        policy.set_policy(pdb, "collection.create", actor_id=9, required=True,
                          default_line_id=lid, line_locked=True)
        with pytest.raises(ApprovalLineRequired) as err:
            policy.resolve_line(pdb, "collection.create")
        assert err.value.default_line_id == lid

    def test_a_locked_line_takes_only_the_open_slots(self, pdb):
        lid = _line(pdb, (1, OPEN))
        policy.set_policy(pdb, "collection.create", actor_id=9, required=True,
                          default_line_id=lid, line_locked=True)
        assert policy.resolve_line(pdb, "collection.create", line_id=lid, steps=_slots(1, 5)) \
            == (lid, _slots(1, 5))
        with pytest.raises(ApprovalError, match="관리자가 정한 결재자"):
            policy.resolve_line(pdb, "collection.create", steps=_slots(5, 1))
        with pytest.raises(ApprovalError, match="차례를 추가하거나 뺄 수 없습니다"):
            policy.resolve_line(pdb, "collection.create", steps=_slots(1, 5, 2))

    def test_a_locked_line_of_people_still_refuses_steps(self, pdb):
        """임의 차례가 없는 고정 결재선은 예전 그대로 — 고를 수 있는 것이 없다."""
        lid = _line(pdb, (1, 2))
        policy.set_policy(pdb, "collection.create", actor_id=9, required=True,
                          default_line_id=lid, line_locked=True)
        assert policy.resolve_line(pdb, "collection.create") == (lid, None)
        with pytest.raises(ApprovalError, match="고정"):
            policy.resolve_line(pdb, "collection.create", steps=_slots(1, 2))

    def test_a_default_line_with_an_open_slot_is_not_used_as_is(self, pdb):
        lid = _line(pdb, (OPEN, 2))
        policy.set_policy(pdb, "collection.create", actor_id=9, required=True, default_line_id=lid)
        with pytest.raises(ApprovalLineRequired) as err:
            policy.resolve_line(pdb, "collection.create")
        assert err.value.default_line_id == lid
        with pytest.raises(ApprovalLineRequired):
            policy.resolve_line(pdb, "collection.create", line_id=lid)
        # 사람을 세워 오면 그대로 간다 — 고정이 아니라 요청자가 무엇이든 고를 수 있다.
        assert policy.resolve_line(pdb, "collection.create", line_id=lid, steps=_slots(5, 1)) \
            == (lid, _slots(5, 1))
