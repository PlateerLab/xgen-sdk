"""**무엇이 결재를 받아야 하는가** — 정책 원장.

첫 번째 규칙은 하나다: **기본은 아무것도 막지 않는다.** 이 기능이 배포되는 날
아무도 막히면 안 된다. 관리자가 [결재 목록 설정] 에서 켠 것만 결재를 탄다.
"""
from __future__ import annotations

import pytest

from xgen_sdk.approval import catalog, policy
from test_approval_store import FakeDB


class PolicyDB(FakeDB):
    """정책 표까지 아는 메모리 DB."""

    def __init__(self):
        super().__init__()
        self.t["approval_action_policies"] = []
        self.t["approval_policy_history"] = []
        self._seq["approval_policy_history"] = 0

    def _run(self, s, p):
        if s.startswith("SELECT action_type, required, default_line_id"):
            return [dict(r) for r in self.t["approval_action_policies"]
                    if r["action_type"] == p[0]]
        if s.startswith("SELECT p.action_type, p.required"):
            return [dict(r, default_line_name=None, updated_by_name=None)
                    for r in self.t["approval_action_policies"]]
        if s.startswith("SELECT action_type FROM approval_action_policies"):
            return [{"action_type": r["action_type"]}
                    for r in self.t["approval_action_policies"] if r["required"]]
        if s.startswith("INSERT INTO approval_action_policies"):
            for r in self.t["approval_action_policies"]:
                if r["action_type"] == p[0]:
                    r.update(required=p[1], default_line_id=p[2], line_locked=p[3],
                             updated_by=p[4], updated_at=p[5])
                    return []
            self.t["approval_action_policies"].append(
                {"action_type": p[0], "required": p[1], "default_line_id": p[2],
                 "line_locked": p[3], "updated_by": p[4], "updated_at": p[5]})
            return []
        if s.startswith("SELECT action_type, default_line_id FROM approval_action_policies"):
            return [{"action_type": r["action_type"], "default_line_id": r["default_line_id"]}
                    for r in self.t["approval_action_policies"]
                    if r.get("line_locked") and r.get("default_line_id")]
        if s.startswith("INSERT INTO approval_policy_history"):
            self._seq["approval_policy_history"] += 1
            self.t["approval_policy_history"].append(
                {"id": self._seq["approval_policy_history"], "target": p[0],
                 "before": p[1], "after": p[2], "changed_by": p[3],
                 "created_at": "T0"})
            return []
        if s.startswith("SELECT h.id, h.target"):
            rows = sorted(self.t["approval_policy_history"],
                          key=lambda r: r["id"], reverse=True)
            return [dict(r, changed_by_name=None) for r in rows[:p[0]]]
        return super()._run(s, p)


@pytest.fixture
def db():
    return PolicyDB()


# ── 기본은 무제한 ─────────────────────────────────────────────────────


def test_nothing_is_required_by_default(db):
    """**배포하는 날 아무도 막히지 않는다.** 표가 비어 있으면 전부 통과다."""
    for sp in catalog.gated_specs():
        assert policy.is_required(db, sp.action_type) is False


def test_an_unknown_action_is_not_required(db):
    """카탈로그에서 사라진 옛 행위의 설정이 남아 있어도 게이트가 되지 않는다."""
    assert policy.is_required(db, "no.such.action") is False


def test_the_screen_lists_the_whole_catalog_even_before_anything_is_set(db):
    """저장된 것만 보여 주면 **아직 켠 적 없는 행위는 영영 켤 수 없다.**"""
    rows = policy.list_all(db)
    assert {r["action_type"] for r in rows} == {s.action_type for s in catalog.gated_specs()}
    assert all(r["required"] is False for r in rows)
    assert all(r["label"] and r["domain"] for r in rows), "화면에 쓸 이름표가 없다"


