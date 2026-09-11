"""결재 원장 — 규칙이 **DB 에 닿을 때** 지켜지는가.

상태기계 자체는 test_approval_engine.py 가 전부 본다. 여기서 보는 것은
그 규칙이 저장까지 갔을 때 깨지지 않는가, 그리고 store 에만 있는 판단들:
자기 결재, 등록되지 않은 종류, **결재선 스냅샷**, 적용 실패 처리, 열람 범위.

진짜 PG 대신 최소 메모리 DB 를 쓴다 — 여기서 확인할 것은 SQL 방언이 아니라
"승인이 기록되는가 / 템플릿이 바뀌어도 진행 중인 건은 그대로인가" 다.
"""
from __future__ import annotations

import re
import pytest

from xgen_sdk.approval import engine as E, registry, store


class FakeDB:
    """이 모듈이 실제로 쓰는 질의만 이해하는 메모리 원장."""

    def __init__(self):
        self.t = {"approval_lines": [], "approval_line_steps": [],
                  "approval_requests": [], "approval_request_steps": [],
                  "users": []}
        self._seq = {k: 0 for k in self.t}

    def add_user(self, uid, username, full_name=None):
        self.t["users"].append({"id": uid, "username": username, "full_name": full_name})

    def _next(self, table):
        self._seq[table] += 1
        return self._seq[table]

    def execute_raw_query(self, sql, params=()):
        s = " ".join(sql.split())
        p = list(params)
        try:
            return {"success": True, "data": self._run(s, p), "error": None}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "data": [], "error": str(exc)}

    def _run(self, s, p):
        if s.startswith("INSERT INTO approval_lines"):
            row = {"id": self._next("approval_lines"), "name": p[0], "description": p[1],
                   "owner_id": p[2], "is_shared": p[3], "is_active": True}
            self.t["approval_lines"].append(row)
            return [{"id": row["id"]}]
        if s.startswith("INSERT INTO approval_line_steps"):
            self.t["approval_line_steps"].append(
                {"id": self._next("approval_line_steps"), "line_id": p[0],
                 "step_order": p[1], "approver_id": p[2]})
            return []
        if s.startswith("SELECT step_order, approver_id FROM approval_line_steps"):
            return [{"step_order": r["step_order"], "approver_id": r["approver_id"]}
                    for r in self.t["approval_line_steps"] if r["line_id"] == p[0]]
        if s.startswith("INSERT INTO approval_requests"):
            row = {"id": self._next("approval_requests"), "title": p[0], "reason": p[1],
                   "action_type": p[2], "payload": p[3], "requester_id": p[4],
                   "line_id": p[5], "status": "pending", "current_step_order": p[6],
                   "decided_at": None, "applied_at": None, "apply_error": None,
                   "created_at": "T0"}
            self.t["approval_requests"].append(row)
            return [{"id": row["id"]}]
        if s.startswith("INSERT INTO approval_request_steps"):
            self.t["approval_request_steps"].append(
                {"id": self._next("approval_request_steps"), "request_id": p[0],
                 "step_order": p[1], "approver_id": p[2], "status": p[3],
                 "acted_at": None, "note": None})
            return []
        if s.startswith("SELECT r.id, r.title, r.reason"):
            out = []
            for r in self.t["approval_requests"]:
                if r["id"] != p[0]:
                    continue
                u = next((x for x in self.t["users"] if x["id"] == r["requester_id"]), {})
                out.append({**r, "requester_username": u.get("username"),
                            "requester_name": u.get("full_name")})
            return out
        if s.startswith("SELECT s.step_order, s.approver_id, s.status"):
            rows = [r for r in self.t["approval_request_steps"] if r["request_id"] == p[0]]
            rows.sort(key=lambda r: (r["step_order"], r["id"]))
            out = []
            for r in rows:
                u = next((x for x in self.t["users"] if x["id"] == r["approver_id"]), {})
                out.append({**r, "username": u.get("username"), "full_name": u.get("full_name")})
            return out
        # 조건부 쓰기(compare-and-set) — 조건이 맞은 행만 RETURNING 으로 돌려준다.
        if s.startswith("UPDATE approval_request_steps SET status = %s, acted_at"):
            hit = []
            for r in self.t["approval_request_steps"]:
                if (r["request_id"] == p[3] and r["approver_id"] == p[4]
                        and r["status"] == p[5]):
                    r.update(status=p[0], acted_at=p[1], note=p[2])
                    hit.append({"id": r["id"]})
            return hit
        if s.startswith("UPDATE approval_request_steps SET status = %s WHERE request_id = %s AND status IN"):
            for r in self.t["approval_request_steps"]:
                if r["request_id"] == p[1] and r["status"] in (p[2], p[3]):
                    r["status"] = p[0]
            return []
        if s.startswith("UPDATE approval_request_steps SET status = %s WHERE request_id = %s AND step_order"):
            for r in self.t["approval_request_steps"]:
                if (r["request_id"] == p[1] and r["step_order"] == p[2]
                        and r["status"] == p[3]):
                    r["status"] = p[0]
            return []
        if s.startswith("UPDATE approval_request_steps SET status = %s WHERE"):
            for r in self.t["approval_request_steps"]:
                if r["request_id"] == p[1] and r["approver_id"] == p[2]:
                    r["status"] = p[0]
            return []
        if s.startswith("UPDATE approval_requests SET status = %s, current_step_order = %s, decided_at = %s WHERE id = %s AND status"):
            hit = []
            for r in self.t["approval_requests"]:
                if (r["id"] == p[3] and r["status"] == p[4]
                        and r["current_step_order"] == p[5]):
                    r.update(status=p[0], current_step_order=p[1], decided_at=p[2])
                    hit.append({"id": r["id"]})
            return hit
        if s.startswith("UPDATE approval_requests SET status = %s, current_step_order"):
            for r in self.t["approval_requests"]:
                if r["id"] == p[3]:
                    r.update(status=p[0], current_step_order=p[1], decided_at=p[2])
            return []
        if s.startswith("UPDATE approval_requests SET status = %s, decided_at"):
            for r in self.t["approval_requests"]:
                if r["id"] == p[2]:
                    r.update(status=p[0], decided_at=p[1])
            return []
        if s.startswith("UPDATE approval_requests SET applied_at"):
            for r in self.t["approval_requests"]:
                if r["id"] == p[2]:
                    r.update(applied_at=p[0], apply_error=p[1])
            return []
        if s.startswith("SELECT r.id, r.title, r.action_type, r.status, r.created_at, r.requester_id"):
            out = []
            for st in self.t["approval_request_steps"]:
                if st["approver_id"] != p[0]:
                    continue
                if "s.status = 'pending'" in s and st["status"] != "pending":
                    continue
                r = next(x for x in self.t["approval_requests"] if x["id"] == st["request_id"])
                out.append({**r, "my_status": st["status"], "my_step_order": st["step_order"]})
            return out
        if s.startswith("SELECT r.id, r.title, r.action_type, r.status, r.created_at, r.current_step_order"):
            return [r for r in self.t["approval_requests"] if r["requester_id"] == p[0]]
        if s.startswith("SELECT r.id, r.title, r.action_type, r.status, r.created_at, r.decided_at"):
            me = p[0]
            n_states = len(p) - 6          # me,me,me, *states, me, me, limit
            states = p[3:3 + n_states]
            out = []
            for r in self.t["approval_requests"]:
                if r["status"] not in states:
                    continue
                mine = r["requester_id"] == me
                steps = [x for x in self.t["approval_request_steps"] if x["request_id"] == r["id"]]
                as_approver = [x for x in steps if x["approver_id"] == me]
                if not mine and not as_approver:
                    continue
                pend = next((x for x in steps if x["status"] == "pending"), None)
                out.append({**r, "i_requested": mine, "i_approve": bool(as_approver),
                            "my_status": as_approver[0]["status"] if as_approver else None,
                            "waiting_on": (pend or {}).get("approver_id")})
            return out
        raise AssertionError(f"가짜 DB 가 모르는 질의: {s[:90]}")


