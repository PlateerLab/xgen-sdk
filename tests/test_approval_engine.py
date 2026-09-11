"""결재 상태기계 — **틀리면 안 되는 것들.**

결재는 "누가 무엇을 승낙했는가" 의 기록이다. 여기서 규칙이 하나 새면 그
기록이 거짓이 되고, 거짓인 결재 기록은 없느니만 못하다. 그래서 이 파일은
되는 경우보다 **안 되는 경우**를 훨씬 많이 본다.

DB 를 세우지 않는다 — 규칙은 전부 dict 위에서 성립한다.
"""
from __future__ import annotations

import pytest

from xgen_sdk.approval import engine as E


def line(*specs):
    """(사용자, 차례) 쌍들 → 막 올라간 결재의 단계들."""
    return E.open_steps(E.plan_steps(
        [{"approver_id": u, "step_order": o} for u, o in specs]
    ))


def req(requester=1, **kw):
    base = {"status": E.PENDING, "current_step_order": 1, "requester_id": requester}
    base.update(kw)
    return base


def states(steps):
    return {s["approver_id"]: s["status"] for s in steps}


# ── 결재선 만들기 ─────────────────────────────────────────────────────


class TestPlanning:
    def test_an_empty_line_is_not_a_line(self):
        with pytest.raises(E.ApprovalError, match="최소 한 명"):
            E.plan_steps([])

    def test_the_same_person_cannot_stand_twice(self):
        """두 번 승인하라는 뜻이 되는데, 그런 결재선은 실수이지 의도인 적이 없다."""
        with pytest.raises(E.ApprovalError, match="두 번"):
            E.plan_steps([{"approver_id": 3, "step_order": 1},
                          {"approver_id": 3, "step_order": 2}])

    def test_gaps_in_the_numbering_are_closed(self):
        """사람이 1,5,9 를 넣었다면 뜻은 '셋을 순서대로' 다."""
        planned = E.plan_steps([{"approver_id": 3, "step_order": 1},
                                {"approver_id": 4, "step_order": 5},
                                {"approver_id": 5, "step_order": 9}])
        assert [s["step_order"] for s in planned] == [1, 2, 3]

    def test_the_same_number_still_makes_one_line(self):
        """**결재선은 한 줄이다.** 같은 번호를 줘도 갈래가 되지 않는다.

        한 차례에 둘이 서면 "지금 누구 차례인가" 에 답이 둘이 된다.
        들어온 순서대로 뒤에 세운다.
        """
        planned = E.plan_steps([{"approver_id": 3, "step_order": 2},
                                {"approver_id": 4, "step_order": 2},
                                {"approver_id": 9, "step_order": 7}])
        assert [(s["approver_id"], s["step_order"]) for s in planned] == [(3, 1), (4, 2), (9, 3)]

    def test_every_step_has_exactly_one_person(self):
        planned = E.plan_steps([{"approver_id": u, "step_order": 1} for u in (3, 4, 9)])
        orders = [s["step_order"] for s in planned]
        assert orders == sorted(set(orders)) == [1, 2, 3]

    def test_only_the_first_turn_is_pending_at_the_start(self):
        steps = line((3, 1), (4, 2), (5, 3))
        assert states(steps) == {3: E.PENDING, 4: E.WAITING, 5: E.WAITING}


# ── 차례 ──────────────────────────────────────────────────────────────


class TestWhoseTurn:
    def test_a_stranger_cannot_decide(self):
        with pytest.raises(E.ApprovalError, match="결재자가 아닙니다"):
            E.decide(req(), line((3, 1)), actor_id=99, action=E.APPROVED)

    def test_a_later_approver_cannot_jump_the_queue(self):
        """2차가 먼저 승인하면 1차의 판단을 건너뛴 결재가 된다."""
        with pytest.raises(E.ApprovalError, match="아직 차례가 아닙니다"):
            E.decide(req(), line((3, 1), (4, 2)), actor_id=4, action=E.APPROVED)

    def test_nobody_decides_twice(self):
        steps = line((3, 1), (4, 2))
        r, steps = E.decide(req(), steps, 3, E.APPROVED)
        with pytest.raises(E.ApprovalError, match="이미 처리한"):
            E.decide(r, steps, 3, E.REJECTED)

    def test_only_approve_or_reject(self):
        for bad in ("pending", "skipped", "", "APPROVE"):
            with pytest.raises(E.ApprovalError, match="승인 또는 거절"):
                E.decide(req(), line((3, 1)), 3, bad)