def test_free_actions_never_show_up_in_the_screen(db):
    """일반·테스트 결재는 게이트가 아니다 — 켜고 끌 것이 없다."""
    shown = {r["action_type"] for r in policy.list_all(db)}
    assert catalog.GENERIC not in shown and catalog.TEST not in shown


# ── 켜고 끄기 ─────────────────────────────────────────────────────────


def test_turning_a_gate_on_and_off(db):
    policy.set_policy(db, "agent.deploy", actor_id=7, required=True)
    assert policy.is_required(db, "agent.deploy") is True

    policy.set_policy(db, "agent.deploy", actor_id=7, required=False)
    assert policy.is_required(db, "agent.deploy") is False


def test_required_actions_lists_only_what_is_on(db):
    policy.set_policy(db, "agent.deploy", actor_id=1, required=True)
    policy.set_policy(db, "tool.publish", actor_id=1, required=True, default_line_id=2)
    policy.set_policy(db, "db.create", actor_id=1, required=False)
    assert set(policy.required_actions(db)) == {"agent.deploy", "tool.publish"}


def test_a_default_line_is_remembered_and_can_be_cleared(db):
    policy.set_policy(db, "agent.deploy", actor_id=1, required=True, default_line_id=5)
    assert policy.get(db, "agent.deploy")["default_line_id"] == 5

    # 안 주면 그대로 — 켜고 끄는 토글이 결재선을 지워 버리면 안 된다.
    policy.set_policy(db, "agent.deploy", actor_id=1, required=False)
    assert policy.get(db, "agent.deploy")["default_line_id"] == 5

    policy.set_policy(db, "agent.deploy", actor_id=1, default_line_id=None)
    assert policy.get(db, "agent.deploy")["default_line_id"] is None


def test_an_unknown_action_cannot_be_configured(db):
    with pytest.raises(ValueError):
        policy.set_policy(db, "no.such.action", actor_id=1, required=True)


def test_a_free_action_cannot_be_made_required(db):
    """'일반 결재를 결재 필수로' 는 뜻이 없는 문장이다."""
    with pytest.raises(ValueError):
        policy.set_policy(db, catalog.GENERIC, actor_id=1, required=True)


# ── 이력 — 감사에서 실제로 문제가 되는 쪽 ─────────────────────────────


def test_turning_a_gate_on_is_written_down(db):
    """"그날 왜 승인 없이 배포됐나" 의 답이 "전날 누군가 껐다" 인 경우가 있다."""
    policy.set_policy(db, "agent.deploy", actor_id=42, required=True)
    h = policy.history(db)
    assert len(h) == 1
    assert h[0]["target"] == "agent.deploy"
    assert h[0]["before"] == "결재 불필요" and h[0]["after"] == "결재 필수"
    assert h[0]["changed_by"] == 42


def test_setting_the_same_value_again_writes_nothing(db):
    policy.set_policy(db, "agent.deploy", actor_id=1, required=True)
    policy.set_policy(db, "agent.deploy", actor_id=1, required=True)
    assert len(policy.history(db)) == 1


def test_changing_the_default_line_is_written_down_too(db):
    policy.set_policy(db, "agent.deploy", actor_id=1, default_line_id=3)
    targets = [h["target"] for h in policy.history(db)]
    assert "agent.deploy:default_line" in targets


def test_history_is_newest_first(db):
    policy.set_policy(db, "agent.deploy", actor_id=1, required=True)
    policy.set_policy(db, "tool.publish", actor_id=1, required=True, default_line_id=2)
    assert [h["target"] for h in policy.history(db)][:2] == [
        "tool.publish:default_line", "tool.publish"]


# ── 슈퍼유저 면제 ─────────────────────────────────────────────────────