@pytest.fixture
def db():
    d = FakeDB()
    for uid, name in ((1, "기안자"), (3, "팀장"), (4, "부장"), (9, "제삼자")):
        d.add_user(uid, name, name)
    return d


def _line(db, *pairs, owner=1, shared=True):
    return store.create_line(
        db, name="표준 결재선", description="", owner_id=owner, is_shared=shared,
        steps=[{"approver_id": u, "step_order": o} for u, o in pairs],
    )["id"]


# ── 올리기 ────────────────────────────────────────────────────────────


def test_submitting_puts_the_first_approver_on_the_clock(db):
    r = store.submit(db, requester_id=1, title="서버 증설", reason="부하",
                     steps=[{"approver_id": 3, "step_order": 1},
                            {"approver_id": 4, "step_order": 2}])
    assert r["status"] == "pending"
    by = {s["approver_id"]: s["status"] for s in r["steps"]}
    assert by == {3: "pending", 4: "waiting"}


def test_you_cannot_put_yourself_in_your_own_line(db):
    """자기 결재를 자기가 승인하면 그건 결재가 아니다."""
    with pytest.raises(E.ApprovalError, match="자기 자신"):
        store.submit(db, requester_id=1, title="x",
                     steps=[{"approver_id": 1, "step_order": 1}])