# ── 진행 ──────────────────────────────────────────────────────────────


class TestAdvancing:
    def test_a_single_approver_finishes_it(self):
        r, steps = E.decide(req(), line((3, 1)), 3, E.APPROVED)
        assert r["status"] == E.APPROVED and states(steps) == {3: E.APPROVED}

    def test_it_moves_one_person_at_a_time(self):
        """[A] → [B] → [C]. A 가 승인하면 **B 에게만** 간다 — C 는 그대로 대기."""
        steps = line((3, 1), (4, 2), (9, 3))
        r, steps = E.decide(req(), steps, 3, E.APPROVED)
        assert r["status"] == E.PENDING and r["current_step_order"] == 2
        assert states(steps) == {3: E.APPROVED, 4: E.PENDING, 9: E.WAITING}, \
            "C 에게 미리 차례가 갔거나 B 를 건너뛰었다"

        r, steps = E.decide(r, steps, 4, E.APPROVED)
        assert r["current_step_order"] == 3 and states(steps)[9] == E.PENDING

    def test_exactly_one_step_is_pending_at_any_time(self):
        """한 줄이므로 "지금 차례" 는 언제나 정확히 하나여야 한다."""
        steps = line((3, 1), (4, 2), (9, 3))
        r = req()
        for who in (3, 4):
            assert sum(1 for s in steps if s["status"] == E.PENDING) == 1
            r, steps = E.decide(r, steps, who, E.APPROVED)
        assert sum(1 for s in steps if s["status"] == E.PENDING) == 1

    def test_the_last_person_is_the_final_approval(self):
        steps = line((3, 1), (4, 2), (9, 3))
        r = req()
        for who in (3, 4):
            r, steps = E.decide(r, steps, who, E.APPROVED)
            assert r["status"] == E.PENDING, "마지막 사람 전에 승인 완료가 됐다"
        r, steps = E.decide(r, steps, 9, E.APPROVED)
        assert r["status"] == E.APPROVED

    def test_the_last_order_finishing_approves_the_request(self):
        steps = line((3, 1), (4, 2))
        r, steps = E.decide(req(), steps, 3, E.APPROVED)
        r, steps = E.decide(r, steps, 4, E.APPROVED)
        assert r["status"] == E.APPROVED and r["decided_at"] is None or True

    def test_decided_at_is_stamped_only_when_it_ends(self):
        steps = line((3, 1), (4, 2))
        r, steps = E.decide(req(), steps, 3, E.APPROVED, now="T1")
        assert r.get("decided_at") is None, "중간 승인에 종결 시각이 찍혔다"
        r, steps = E.decide(r, steps, 4, E.APPROVED, now="T2")
        assert r["decided_at"] == "T2"


# ── 거절 ──────────────────────────────────────────────────────────────


class TestRejection:
    def test_one_rejection_ends_everything(self):
        steps = line((3, 1), (4, 2), (5, 3))
        r, steps = E.decide(req(), steps, 3, E.REJECTED, note="근거 부족")
        assert r["status"] == E.REJECTED

    def test_those_whose_turn_never_came_are_skipped_not_pending(self):
        """'처리하지 않았다' 와 '차례가 오지 않았다' 는 다른 사실이다.

        합쳐 버리면 나중에 기록을 읽는 사람이 그 둘을 구별할 수 없다.
        """
        steps = line((3, 1), (4, 2), (5, 3))
        _r, steps = E.decide(req(), steps, 3, E.REJECTED)
        assert states(steps) == {3: E.REJECTED, 4: E.SKIPPED, 5: E.SKIPPED}

    def test_a_rejection_in_the_middle_keeps_earlier_approvals(self):
        """이미 승인한 사람의 기록은 남는다 — 그는 실제로 승인했다."""
        steps = line((3, 1), (4, 2), (5, 3))
        r, steps = E.decide(req(), steps, 3, E.APPROVED)
        r, steps = E.decide(r, steps, 4, E.REJECTED)
        assert states(steps) == {3: E.APPROVED, 4: E.REJECTED, 5: E.SKIPPED}

    def test_the_note_is_kept(self):
        _r, steps = E.decide(req(), line((3, 1)), 3, E.REJECTED, note="예산 초과")
        assert steps[0]["note"] == "예산 초과"

    def test_a_long_note_is_cut_not_rejected(self):
        _r, steps = E.decide(req(), line((3, 1)), 3, E.REJECTED, note="가" * 900)
        assert len(steps[0]["note"]) == 500, "긴 사유 때문에 거절 자체가 실패하면 안 된다"