def test_superuser_exemption_defaults_to_on():
    """지금까지 superuser 는 게이트를 건너뛰었다. 말없이 뒤집지 않는다 —
    어제까지 되던 관리 작업이 오늘 결재를 기다리면 그건 사고다."""
    assert policy.superuser_exempt(None) is True


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("1", True), ("on", True), ("YES", True),
    ("false", False), ("0", False), ("off", False), ("", False),
    (True, True), (False, False),
])
def test_superuser_exemption_reads_the_usual_shapes(raw, expected):
    assert policy.superuser_exempt(raw) is expected


# ── [결재 로그] — 전 사용자 열람 ──────────────────────────────────────


class LogDB(PolicyDB):
    """search() 의 SQL 까지 이해하는 메모리 DB."""

    def _run(self, s, p):
        if s.startswith("SELECT COUNT(*) AS n"):
            return [{"n": len(self._match(s, p))}]
        if s.startswith("SELECT r.id, r.title, r.action_type, r.status, r.created_at, r.decided_at,"):
            rows = self._match(s, p[:-2])
            rows.sort(key=lambda r: r["id"], reverse=True)
            return [dict(r, requester_username=None, requester_name=None,
                         waiting_on=None, step_count=1)
                    for r in rows[p[-1]:p[-1] + p[-2]]]
        return super()._run(s, p)

    def _match(self, sql, params):
        rows = list(self.t["approval_requests"])
        i = 0
        if "r.status IN" in sql:
            n = sql.split("r.status IN (")[1].split(")")[0].count("%s")
            wanted, i = params[i:i + n], i + n
            rows = [r for r in rows if r["status"] in wanted]
        if "r.action_type IN" in sql:
            n = sql.split("r.action_type IN (")[1].split(")")[0].count("%s")
            wanted, i = params[i:i + n], i + n
            rows = [r for r in rows if r["action_type"] in wanted]
        if "r.title ILIKE" in sql:
            needle = params[i].strip("%")
            i += 4
            rows = [r for r in rows if needle in (r["title"] or "")
                    or needle in (r.get("target_ref") or "")]
        return rows


@pytest.fixture
def logdb():
    from xgen_sdk.approval import store
    d = LogDB()
    for uid, name in ((1, "기안자"), (3, "팀장")):
        d.add_user(uid, name, name)
    for title, action in (("배포 A", "agent.deploy"), ("배포 B", "agent.deploy"),
                          ("컬렉션 C", "collection.create")):
        store.submit(d, requester_id=1, title=title, action_type=action,
                     target_ref=f"t:{title}",
                     steps=[{"approver_id": 3, "step_order": 1}])
    return d


def test_the_log_sees_everyone(logdb):
    """기안자도 결재자도 아닌 관리자가 전부 본다 — 그래서 권한이 필요하다."""
    from xgen_sdk.approval import store
    out = store.search(logdb)
    assert out["total"] == 3 and len(out["rows"]) == 3


def test_the_log_filters_by_action(logdb):
    from xgen_sdk.approval import store
    out = store.search(logdb, action_types=["agent.deploy"])
    assert out["total"] == 2


def test_the_log_filters_by_status(logdb):
    from xgen_sdk.approval import store
    rows = store.search(logdb)["rows"]
    store.decide(logdb, rows[0]["id"], actor_id=3, action="approved")
    assert store.search(logdb, statuses=["approved"])["total"] == 1
    assert store.search(logdb, statuses=["pending"])["total"] == 2


def test_the_log_searches_title_and_target(logdb):
    from xgen_sdk.approval import store
    assert store.search(logdb, query="컬렉션")["total"] == 1
    assert store.search(logdb, query="배포")["total"] == 2


def test_the_log_pages(logdb):
    from xgen_sdk.approval import store
    first = store.search(logdb, limit=2)
    assert first["total"] == 3 and len(first["rows"]) == 2
    second = store.search(logdb, limit=2, offset=2)
    assert len(second["rows"]) == 1
    assert {r["id"] for r in first["rows"]} & {r["id"] for r in second["rows"]} == set()


def test_the_log_is_newest_first(logdb):
    from xgen_sdk.approval import store
    ids = [r["id"] for r in store.search(logdb)["rows"]]
    assert ids == sorted(ids, reverse=True)