def test_an_unregistered_action_type_is_refused_at_the_door(db):
    """승인해 봐야 아무도 무엇을 해야 할지 모르는 건을 만들지 않는다."""
    with pytest.raises(E.ApprovalError, match="등록되지 않은"):
        store.submit(db, requester_id=1, title="x", action_type="없는.동작",
                     steps=[{"approver_id": 3, "step_order": 1}])


def test_a_title_is_required(db):
    with pytest.raises(E.ApprovalError, match="제목"):
        store.submit(db, requester_id=1, title="  ",
                     steps=[{"approver_id": 3, "step_order": 1}])


def test_a_line_must_be_given(db):
    with pytest.raises(E.ApprovalError, match="결재선을 지정"):
        store.submit(db, requester_id=1, title="x")


# ── 스냅샷 ────────────────────────────────────────────────────────────


def test_the_line_is_copied_not_referenced(db):
    """진행 중인 결재의 결재선이 템플릿 수정으로 바뀌면 그건 위조다.

    이미 승인한 사람이 결재선에서 사라지거나, 승인한 적 없는 사람이 결재선에
    나타난다 — 어느 쪽이든 기록이 거짓이 된다.
    """
    line_id = _line(db, (3, 1), (4, 2))
    r = store.submit(db, requester_id=1, title="증설", line_id=line_id)
    assert {s["approver_id"] for s in r["steps"]} == {3, 4}

    # 템플릿을 바꾼다 — 부장을 빼고 제삼자를 넣는다.
    db.t["approval_line_steps"] = [x for x in db.t["approval_line_steps"]
                                   if not (x["line_id"] == line_id and x["approver_id"] == 4)]
    db.t["approval_line_steps"].append(
        {"id": 99, "line_id": line_id, "step_order": 2, "approver_id": 9})

    again = store.get(db, r["id"])
    assert {s["approver_id"] for s in again["steps"]} == {3, 4}, "진행 중인 결재선이 바뀌었다"


# ── 결정이 기록되는가 ─────────────────────────────────────────────────


def test_an_approval_is_written_down(db):
    r = store.submit(db, requester_id=1, title="증설",
                     steps=[{"approver_id": 3, "step_order": 1},
                            {"approver_id": 4, "step_order": 2}])
    out = store.decide(db, r["id"], actor_id=3, action="approved", note="확인함")
    by = {s["approver_id"]: s for s in out["steps"]}
    assert by[3]["status"] == "approved" and by[3]["note"] == "확인함"
    assert by[3]["acted_at"] is not None
    assert by[4]["status"] == "pending", "다음 차례가 열리지 않았다"
    assert out["current_step_order"] == 2


def test_a_rejection_ends_it_in_the_ledger_too(db):
    r = store.submit(db, requester_id=1, title="증설",
                     steps=[{"approver_id": 3, "step_order": 1},
                            {"approver_id": 4, "step_order": 2}])
    out = store.decide(db, r["id"], actor_id=3, action="rejected", note="근거 부족")
    assert out["status"] == "rejected"
    assert {s["approver_id"]: s["status"] for s in out["steps"]} == {3: "rejected", 4: "skipped"}


def test_the_engines_refusals_reach_the_caller(db):
    r = store.submit(db, requester_id=1, title="증설",
                     steps=[{"approver_id": 3, "step_order": 1},
                            {"approver_id": 4, "step_order": 2}])
    with pytest.raises(E.ApprovalError, match="아직 차례가 아닙니다"):
        store.decide(db, r["id"], actor_id=4, action="approved")
    with pytest.raises(E.ApprovalError, match="결재자가 아닙니다"):
        store.decide(db, r["id"], actor_id=9, action="approved")