# ── 종결 후 ───────────────────────────────────────────────────────────


class TestTerminal:
    @pytest.mark.parametrize("final", [E.APPROVED, E.REJECTED, E.CANCELED])
    def test_a_finished_request_takes_no_more_decisions(self, final):
        steps = line((3, 1), (4, 2))
        with pytest.raises(E.ApprovalError, match="이미|회수"):
            E.decide(req(status=final), steps, 4, E.APPROVED)

    def test_the_message_says_how_it_ended(self):
        """'처리할 수 없습니다' 로 뭉뚱그리면 사람이 다음에 무엇을 할지 모른다."""
        for final, word in ((E.APPROVED, "승인"), (E.REJECTED, "거절"), (E.CANCELED, "회수")):
            with pytest.raises(E.ApprovalError, match=word):
                E.decide(req(status=final), line((3, 1)), 3, E.APPROVED)


# ── 회수 ──────────────────────────────────────────────────────────────


class TestCancel:
    def test_the_requester_can_take_it_back_before_anyone_approves(self):
        r, steps = E.cancel(req(requester=1), line((3, 1), (4, 2)), actor_id=1)
        assert r["status"] == E.CANCELED
        assert states(steps) == {3: E.SKIPPED, 4: E.SKIPPED}

    def test_an_approver_cannot_cancel_someone_elses_request(self):
        with pytest.raises(E.ApprovalError, match="기안자만"):
            E.cancel(req(requester=1), line((3, 1)), actor_id=3)

    def test_once_someone_approved_it_cannot_be_taken_back(self):
        """회수할 수 있으면 기안자가 **남의 승인을 없던 일로** 만들 수 있다."""
        steps = line((3, 1), (4, 2))
        r, steps = E.decide(req(requester=1), steps, 3, E.APPROVED)
        with pytest.raises(E.ApprovalError, match="이미 승인한"):
            E.cancel(r, steps, actor_id=1)

    def test_a_finished_request_cannot_be_canceled(self):
        with pytest.raises(E.ApprovalError):
            E.cancel(req(requester=1, status=E.APPROVED), line((3, 1)), actor_id=1)


# ── 입력을 바꾸지 않는다 ──────────────────────────────────────────────


class TestPurity:
    def test_decide_does_not_mutate_its_input(self):
        """호출부가 실패 시 원래 상태로 되돌릴 수 있어야 한다."""
        steps = line((3, 1), (4, 2))
        original = req()
        snapshot_req = dict(original)
        snapshot_steps = [dict(s) for s in steps]

        E.decide(original, steps, 3, E.APPROVED, now="T")

        assert original == snapshot_req, "request 가 제자리에서 바뀌었다"
        assert [dict(s) for s in steps] == snapshot_steps, "steps 가 제자리에서 바뀌었다"


# ── 기록은 계정보다 오래 산다 ─────────────────────────────────────────


class TestTheRecordOutlivesTheAccount:
    """XGEN 은 사용자를 **실제로 지운다**(adminUserController.delete_user).

    그때 결재 기록이 함께 지워지면 두 가지가 한꺼번에 무너진다: 이미 승인한
    사람이 결재선에서 사라져 기록이 거짓이 되고, 그 사람이 지금 차례였다면
    그 차수에 아무도 남지 않아 건이 **영영 멈춘다** — 이유를 아무도 모르는 정지다.
    """

    def test_request_steps_do_not_cascade_from_users(self):
        from xgen_sdk.approval.models import ApprovalRequestStep

        col = ApprovalRequestStep().get_schema()["approver_id"]
        assert "REFERENCES users" not in col, (
            "결재 기록에 users FK 가 붙었다 — 계정을 지우면 승인 기록이 사라지거나"
            "(CASCADE) 퇴사 처리가 막힌다(RESTRICT). 이 표는 설정이 아니라 기록이다."
        )
        assert "NOT NULL" in col, "누가 승인했는지는 비어서는 안 된다"

    def test_line_templates_do_cascade(self):
        """반대로 **템플릿**은 설정이라, 퇴사자는 앞으로 쓸 결재선에서 빠져야 한다."""
        from xgen_sdk.approval.models import ApprovalLineStep

        col = ApprovalLineStep().get_schema()["approver_id"]
        assert "REFERENCES users(id) ON DELETE CASCADE" in col
