"""결재선을 **관리자가 관리한다** — 만들고, 고치고, 내린다.

이 기능이 없으면 결재선은 어디에서도 만들어지지 않는다. 그러면 [결재 목록
설정] 의 [기본 결재선] 드롭다운은 영영 비어 있고, 요청 화면의 결재선 모달은
[저장된 결재선 불러오기] 를 띄울 일이 없어 사용자가 매번 결재자를 손으로
고른다 — 결재선이라는 개념이 있으나 마나가 된다.
"""
from __future__ import annotations

import pytest

from xgen_sdk.approval import engine as E, store


class LineDB:
    """결재선 관리 SQL 만 이해하는 최소 원장."""

    def __init__(self):
        self.lines = []
        self.steps = []
        self.users = {}
        self.policies = []   # {action_type, default_line_id}
        self._seq = 0

    def add_user(self, uid, username, full_name=None):
        self.users[uid] = {"id": uid, "username": username, "full_name": full_name}

    def execute_raw_query(self, sql, params=()):
        s = " ".join(sql.split())
        try:
            return {"success": True, "data": self._run(s, list(params)), "error": None}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "data": [], "error": str(exc)}

    def _line(self, lid):
        return next((l for l in self.lines if l["id"] == int(lid)), None)

    def _steps_of(self, lid):
        rows = [dict(s) for s in self.steps if s["line_id"] == int(lid)]
        rows.sort(key=lambda r: r["step_order"])
        for r in rows:
            u = self.users.get(r["approver_id"], {})
            r["username"] = u.get("username")
            r["full_name"] = u.get("full_name")
            r.pop("line_id", None)
        return rows

    def _run(self, s, p):
        if s.startswith("INSERT INTO approval_lines"):
            self._seq += 1
            self.lines.append({"id": self._seq, "name": p[0], "description": p[1],
                               "owner_id": p[2], "is_shared": p[3], "is_active": True})
            return [{"id": self._seq}]
        if s.startswith("INSERT INTO approval_line_steps"):
            self.steps.append({"line_id": p[0], "step_order": p[1], "approver_id": p[2]})
            return []
        if s.startswith("DELETE FROM approval_line_steps"):
            self.steps = [x for x in self.steps if x["line_id"] != int(p[0])]
            return []
        # 한 줄 조회(get_line) — 공용 목록과 앞부분이 같아 뒷조건으로 가른다.
        if s.startswith("SELECT l.id, l.name, l.description") and "WHERE l.id = %s" in s:
            ln = self._line(p[0])
            if not ln:
                return []
            return [{**ln, "form_id": ln.get("form_id"), "form_name": None}]
        if s.startswith("SELECT s.step_order, s.approver_id"):
            return self._steps_of(p[0])
        if s.startswith("SELECT owner_id FROM approval_lines"):
            ln = self._line(p[0])
            return [{"owner_id": ln["owner_id"]}] if ln else []
        if s.startswith("UPDATE approval_lines SET is_active = FALSE"):
            ln = self._line(p[0])
            if ln:
                ln["is_active"] = False
            return []
        if s.startswith("UPDATE approval_lines SET"):
            ln = self._line(p[-1])
            assigns = s[len("UPDATE approval_lines SET "):].split(" WHERE ")[0].split(", ")
            for i, a in enumerate(assigns):
                ln[a.split(" = ")[0]] = p[i]
            return []
        if s.startswith("SELECT l.id, l.name, l.description") and "AS owner_name" in s:
            out = []
            for ln in self.lines:
                if ln["is_active"] and ln["is_shared"]:
                    u = self.users.get(ln["owner_id"], {})
                    out.append({**ln, "form_id": ln.get("form_id"), "form_name": None,
                                "owner_name": u.get("full_name") or u.get("username")})
            out.sort(key=lambda r: r["name"])
            return out
        if s.startswith("SELECT action_type FROM approval_action_policies"):
            return [{"action_type": x["action_type"]} for x in self.policies
                    if x["default_line_id"] == int(p[0])]
        if s.startswith("UPDATE approval_action_policies SET default_line_id = NULL"):
            for x in self.policies:
                if x["default_line_id"] == int(p[0]):
                    x["default_line_id"] = None
            return []
        raise AssertionError("모르는 SQL: " + s)


@pytest.fixture()
def db():
    d = LineDB()
    for uid, name in ((1, "kim"), (2, "lee"), (3, "park"), (9, "admin")):
        d.add_user(uid, name, name.upper())
    return d


def _make(db, name="기본선", shared=True, approvers=(1, 2)):
    return store.create_line(
        db, name=name, description="", owner_id=9, is_shared=shared,
        steps=[{"approver_id": a, "step_order": i + 1} for i, a in enumerate(approvers)],
    )