# ── 적용 ──────────────────────────────────────────────────────────────


def test_the_applier_runs_once_on_final_approval(db):
    calls = []
    registry.register_action("t.ok", lambda payload, req: calls.append(payload), label="테스트")
    r = store.submit(db, requester_id=1, title="증설", action_type="t.ok",
                     payload={"n": 7}, steps=[{"approver_id": 3, "step_order": 1}])
    out = store.decide(db, r["id"], actor_id=3, action="approved")
    assert out["status"] == "approved"
    assert calls == [{"n": 7}], "적용이 한 번 돌지 않았다"
    assert out["applied_at"] is not None and not out["apply_error"]


def test_an_apply_failure_does_not_undo_the_approval(db):
    """사람의 결재는 이미 일어난 사실이다.

    적용이 실패했다고 승인을 무르면, 화면은 '승인되지 않음' 이라 말하는데
    실제로는 사람이 승인한 상태가 된다 — 그쪽이 훨씬 나쁜 거짓말이다.
    """
    def boom(payload, req):
        raise RuntimeError("대상 서버 없음")

    registry.register_action("t.boom", boom, label="터지는 것")
    r = store.submit(db, requester_id=1, title="증설", action_type="t.boom",
                     steps=[{"approver_id": 3, "step_order": 1}])
    out = store.decide(db, r["id"], actor_id=3, action="approved")

    assert out["status"] == "approved", "적용 실패가 승인을 물렸다"
    assert "대상 서버 없음" in (out["apply_error"] or ""), "실패를 숨겼다"


def test_the_applier_does_not_run_on_rejection(db):
    calls = []
    registry.register_action("t.never", lambda p, r: calls.append(1), label="안 돌아야")
    r = store.submit(db, requester_id=1, title="증설", action_type="t.never",
                     steps=[{"approver_id": 3, "step_order": 1}])
    store.decide(db, r["id"], actor_id=3, action="rejected")
    assert calls == []


def test_the_applier_does_not_run_mid_line(db):
    """마지막 사람이 승인하기 전에는 아무 일도 일어나지 않아야 한다."""
    calls = []
    registry.register_action("t.mid", lambda p, r: calls.append(1), label="중간")
    r = store.submit(db, requester_id=1, title="증설", action_type="t.mid",
                     steps=[{"approver_id": 3, "step_order": 1},
                            {"approver_id": 4, "step_order": 2}])
    store.decide(db, r["id"], actor_id=3, action="approved")
    assert calls == [], "1차 승인만으로 적용이 돌았다"
    store.decide(db, r["id"], actor_id=4, action="approved")
    assert calls == [1]


# ── 회수 ──────────────────────────────────────────────────────────────


def test_cancel_is_written_down(db):
    r = store.submit(db, requester_id=1, title="증설",
                     steps=[{"approver_id": 3, "step_order": 1}])
    out = store.cancel(db, r["id"], actor_id=1)
    assert out["status"] == "canceled"
    assert out["steps"][0]["status"] == "skipped"


# ── 결재함 ────────────────────────────────────────────────────────────


def test_the_inbox_shows_only_whose_turn_it_is(db):
    store.submit(db, requester_id=1, title="A",
                 steps=[{"approver_id": 3, "step_order": 1},
                        {"approver_id": 4, "step_order": 2}])
    assert [x["title"] for x in store.inbox(db, 3)] == ["A"]
    assert store.inbox(db, 4) == [], "차례가 오지 않은 사람의 결재함에 떴다"
    assert store.inbox(db, 9) == []


def test_the_inbox_can_also_show_what_i_already_handled(db):
    r = store.submit(db, requester_id=1, title="A",
                     steps=[{"approver_id": 3, "step_order": 1}])
    store.decide(db, r["id"], actor_id=3, action="approved")
    assert store.inbox(db, 3) == []
    assert len(store.inbox(db, 3, only_pending=False)) == 1


def test_the_outbox_is_what_i_sent(db):
    store.submit(db, requester_id=1, title="A", steps=[{"approver_id": 3, "step_order": 1}])
    assert [x["title"] for x in store.outbox(db, 1)] == ["A"]
    assert store.outbox(db, 3) == []


# ── 열람 범위 ─────────────────────────────────────────────────────────


