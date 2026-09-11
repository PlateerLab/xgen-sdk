"""결재 알림 — 기존 알림 체계(``user_notifications``)를 그대로 탄다.

왜 알림이 필요한가
------------------
결재함을 주기적으로 열어 보는 사람은 없다. 알림이 없으면 결재는 "아무도
반대하지 않아서" 가 아니라 **"아무도 몰라서"** 며칠씩 멈춘다. 그 정지는
화면 어디에도 사유로 남지 않으므로 가장 나쁜 종류의 지연이다.

이 체계에 어떻게 얹히나
-----------------------
새 알림 통로를 만들지 않았다. 배포·정책 알림과 **같은 표(user_notifications)**,
같은 템플릿 체계(``notification_message_templates`` + ko 번역), 같은 치환
규칙(``{{변수}}``)을 쓴다. 그래서 운영팀이 관리 화면에서 결재 알림 문구도
똑같이 다듬을 수 있고, 사용자의 알림함에서 다른 알림과 섞여 시간순으로 보인다.

한 가지만 다르게 한다 — **upsert 가 아니라 insert**
----------------------------------------------------
``deploy_notifier._upsert_notification`` 은 ``(user_id, template_id)`` 로 덮어쓴다.
"승인 대기 N건" 같은 요약 알림에는 맞지만 결재에는 맞지 않는다: 결재는 건마다
**따로 처리해야 하는 별개의 일**이고 링크도 건마다 다르다. 덮어쓰면 먼저 온
결재의 알림이 사라져서, 그 건은 아무도 모르는 채로 남는다.

실패해도 결재를 막지 않는다
---------------------------
알림은 결재의 **부산물**이다. 알림을 못 보냈다고 승인이 실패하면, 사람이 이미
누른 승인이 사라진다. 그래서 모든 함수가 예외를 삼키고 로그만 남긴다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from xgen_sdk.approval.sql import q as _q

logger = logging.getLogger("notification.approval")

#: 결재 화면으로 가는 링크. ``request`` 를 붙이면 그 건이 바로 열린다.
#:
#: **마이페이지**다. 처음엔 Agent 제작 사이드바(/main)에 두었다가 옮겼는데
#: 알림 링크는 옛 자리를 가리킨 채 남아 있었다 — 알림을 눌러도 결재가 안 열리는
#: 상태였다(2026-09-11 검토에서 잡음). 화면이 또 옮겨지면 여기와 시드의
#: ``_LINK`` 를 같이 바꿔야 한다 — 그래서 시드 테스트가 둘이 같은지 본다.
_LINK_BASE = "/mypage?view=approval"

TEMPLATE_TURN = "APV-TURN"      # 내 차례가 왔다
TEMPLATE_APPROVED = "APV-OK"    # 내가 올린 건이 승인 완료
TEMPLATE_REJECTED = "APV-NO"    # 내가 올린 건이 거절
TEMPLATE_APPLY_FAIL = "APV-ERR"  # 승인은 됐는데 적용이 실패

#: 템플릿이 DB 에 없어도 알림은 나가야 한다 — 시드는 "운영팀이 다듬을 수 있게"
#: 올려 두는 것이지 동작의 전제가 아니다.
DEFAULTS: Dict[str, tuple] = {
    TEMPLATE_TURN: (
        "결재 요청 — {{title}}",
        "{{requester}} 님이 올린 결재 '{{title}}' 이(가) 회원님 차례입니다. "
        "결재 화면에서 승인 또는 거절해 주세요.",
    ),
    TEMPLATE_APPROVED: (
        "결재 승인 완료 — {{title}}",
        "올리신 결재 '{{title}}' 이(가) 모든 결재자의 승인을 받았습니다.",
    ),
    TEMPLATE_REJECTED: (
        "결재 거절 — {{title}}",
        "올리신 결재 '{{title}}' 이(가) {{actor}} 님에 의해 거절되었습니다. "
        "사유: {{note}}",
    ),
    TEMPLATE_APPLY_FAIL: (
        "결재는 승인됐지만 적용에 실패했습니다 — {{title}}",
        "'{{title}}' 은(는) 모든 결재자의 승인을 받았으나 실제 적용이 실패했습니다. "
        "사유: {{error}} — 승인은 유효하며, 적용만 다시 시도하면 됩니다.",
    ),
}


def _link(request_id: Any, base: Optional[str] = None) -> str:
    """알림이 싣는 주소 — **언제나 그 건이 바로 열리는** 주소다.

    ``base`` 는 DB 템플릿의 link_url(운영팀이 관리 화면에서 고칠 수 있다)이고,
    없으면 코드 기본값이다. 어느 쪽이든 ``request=`` 는 **여기서 붙인다**.

    예전에는 DB 템플릿에 link_url 이 있으면 그것을 그대로 썼다. 시드가 한 번
    돌고 나면 언제나 있으므로, 결과적으로 **모든 알림이 건 번호 없이** 목록만
    가리켰다 — 알림을 누른 사람이 방금 읽은 그 건을 목록에서 다시 찾아야 했다.
    """
    root = (base or "").strip() or _LINK_BASE
    sep = "&" if "?" in root else "?"
    return f"{root}{sep}request={request_id}"


def _fill(text: str, replacements: Dict[str, str]) -> str:
    """``{{이름}}`` 치환 — 배포·정책 알림과 **같은 규칙**이다.

    ``{{date}}`` 는 언제나 채워진다(운영팀이 문구에 날짜를 넣을 수 있게).
    """
    out = text or ""
    base = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    base.update({k: str(v) for k, v in (replacements or {}).items() if v is not None})
    for key, value in base.items():
        out = out.replace("{{%s}}" % key, value)
    return out


def _fetch_template(app_db, template_id: str, lang_cd: str = "ko") -> Optional[tuple]:
    """DB 템플릿 (title, content, link_url) — 없으면 ``None``.

    **모델이 아니라 SQL 로 읽는다.** 이 모듈은 core 뿐 아니라 workflow·documents
    파드에서도 돈다(결재를 올리는 쪽이 거기다). 그쪽에는 ``NotificationMessageTemplate``
    모델 클래스가 없으므로, 모델을 쓰면 결재는 올라가는데 **알림만 조용히 빠진다**.
    표 이름은 세 서비스가 이미 공유하는 것이다.
    """
    tpl = _q(app_db,
             "SELECT link_url FROM notification_message_templates WHERE template_id = %s LIMIT 1",
             (template_id,))
    if not tpl:
        return None
    trans = _q(app_db,
               "SELECT title, content FROM notification_message_template_translations "
               "WHERE template_id = %s AND lang_cd = %s LIMIT 1",
               (template_id, lang_cd))
    if not trans:
        return None
    return (str(trans[0].get("title") or ""),
            str(trans[0].get("content") or ""),
            str(tpl[0].get("link_url") or ""))


def _render(app_db, template_id: str, replacements: Dict[str, str]) -> tuple:
    """(title, message, link) — DB 템플릿이 있으면 그것, 없으면 코드 기본값."""
    raw_title, raw_content = DEFAULTS[template_id]
    link: Optional[str] = None
    try:
        found = _fetch_template(app_db, template_id)
        if found:
            raw_title, raw_content, link = found
    except Exception as exc:  # noqa: BLE001 — 템플릿 조회 실패로 알림을 잃지 않는다
        logger.warning("결재 알림 템플릿 조회 실패 (%s): %s", template_id, exc)

    return _fill(raw_title, replacements), _fill(raw_content, replacements), link


def _insert(app_db, user_id: int, template_id: str, title: str, message: str, link: str) -> None:
    """알림 한 줄. **덮어쓰지 않는다** — 건마다 따로 처리해야 하는 일이다.

    (``deploy_notifier`` 는 ``(user_id, template_id)`` 로 upsert 한다. "승인 대기
    N건" 같은 요약에는 맞지만 결재에는 맞지 않는다 — 먼저 온 결재의 알림이
    사라져서 그 건은 아무도 모르는 채로 남는다.)
    """
    _q(app_db,
       """INSERT INTO user_notifications
              (user_id, template_id, title, message, link_url, is_read, notified_at)
          VALUES (%s, %s, %s, %s, %s, FALSE, %s)""",
       (int(user_id), template_id, title[:300], message, link[:500],
        datetime.now(timezone.utc)))


def _requester_name(req: Dict[str, Any]) -> str:
    return str(req.get("requester_name") or req.get("requester_username") or "미상")


def notify_turn(app_db, req: Dict[str, Any], approver_ids: Iterable[int]) -> None:
    """차례가 온 결재자들에게. 기안자 본인에게는 보내지 않는다(자기 차례일 수 없다)."""
    ids: List[int] = [int(u) for u in approver_ids or [] if u]
    if not ids or app_db is None:
        return
    try:
        title, message, link = _render(app_db, TEMPLATE_TURN, {
            "title": str(req.get("title") or ""),
            "requester": _requester_name(req),
        })
        link = _link(req.get("id"), base=link)
        for uid in ids:
            try:
                _insert(app_db, uid, TEMPLATE_TURN, title, message, link)
            except Exception as exc:  # noqa: BLE001 — 한 사람 실패가 나머지를 막지 않는다
                logger.warning("결재 차례 알림 실패 (user=%s): %s", uid, exc)
        logger.info("결재 #%s 차례 알림 %d명", req.get("id"), len(ids))
    except Exception as exc:  # noqa: BLE001 — 알림 실패가 결재를 막지 않는다
        logger.warning("결재 차례 알림 전체 실패: %s", exc)


def notify_settled(app_db, req: Dict[str, Any], *, actor_name: str = "", note: str = "") -> None:
    """종결(승인/거절)을 **기안자에게**.

    회수는 알리지 않는다 — 회수한 사람이 기안자 본인이라, 자기가 방금 한 일을
    자기에게 알리는 꼴이 된다.
    """
    if app_db is None:
        return
    requester = req.get("requester_id")
    status = str(req.get("status") or "")
    if not requester or status not in ("approved", "rejected"):
        return
    try:
        tid = TEMPLATE_APPROVED if status == "approved" else TEMPLATE_REJECTED
        title, message, link = _render(app_db, tid, {
            "title": str(req.get("title") or ""),
            "actor": actor_name or "결재자",
            "note": note or "(사유 없음)",
        })
        _insert(app_db, int(requester), tid, title, message, _link(req.get("id"), base=link))
    except Exception as exc:  # noqa: BLE001
        logger.warning("결재 종결 알림 실패: %s", exc)


def notify_apply_failed(app_db, req: Dict[str, Any], error: str) -> None:
    """승인은 됐는데 적용이 실패한 것을 기안자에게.

    이 알림이 없으면 그 상태는 **아무도 모른다** — 결재는 승인으로 끝났고,
    화면을 다시 열어 보는 사람이 없으면 적용 실패는 영영 묻힌다.
    """
    if app_db is None or not req.get("requester_id"):
        return
    try:
        title, message, link = _render(app_db, TEMPLATE_APPLY_FAIL, {
            "title": str(req.get("title") or ""),
            "error": (error or "")[:300],
        })
        _insert(app_db, int(req["requester_id"]), TEMPLATE_APPLY_FAIL,
                title, message, _link(req.get("id"), base=link))
    except Exception as exc:  # noqa: BLE001
        logger.warning("결재 적용 실패 알림 실패: %s", exc)
