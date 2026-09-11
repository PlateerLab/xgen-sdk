"""결재 알림 — 기존 알림 체계를 **제대로** 타는가.

여기서 보는 것 넷:

  1. 차례가 온 사람에게만 간다 (아직 차례가 아닌 사람에게 미리 가지 않는다)
  2. 건마다 **따로** 쌓인다 — 덮어쓰지 않는다. 배포 알림은 (user, template) 로
     upsert 하지만 결재는 건마다 별개의 일이고 링크도 다르다. 덮어쓰면 먼저 온
     결재의 알림이 사라져서 그 건은 아무도 모르는 채로 남는다.
  3. 종결은 기안자에게 간다 (승인/거절). 회수는 알리지 않는다 — 회수한 사람이
     기안자 본인이라 자기가 방금 한 일을 자기에게 알리는 꼴이다.
  4. **알림이 실패해도 결재는 그대로다.** 알림은 결재의 부산물이라, 알림 때문에
     사람이 이미 누른 승인이 사라지면 안 된다.
"""
from __future__ import annotations

import pytest

from xgen_sdk.approval import store
from xgen_sdk.approval import notifier
from test_approval_store import FakeDB


class NotifyDB(FakeDB):
    """알림까지 받아 적는 메모리 DB.

    알림은 **모델이 아니라 SQL 로** 들어온다 — notifier 가 core 밖(workflow·
    documents 파드)에서도 돌아야 해서 모델 클래스에 기대지 않는다. 그래서 이
    가짜 DB 도 같은 SQL 을 받는다.
    """

    #: 템플릿 표에 link_url 이 있는가. ``None`` = 표가 비어 있다(코드 기본값 경로).
    template_link = None

    def __init__(self, insert_raises: bool = False):
        super().__init__()
        self.notifications: list = []
        self._insert_raises = insert_raises

    def _run(self, s, p):
        if s.startswith("INSERT INTO user_notifications"):
            if self._insert_raises:
                raise RuntimeError("알림 표가 잠겼습니다")
            self.notifications.append({
                "user_id": p[0], "template_id": p[1], "title": p[2],
                "message": p[3], "link_url": p[4],
            })
            return []
        if s.startswith("SELECT link_url FROM notification_message_templates"):
            return [] if self.template_link is None else [{"link_url": self.template_link}]
        if s.startswith("SELECT title, content FROM notification_message_template_translations"):
            if self.template_link is None:
                return []
            return [{"title": "결재 요청 — {{title}}", "content": "{{requester}} 님의 결재"}]
        return super()._run(s, p)


@pytest.fixture
def db():
    d = NotifyDB()
    for uid, name in ((1, "기안자"), (3, "팀장"), (4, "부장"), (9, "제삼자")):
        d.add_user(uid, name, name)
    return d


def _submit(db, steps=((3, 1), (4, 2))):
    return store.submit(db, requester_id=1, title="서버 증설", reason="부하",
                        steps=[{"approver_id": u, "step_order": o} for u, o in steps])


def by_template(db, tid):
    return [n for n in db.notifications if n["template_id"] == tid]


# ── 1. 차례가 온 사람에게만 ───────────────────────────────────────────


def test_only_the_first_approver_is_told_on_submit(db):
    _submit(db)
    turns = by_template(db, notifier.TEMPLATE_TURN)
    assert [n["user_id"] for n in turns] == [3], "차례가 아닌 사람에게 미리 갔다"


def test_the_next_approver_is_told_when_the_turn_moves(db):
    r = _submit(db)
    db.notifications.clear()
    store.decide(db, r["id"], actor_id=3, action="approved")
    assert [n["user_id"] for n in by_template(db, notifier.TEMPLATE_TURN)] == [4]


def test_only_one_person_is_told_at_a_time(db):
    """결재선은 한 줄이다 — 같은 번호를 줘도 한 사람씩 간다.

    전원에게 한꺼번에 알리면 아직 차례가 아닌 사람이 열었다가 버튼이 없어
    되돌아간다. 그런 알림은 두 번째부터 안 읽힌다.
    """
    _submit(db, steps=((3, 1), (4, 1)))
    told = [n["user_id"] for n in by_template(db, notifier.TEMPLATE_TURN)]
    assert told == [3], f"한 번에 한 사람이어야 하는데 {told} 에게 갔다"