class TestCanRead:
    req = {"requester_id": 1, "steps": [{"approver_id": 3}, {"approver_id": 4}]}

    def test_the_requester_can_read_it(self):
        assert store.can_read(self.req, 1, False)

    def test_an_approver_can_read_it_even_before_their_turn(self):
        assert store.can_read(self.req, 4, False)

    def test_a_stranger_cannot(self):
        """목록에 없는 건의 id 를 찍어 넣어 읽는 길을 열면 결재함을 나눈 의미가 없다."""
        assert not store.can_read(self.req, 9, False)

    def test_an_admin_can(self):
        assert store.can_read(self.req, 9, True)


def test_db_errors_are_not_swallowed_as_empty(db):
    """DB 오류를 빈 결과로 삼키면 '결재가 없습니다' 로 보인다 — 가장 나쁜 거짓말이다."""
    class Broken:
        def execute_raw_query(self, sql, params=()):
            return {"success": False, "data": [], "error": "connection refused"}

    with pytest.raises(RuntimeError, match="connection refused"):
        store.outbox(Broken(), 1)


# ── 내가 관련된 문서 (진행 / 완료 탭) ─────────────────────────────────


def test_involved_shows_what_i_am_on_even_before_my_turn(db):
    """받은 결재함과 다른 목록이다.

    받은 결재함은 "지금 내가 눌러야 하는 것" 이고, 이쪽은 "내가 걸려 있는 것
    전부" 다. 2차 결재자가 자기가 걸린 건을 아예 못 보면, 언제 자기 차례가
    올지도 모른 채 기다리게 된다.
    """
    store.submit(db, requester_id=1, title="A",
                 steps=[{"approver_id": 3, "step_order": 1},
                        {"approver_id": 4, "step_order": 2}])
    assert store.inbox(db, 4) == [], "아직 차례가 아니다"
    rows = store.involved(db, 4, store.IN_PROGRESS)
    assert [r["title"] for r in rows] == ["A"]
    assert rows[0]["i_approve"] and not rows[0]["i_requested"]


def test_involved_shows_what_i_sent(db):
    store.submit(db, requester_id=1, title="A", steps=[{"approver_id": 3, "step_order": 1}])
    rows = store.involved(db, 1, store.IN_PROGRESS)
    assert rows[0]["i_requested"] and not rows[0]["i_approve"]


def test_involved_hides_other_peoples_business(db):
    store.submit(db, requester_id=1, title="A", steps=[{"approver_id": 3, "step_order": 1}])
    assert store.involved(db, 9, store.IN_PROGRESS) == []


def test_in_progress_and_done_are_different_piles(db):
    a = store.submit(db, requester_id=1, title="끝난 것",
                     steps=[{"approver_id": 3, "step_order": 1}])
    store.decide(db, a["id"], actor_id=3, action="approved")
    store.submit(db, requester_id=1, title="도는 것",
                 steps=[{"approver_id": 3, "step_order": 1}])

    assert [r["title"] for r in store.involved(db, 1, store.IN_PROGRESS)] == ["도는 것"]
    assert [r["title"] for r in store.involved(db, 1, store.DONE)] == ["끝난 것"]


def test_done_includes_rejected_and_canceled(db):
    """거절·회수도 **끝난 것**이다 — 진행 중에 남아 있으면 영영 안 사라진다."""
    a = store.submit(db, requester_id=1, title="거절", steps=[{"approver_id": 3, "step_order": 1}])
    store.decide(db, a["id"], actor_id=3, action="rejected")
    b = store.submit(db, requester_id=1, title="회수", steps=[{"approver_id": 3, "step_order": 1}])
    store.cancel(db, b["id"], actor_id=1)

    assert sorted(r["title"] for r in store.involved(db, 1, store.DONE)) == ["거절", "회수"]
    assert store.involved(db, 1, store.IN_PROGRESS) == []


def test_in_progress_says_whose_desk_it_is_on(db):
    """"지금 누구 손에 있나" 가 없으면 재촉할 사람을 모른다."""
    store.submit(db, requester_id=1, title="A",
                 steps=[{"approver_id": 3, "step_order": 1},
                        {"approver_id": 4, "step_order": 2}])
    row = store.involved(db, 1, store.IN_PROGRESS)[0]
    assert row["waiting_on"] == 3