# ── 결재선은 요청하는 사람이 그 자리에서 고른다 ──────────────────────


def test_a_gate_can_be_turned_on_without_a_default_line(db):
    """**관리자가 모든 행위의 결재선을 미리 정해 둘 이유가 없다.**

    한때 "결재선을 고를 자리가 없는 행위는 기본 결재선 없이 켤 수 없다" 는
    제약을 뒀었다. 자리가 없으면 만들면 되는 것이지, 켜는 것 자체를 막을 일이
    아니다 — 지금은 요청하는 화면이 결재선 모달을 띄운다.
    """
    policy.set_policy(db, "collection.create", actor_id=1, required=True)
    assert policy.is_required(db, "collection.create") is True
    assert policy.get(db, "collection.create")["default_line_id"] is None


def test_the_requesters_own_steps_win(db):
    """요청자가 그 자리에서 고른 결재선이 언제나 이긴다 — 기본값은 미리
    채워 주는 것이지 강제가 아니다. 잠그면 부서가 다른 사람이 남의 결재선을 탄다."""
    policy.set_policy(db, "collection.create", actor_id=1, default_line_id=9)
    line, steps = policy.resolve_line(
        db, "collection.create", steps=[{"approver_id": 3, "step_order": 1}])
    assert line is None and steps == [{"approver_id": 3, "step_order": 1}]


def test_an_explicit_line_also_wins(db):
    policy.set_policy(db, "collection.create", actor_id=1, default_line_id=9)
    assert policy.resolve_line(db, "collection.create", line_id=4) == (4, None)


def test_the_default_line_fills_in_when_nothing_was_picked(db):
    policy.set_policy(db, "collection.create", actor_id=1, default_line_id=9)
    assert policy.resolve_line(db, "collection.create") == (9, None)


def test_nothing_anywhere_asks_the_user(db):
    """**실패가 아니라 한 단계 덜 온 것**이다 — 화면은 오류창이 아니라
    결재선 모달을 띄우고, 사용자가 고르면 같은 요청이 그대로 진행된다."""
    from xgen_sdk.approval.engine import ApprovalLineRequired

    with pytest.raises(ApprovalLineRequired) as err:
        policy.resolve_line(db, "collection.create")
    assert err.value.action_type == "collection.create"
    assert err.value.action_label == "지식 컬렉션 생성", "화면에 띄울 이름이 없다"


def test_the_line_required_error_is_still_an_approval_error(db):
    """호출부가 ApprovalError 하나로 잡고 있어도 흐름이 깨지지 않아야 한다."""
    from xgen_sdk.approval.engine import ApprovalError, ApprovalLineRequired

    assert issubclass(ApprovalLineRequired, ApprovalError)


def test_the_screen_can_ask_what_needs_approval_before_acting(db):
    """화면이 **행동 전에** 알아야 사용자가 요청을 보낸 뒤 거절당하는 대신
    처음부터 결재선을 고르고 보낼 수 있다."""
    policy.set_policy(db, "collection.create", actor_id=1, required=True)
    out = policy.required_for(db)
    assert out["collection.create"] is True
    assert out["tool.publish"] is False
    assert set(out) == {s.action_type for s in catalog.gated_specs()}


def test_required_for_can_be_narrowed(db):
    policy.set_policy(db, "tool.publish", actor_id=1, required=True)
    assert policy.required_for(db, ["tool.publish"]) == {"tool.publish": True}



# ── 결재선 고정 ───────────────────────────────────────────────────────
#
# 관리자는 결재선을 **고정**할 수 있다. 고정이면 요청자는 그 줄로만 올린다.
# 고정하지 않으면(기본) 기본 결재선은 모달을 미리 채우는 편의일 뿐이고
# 요청자가 바꾼다. 잠그는 것은 관리자의 명시적 선택이어야 한다.