def test_the_next_person_is_told_only_after_the_previous_one_approves(db):
    r = _submit(db, steps=((3, 1), (4, 2), (9, 3)))
    db.notifications.clear()
    store.decide(db, r["id"], actor_id=3, action="approved")
    told = [n["user_id"] for n in by_template(db, notifier.TEMPLATE_TURN)]
    assert told == [4], f"바로 다음 한 사람에게만 가야 하는데 {told}"


def test_the_link_points_at_that_request(db):
    """알림을 눌렀을 때 **그 건**이 열려야 한다 — 목록만 열면 다시 찾아야 한다."""
    r = _submit(db)
    link = by_template(db, notifier.TEMPLATE_TURN)[0]["link_url"]
    assert f"request={r['id']}" in link and "view=approval" in link


# ── 2. 덮어쓰지 않는다 ────────────────────────────────────────────────


def test_two_requests_leave_two_notifications(db):
    """**덮어쓰면 먼저 온 결재는 아무도 모르는 채로 남는다.**

    배포 알림은 (user, template) 로 upsert 하지만 결재는 그러면 안 된다 —
    건마다 따로 처리해야 하는 일이고 링크도 건마다 다르다.
    """
    a = _submit(db)
    b = _submit(db)
    turns = by_template(db, notifier.TEMPLATE_TURN)
    assert len(turns) == 2, "두 번째 결재 알림이 첫 번째를 덮었다"
    links = {n["link_url"] for n in turns}
    assert f"request={a['id']}" in " ".join(links)
    assert f"request={b['id']}" in " ".join(links)


# ── 3. 종결은 기안자에게 ──────────────────────────────────────────────


def test_the_requester_is_told_when_it_is_approved(db):
    r = _submit(db, steps=((3, 1),))
    db.notifications.clear()
    store.decide(db, r["id"], actor_id=3, action="approved")
    done = by_template(db, notifier.TEMPLATE_APPROVED)
    assert [n["user_id"] for n in done] == [1]
    assert "서버 증설" in done[0]["title"]


def test_the_requester_is_told_when_it_is_rejected_and_why(db):
    r = _submit(db, steps=((3, 1),))
    db.notifications.clear()
    store.decide(db, r["id"], actor_id=3, action="rejected", note="예산 초과")
    no = by_template(db, notifier.TEMPLATE_REJECTED)
    assert [n["user_id"] for n in no] == [1]
    assert "예산 초과" in no[0]["message"], "거절 사유가 알림에 없으면 다시 물어봐야 한다"
    assert "팀장" in no[0]["message"], "누가 거절했는지 없으면 누구에게 물어볼지 모른다"


def test_mid_line_approval_does_not_tell_the_requester_it_is_done(db):
    r = _submit(db)
    db.notifications.clear()
    store.decide(db, r["id"], actor_id=3, action="approved")
    assert by_template(db, notifier.TEMPLATE_APPROVED) == [], "1차 승인을 완료로 알렸다"


def test_cancel_is_not_announced(db):
    """회수한 사람이 기안자 본인이다 — 자기가 방금 한 일을 자기에게 알리지 않는다."""
    r = _submit(db)
    db.notifications.clear()
    store.cancel(db, r["id"], actor_id=1)
    assert db.notifications == []


def test_an_apply_failure_is_announced_to_the_requester(db):
    """이 알림이 없으면 '승인됐으나 적용 실패' 는 영영 묻힌다."""
    from xgen_sdk.approval import registry

    registry.register_action("t.noti_boom",
                             lambda p, q: (_ for _ in ()).throw(RuntimeError("대상 없음")),
                             label="터지는 것")
    r = store.submit(db, requester_id=1, title="증설", action_type="t.noti_boom",
                     steps=[{"approver_id": 3, "step_order": 1}])
    db.notifications.clear()
    store.decide(db, r["id"], actor_id=3, action="approved")

    fail = by_template(db, notifier.TEMPLATE_APPLY_FAIL)
    assert len(fail) == 1 and fail[0]["user_id"] == 1
    assert "대상 없음" in fail[0]["message"]


# ── 4. 알림이 실패해도 결재는 그대로 ──────────────────────────────────


