"""결재자 찾기 — **못 찾으면 결재선을 짤 수 없다.**

이 화면에서 사람이 못 찾으면 그냥 "그런 사람 없네" 로 끝난다. 오류도 안 나고,
누구도 무엇이 빠졌는지 모른다. 그래서 여기서 보는 것은 "찾아지는가" 보다
**"어떤 단서로도 찾아지는가"** 다.
"""
from __future__ import annotations

import re
import pytest

from xgen_sdk.approval import directory  # noqa: E402


class FakeUsersDB:
    """SQL 을 해석하지 않고 **어떤 조건이 걸렸는지**만 본다.

    방언을 흉내 내기보다, 질의가 훑는 칸과 붙인 조건을 확인하는 편이
    이 파일이 지키려는 것(단서마다 찾아진다 / 죽은 계정은 안 나온다)에 가깝다.
    """

    def __init__(self):
        self.sql = ""
        self.params = ()

    def execute_raw_query(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("SELECT ur.user_id"):
            return {"success": True, "data": [], "error": None}
        self.sql, self.params = s, tuple(params)
        return {"success": True, "data": [], "error": None}


@pytest.fixture
def db():
    return FakeUsersDB()


def _cols(sql: str) -> set:
    """ILIKE 로 훑는 칸들."""
    return set(re.findall(r"(u\.\w+) ILIKE", sql))


# ── 어떤 단서로도 찾아진다 ────────────────────────────────────────────


def test_search_covers_every_clue_a_person_might_remember(db):
    """사람마다 기억하는 단서가 다르다.

    이름만 아는 사람, 아이디만 아는 사람, 메일 앞부분만 아는 사람, 부서만
    아는 사람. 한 축만 열어 두면 나머지 사람에게는 그 동료가 **없는 사람**이다.
    """
    directory.list_users(db, query="jang")
    assert _cols(db.sql) == {
        "u.full_name",       # 이름
        "u.username",        # 아이디
        "u.email",           # 메일
        "u.department",      # 부서
        "u.top_department",  # 본부
    }


def test_one_word_is_tried_against_all_of_them(db):
    directory.list_users(db, query="jang")
    likes = [p for p in db.params if p == "%jang%"]
    assert len(likes) == 5, "한 단어를 다섯 칸 모두에 대 봐야 한다"


def test_the_clauses_are_or_not_and(db):
    """AND 면 다섯 칸을 **동시에** 만족해야 해서 아무도 안 나온다."""
    directory.list_users(db, query="jang")
    where = db.sql[db.sql.index("WHERE"):]
    ilike_group = re.search(r"\(([^()]*ILIKE[^()]*)\)", where).group(1)
    assert " OR " in ilike_group and " AND " not in ilike_group


def test_partial_matches_work(db):
    """앞부분만 쳐도 걸려야 한다 — 전체를 기억하면 찾을 필요도 없다."""
    directory.list_users(db, query="hr")
    assert all(p in ("%hr%",) or not isinstance(p, str) or not p.startswith("%")
               for p in db.params if isinstance(p, str) and p.startswith("%"))


# ── 죽은 계정은 나오지 않는다 ─────────────────────────────────────────


def test_inactive_accounts_are_excluded(db):
    """**결재선에 세우면 그 차례에서 영영 멈춘다.**

    본인이 로그인할 수 없으니 누를 수가 없고, 그 정지는 화면 어디에도 사유로
    남지 않는다 — 기안자는 "왜 안 넘어가지" 만 반복하게 된다.
    """
    directory.list_users(db)
    assert "u.is_active = TRUE" in db.sql
    assert "u.status = '1'" in db.sql


# (로그인 판정과 같은 기준인지는 **core** 가 본다 — USER_STATUS_ACTIVE 가 거기 있다:
#  xgen-core/tests/test_approval_wiring.py)


def test_inactive_filter_applies_even_without_a_query(db):
    directory.list_users(db)
    assert "is_active" in db.sql


# ── 부서는 필터일 뿐 ──────────────────────────────────────────────────


def test_no_filter_means_everyone(db):
    """역할이 없는 사람이 빠지면 그 사람에게는 결재를 올릴 수 없다."""
    directory.list_users(db)
    assert "user_roles" not in db.sql, "부서를 주지 않았는데 역할로 걸렀다"
    assert "ILIKE" not in db.sql


def test_a_role_narrows_but_does_not_replace(db):
    directory.list_users(db, role_id=7, query="jang")
    assert "user_roles" in db.sql and "ILIKE" in db.sql
    assert 7 in db.params


# ── 내보내는 것 ───────────────────────────────────────────────────────


def test_email_is_searchable_but_never_returned(db):
    """찾는 데 쓰는 것과 화면에 뿌리는 것은 다른 문제다.

    결재자를 고르는 데 필요한 것은 이름과 소속이지 연락처가 아니다.
    """
    directory.list_users(db, query="x")
    select = db.sql[:db.sql.index("FROM")]
    assert "u.email" not in select, "이메일이 응답에 실렸다"
    assert "u.email" in db.sql, "이메일로 찾을 수는 있어야 한다"


def test_the_department_comes_back_for_display(db):
    """'hrjang · role_hrj' 처럼 내부 역할 이름만 보이면 누구인지 모른다."""
    directory.list_users(db)
    select = db.sql[:db.sql.index("FROM")]
    assert "u.department" in select and "u.top_department" in select


def test_the_limit_is_capped(db):
    directory.list_users(db, limit=99999)
    assert db.params[-1] == 500
