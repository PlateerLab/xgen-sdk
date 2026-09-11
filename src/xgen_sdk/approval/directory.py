"""결재자를 찾는 길 — **부서는 필터일 뿐이다.**

무엇이 잘못됐었나
-----------------
처음에는 "일반 사용자는 역할로만 훑고, 전수 검색은 관리자만" 으로 갈랐다.
그랬더니 **역할이 없는 사람은 결재선에 세울 수가 없었다** — 어느 부서에도
속하지 않은 사람, 아직 배정 전인 사람이 전부 그렇다. 결재는 조직도가 아니라
**사람**에게 올리는 것인데, 조직도에 없으면 존재하지 않는 것으로 취급한 셈이다.

지금
----
사람 목록은 하나다. ``role_id`` 는 **좁히는 조건**이고(없으면 전체), ``query``
는 이름·아이디로 더 좁힌다. 둘 다 선택이라 아무것도 주지 않으면 전원이 나온다 —
부서를 모르는 사람도 이름만으로, 부서만 아는 사람도 부서만으로 찾는다.

권한을 걸지 않는 이유는 결재 자체와 같다: 결재선에 세울 수 없는 사람이 있으면
결재선을 짤 수 없다.

무엇으로 찾히나
---------------
이름 · 아이디 · 메일 · 부서 · 본부를 한꺼번에 훑는다. 사람마다 기억하는 단서가
다르다 — 이름만 아는 사람, 아이디만 아는 사람, 메일 앞부분만 아는 사람. 한
축만 열어 두면 나머지 사람은 **"없는 사람"** 을 보게 된다.

찾히기만 하고 **이메일 값 자체는 내보내지 않는다**. 찾는 데 쓰는 것과 화면에
뿌리는 것은 다른 문제다 — 결재자를 고르는 데 필요한 것은 이름과 소속이다.

누가 빠지나
-----------
**로그인할 수 없는 계정**(비활성·미승인)은 목록에 없다. 결재선에 세우면 그
차례에서 결재가 영영 멈추기 때문이다 — 본인이 들어와서 누를 수가 없다.
그 정지는 화면 어디에도 사유로 남지 않아 기안자는 "왜 안 넘어가지" 만 반복한다.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from xgen_sdk.approval.sql import q as _q


def list_roles(app_db) -> List[Dict[str, Any]]:
    """역할 목록 + 인원수 — 사람 목록을 **좁히는 선택지**다.

    화면에서 이 목록 앞에 "전체" 가 붙는다. 부서를 고르지 않아도 사람을 고를 수
    있어야 하기 때문이다.
    """
    return _q(
        app_db,
        """
        SELECT r.id, r.name, r.display_name, r.description,
               COUNT(ur.user_id) AS member_count
          FROM roles r
          LEFT JOIN user_roles ur ON ur.role_id = r.id
         GROUP BY r.id, r.name, r.display_name, r.description
         ORDER BY r.display_name, r.name
        """,
    )


#: 결재선에 세울 수 있는 사람 — **로그인할 수 있는 사람만.**
#:
#: 비활성·미승인 계정을 결재선에 세우면 그 차례에서 결재가 **영영 멈춘다**.
#: 본인이 들어와서 누를 수가 없기 때문이다. 그 정지는 화면 어디에도 사유로
#: 남지 않아서, 기안자는 "왜 안 넘어가지" 만 반복하게 된다.
#: 판정 기준은 로그인 판정과 **같은 것**을 쓴다(user.is_user_login_allowed).
_ACTIVE = "u.is_active = TRUE AND u.status = '1'"

#: 이름으로 찾을 때 훑는 칸들. 사람마다 기억하는 단서가 다르다 — 이름만 아는
#: 사람, 아이디만 아는 사람, 메일 주소만 아는 사람, 부서만 아는 사람.
#: 하나만 열어 두면 나머지 사람은 "없는 사람" 을 보게 된다.
_SEARCH_COLUMNS = (
    "u.full_name",       # 이름
    "u.username",        # 아이디
    "u.email",           # 메일 (앞부분만 쳐도 걸린다)
    "u.department",      # 부서
    "u.top_department",  # 상위 부서(본부)
)


def list_users(
    app_db,
    role_id: Optional[int] = None,
    query: str = "",
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """결재선에 세울 수 있는 사람들.

    ``role_id`` 도 ``query`` 도 **선택**이다. 아무것도 주지 않으면 (활성인)
    전원이 나온다 — 역할이 없는 사람이 목록에서 빠지면 그 사람에게는 결재를
    올릴 수 없게 된다.

    ``query`` 는 이름·아이디·메일·부서·본부를 한꺼번에 훑는다. 사람마다
    기억하는 단서가 다르기 때문이다.
    """
    where: List[str] = [_ACTIVE]
    params: List[Any] = []

    if role_id:
        where.append("EXISTS (SELECT 1 FROM user_roles ur "
                     "WHERE ur.user_id = u.id AND ur.role_id = %s)")
        params.append(int(role_id))

    q = str(query or "").strip()
    if q:
        like = f"%{q}%"
        where.append("(" + " OR ".join(f"{c} ILIKE %s" for c in _SEARCH_COLUMNS) + ")")
        params += [like] * len(_SEARCH_COLUMNS)

    params.append(max(1, min(int(limit), 500)))
    rows = _q(
        app_db,
        f"""
        SELECT u.id, u.username, u.full_name, u.department, u.top_department
          FROM users u
         WHERE {" AND ".join(where)}
         ORDER BY u.full_name NULLS LAST, u.username
         LIMIT %s
        """,
        params,
    )
    return _attach_roles(app_db, rows)


def _attach_roles(app_db, users: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """각 사용자의 역할 이름 — 동명이인을 가르는 것은 결국 소속이다.

    부서(``department``)가 있으면 그쪽이 더 정확한 소속이다. 역할은 권한
    묶음이라 "role_hrj" 같은 내부 이름일 때가 있어서, 화면은 부서를 먼저
    보여 주고 없을 때 역할로 떨어진다.
    """
    if not users:
        return users
    ids = [int(u["id"]) for u in users]
    placeholders = ",".join(["%s"] * len(ids))
    rows = _q(
        app_db,
        f"""
        SELECT ur.user_id, r.display_name, r.name
          FROM user_roles ur JOIN roles r ON r.id = ur.role_id
         WHERE ur.user_id IN ({placeholders})
        """,
        ids,
    )
    by_user: Dict[int, List[str]] = {}
    for r in rows:
        by_user.setdefault(int(r["user_id"]), []).append(r.get("display_name") or r.get("name"))
    for u in users:
        u["roles"] = by_user.get(int(u["id"]), [])
    return users


def resolve_names(app_db, user_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """id → 표시용 정보. 결재선에 담긴 id 를 화면에 보여 줄 때 쓴다."""
    ids = [int(i) for i in dict.fromkeys(user_ids or [])]
    if not ids:
        return {}
    placeholders = ",".join(["%s"] * len(ids))
    rows = _q(
        app_db,
        f"SELECT id, username, full_name FROM users WHERE id IN ({placeholders})",
        ids,
    )
    return {int(r["id"]): r for r in rows}