def test_a_broken_notifier_does_not_break_the_approval():
    """**알림은 결재의 부산물이다.**

    알림 때문에 500 이 나가면 화면은 실패로 읽는데, 서버에는 승인이 이미
    기록돼 있다 — 사람이 누른 승인이 사라지는 것보다 알림을 잃는 편이 낫다.
    """
    db = NotifyDB(insert_raises=True)
    for uid, name in ((1, "기안자"), (3, "팀장")):
        db.add_user(uid, name, name)

    r = store.submit(db, requester_id=1, title="증설",
                     steps=[{"approver_id": 3, "step_order": 1}])
    assert r["status"] == "pending", "알림 실패가 상신을 막았다"

    out = store.decide(db, r["id"], actor_id=3, action="approved")
    assert out["status"] == "approved", "알림 실패가 승인을 막았다"
    assert out["steps"][0]["status"] == "approved"


def test_a_missing_template_falls_back_to_code_defaults(db):
    """시드는 운영팀이 문구를 다듬으라고 있는 것이지 동작의 전제가 아니다."""
    _submit(db, steps=((3, 1),))
    turn = by_template(db, notifier.TEMPLATE_TURN)[0]
    assert "서버 증설" in turn["title"]
    assert "기안자" in turn["message"], "치환이 안 되면 {{requester}} 가 그대로 보인다"
    assert "{{" not in turn["title"] and "{{" not in turn["message"]


# ── 알림을 누르면 **그 건**이 열려야 한다 ─────────────────────────────
#
# 2026-09-11 검토에서 잡은 것 셋. 셋 다 "알림은 갔는데 눌러도 결재가 안 열린다"
# 로 보였고, 서버 로그에는 아무것도 남지 않았다.


class TemplateDB(NotifyDB):
    """시드가 이미 돈 상태 — 템플릿에 link_url 이 있다."""

    def __init__(self, link_url="/mypage?view=approval"):
        super().__init__()
        self.template_link = link_url


def test_the_link_keeps_the_request_id_even_when_the_template_has_a_link():
    """**시드가 한 번 돌고 나면 언제나 템플릿 링크가 있다.**

    예전 코드는 템플릿 링크가 있으면 그대로 썼다 — 그래서 실제 운영에서는
    모든 알림이 건 번호 없이 목록만 가리켰다. 눌러 봐야 방금 읽은 건을 다시
    찾아야 하고, 결재함에 열 건이 쌓여 있으면 그게 제일 성가신 일이다.
    """
    db = TemplateDB()
    for uid, name in ((1, "기안자"), (3, "팀장")):
        db.add_user(uid, name, name)
    r = store.submit(db, requester_id=1, title="증설",
                     steps=[{"approver_id": 3, "step_order": 1}])
    turn = by_template(db, notifier.TEMPLATE_TURN)[0]
    assert f"request={r['id']}" in turn["link_url"], turn["link_url"]


def test_the_link_points_at_mypage_not_main():
    """화면은 마이페이지 [결재 관리] 에 있다. /main 은 죽은 자리다."""
    assert notifier._LINK_BASE.startswith("/mypage?")
    assert notifier._link(7).startswith("/mypage?view=approval")
    assert "request=7" in notifier._link(7)


def test_a_template_base_without_a_query_string_gets_a_question_mark():
    assert notifier._link(7, base="/mypage") == "/mypage?request=7"
    assert notifier._link(7, base="/mypage?view=approval") == "/mypage?view=approval&request=7"


def test_settled_and_apply_failed_links_also_carry_the_request_id():
    from xgen_sdk.approval import registry

    registry.register_action("t.link_boom",
                             lambda p, q: (_ for _ in ()).throw(RuntimeError("x")),
                             label="터짐")
    db = TemplateDB()
    for uid, name in ((1, "기안자"), (3, "팀장")):
        db.add_user(uid, name, name)
    r = store.submit(db, requester_id=1, title="증설", action_type="t.link_boom",
                     steps=[{"approver_id": 3, "step_order": 1}])
    db.notifications.clear()
    store.decide(db, r["id"], actor_id=3, action="approved")
    for n in db.notifications:
        assert f"request={r['id']}" in n["link_url"], (n["template_id"], n["link_url"])


# (시드 쪽 검증 — 표 CHECK·template_id 길이·커버리지·옛 링크 바로잡기 — 은
#  **core** 가 본다. 시드는 core 의 마이그레이션이다:
#  xgen-core/tests/test_approval_wiring.py)