class TestCreateAndList:
    def test_a_shared_line_shows_up_for_the_admin(self, db):
        _make(db, "배포 2단")
        rows = store.list_all_shared_lines(db)
        assert [r["name"] for r in rows] == ["배포 2단"]
        assert [s["approver_id"] for s in rows[0]["steps"]] == [1, 2]

    def test_a_private_line_is_not_an_admin_concern(self, db):
        """관리자 화면이 손대는 것은 공용 줄뿐이다 — 남의 개인 결재선을
        관리자가 고치기 시작하면 그건 관리가 아니라 침해다."""
        _make(db, "내 개인선", shared=False)
        assert store.list_all_shared_lines(db) == []

    def test_the_owner_name_comes_along(self, db):
        """누가 만든 줄인지 보이지 않으면 목록이 금세 정체불명이 된다."""
        _make(db, "감사선")
        assert store.list_all_shared_lines(db)[0]["owner_name"] == "ADMIN"


class TestUpdate:
    def test_renaming_keeps_the_approvers(self, db):
        line = _make(db, "옛 이름")
        out = store.update_line(db, line["id"], name="새 이름")
        assert out["name"] == "새 이름"
        assert [s["approver_id"] for s in out["steps"]] == [1, 2]

    def test_approvers_are_replaced_wholesale(self, db):
        """부분 수정이 아니다 — 순서가 곧 결재 차례라 반쪽만 바꾸면
        어느 차례가 남았는지 아무도 모른다."""
        line = _make(db, "L")
        out = store.update_line(db, line["id"], steps=[
            {"approver_id": 3, "step_order": 1}, {"approver_id": 1, "step_order": 2}])
        assert [s["approver_id"] for s in out["steps"]] == [3, 1]
        assert [s["step_order"] for s in out["steps"]] == [1, 2]

    def test_a_broken_line_is_refused(self, db):
        """같은 사람이 두 번 서면 두 번 승인하라는 뜻이 된다 — engine 이 막는다."""
        line = _make(db, "L")
        with pytest.raises(E.ApprovalError):
            store.update_line(db, line["id"], steps=[
                {"approver_id": 1, "step_order": 1}, {"approver_id": 1, "step_order": 2}])

    def test_an_empty_name_is_refused(self, db):
        line = _make(db, "L")
        with pytest.raises(E.ApprovalError):
            store.update_line(db, line["id"], name="   ")

    def test_a_missing_line_is_refused(self, db):
        with pytest.raises(E.ApprovalError):
            store.update_line(db, 999, name="x")


class TestDelete:
    def test_it_is_taken_down_not_erased(self, db):
        """지우면 '그때 어느 결재선으로 올렸나' 를 답할 수 없다."""
        line = _make(db, "L")
        store.delete_line(db, line["id"], actor_id=9, is_superuser=True)
        assert store.get_line(db, line["id"])["is_active"] is False
        assert store.list_all_shared_lines(db) == []

    def test_a_line_in_use_as_a_default_is_not_taken_down_silently(self, db):
        """그냥 내리면 그 행위의 모달이 빈 채로 뜨는데 이유가 어디에도 안 나온다."""
        line = _make(db, "L")
        db.policies.append({"action_type": "agent.deploy", "default_line_id": line["id"]})
        with pytest.raises(E.ApprovalError) as caught:
            store.delete_line(db, line["id"], actor_id=9, is_superuser=True)
        assert "agent.deploy" in str(caught.value)
        assert store.get_line(db, line["id"])["is_active"] is True

    def test_force_clears_the_defaults_it_frees(self, db):
        """강제로 내릴 때는 그 행위들의 기본 결재선을 **함께 비운다** —
        죽은 줄을 가리킨 채로 남기지 않는다."""
        line = _make(db, "L")
        db.policies.append({"action_type": "agent.deploy", "default_line_id": line["id"]})
        store.delete_line(db, line["id"], actor_id=9, is_superuser=True, force=True)
        assert db.policies[0]["default_line_id"] is None
        assert store.get_line(db, line["id"])["is_active"] is False

    def test_someone_elses_line_is_not_mine_to_take_down(self, db):
        line = _make(db, "L")
        with pytest.raises(E.ApprovalError):
            store.delete_line(db, line["id"], actor_id=1, is_superuser=False)


def test_which_actions_lean_on_a_line(db):
    line = _make(db, "L")
    db.policies.append({"action_type": "collection.create", "default_line_id": line["id"]})
    db.policies.append({"action_type": "db.create", "default_line_id": None})
    assert store.default_policies_using_line(db, line["id"]) == ["collection.create"]