def _line(db, name="L", approvers=(1, 2)):
    from xgen_sdk.approval import store

    db.add_user(1, "kim"); db.add_user(2, "lee"); db.add_user(3, "park")
    return store.create_line(
        db, name=name, description="", owner_id=9, is_shared=True,
        steps=[{"approver_id": a, "step_order": i + 1} for i, a in enumerate(approvers)],
    )["id"]


def test_a_line_is_not_locked_by_default(db):
    """기본은 무제한 — 결재선을 정해 둬도 요청자가 바꿀 수 있다."""
    lid = _line(db)
    policy.set_policy(db, "collection.create", actor_id=9, required=True, default_line_id=lid)
    assert policy.get(db, "collection.create")["line_locked"] is False
    # 요청자가 다른 사람을 골라 왔다 — 그것이 이긴다
    got = policy.resolve_line(db, "collection.create",
                              steps=[{"approver_id": 3, "step_order": 1}])
    assert got == (None, [{"approver_id": 3, "step_order": 1}])


def test_nothing_can_be_locked_without_a_line(db):
    """아무것도 없는 것을 고정할 수는 없다."""
    with pytest.raises(ValueError):
        policy.set_policy(db, "collection.create", actor_id=9, line_locked=True)


def test_a_locked_line_is_the_only_line(db):
    lid = _line(db)
    policy.set_policy(db, "collection.create", actor_id=9, required=True,
                      default_line_id=lid, line_locked=True)
    assert policy.get(db, "collection.create")["line_locked"] is True
    # 아무것도 안 들고 와도 그 줄로 간다 — 모달이 고르는 자리를 안 보여 준다
    assert policy.resolve_line(db, "collection.create") == (lid, None)
    # 같은 줄을 들고 오는 것은 괜찮다
    assert policy.resolve_line(db, "collection.create", line_id=lid) == (lid, None)


def test_a_different_line_is_refused_when_locked(db):
    """조용히 바꿔치기하면 화면은 '내가 고른 사람에게 갔다' 고 믿는데 실제로는
    다른 사람에게 가 있다. 소리를 내야 한다."""
    from xgen_sdk.approval.engine import ApprovalError

    lid = _line(db)
    other = _line(db, "other", approvers=(3,))
    policy.set_policy(db, "collection.create", actor_id=9, required=True,
                      default_line_id=lid, line_locked=True)
    with pytest.raises(ApprovalError):
        policy.resolve_line(db, "collection.create", line_id=other)
    with pytest.raises(ApprovalError):
        policy.resolve_line(db, "collection.create",
                            steps=[{"approver_id": 3, "step_order": 1}])


def test_clearing_the_line_also_unlocks(db):
    """줄을 지우면 고정도 풀린다 — '고정됐는데 아무것도 없는' 상태를 남기지 않는다."""
    lid = _line(db)
    policy.set_policy(db, "collection.create", actor_id=9, default_line_id=lid, line_locked=True)
    policy.set_policy(db, "collection.create", actor_id=9, default_line_id=None)
    row = policy.get(db, "collection.create")
    assert row["default_line_id"] is None
    assert row["line_locked"] is False


def test_locking_is_recorded(db):
    """[결재 로그 → 설정 변경 이력] 이 '그날 왜 그 사람들에게 갔나' 를 답해야 한다."""
    lid = _line(db)
    policy.set_policy(db, "collection.create", actor_id=9, default_line_id=lid, line_locked=True)
    targets = [h["target"] for h in policy.history(db)]
    assert "collection.create:line_locked" in targets


def test_the_screen_can_see_which_lines_are_locked(db):
    lid = _line(db)
    policy.set_policy(db, "collection.create", actor_id=9, default_line_id=lid, line_locked=True)
    policy.set_policy(db, "db.create", actor_id=9, default_line_id=lid)   # 고정 안 함
    assert policy.locked_lines(db) == {"collection.create": lid}
