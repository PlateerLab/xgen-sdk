"""에이전트 공유: **누가 이 에이전트를 어떤 권한으로 쓰는가**를 정하는 한 곳.

공유는 XGEN 안에서 원본 에이전트를 다른 사람과 함께 쓰는 것이다(외부 링크는 배포의 일이다).
대상은 두 가지다.

* **역할**: ``workflow_meta.share_roles``(이름 목록)와 ``share_permissions``(역할 → 권한).
* **사용자**: ``workflow_meta.share_users``(사용자 id → 권한, 2026-10-28).

권한은 ``read``(보고 쓰는 사람: 열어 보기 · 대화 · 실행)와 ``read_write``(함께 편집하는 사람) 둘이다.
한 사람에게 사용자 권한과 역할 권한이 함께 걸리면 **높은 쪽**이다. ``is_shared`` 는 공유 전체의
스위치이고, 대상이 하나도 없으면 공유가 아니다.

왜 SDK 인가
-----------
판정이 core 와 workflow 에 따로 있었다. 목록 SQL(``share_roles && 역할``)이 아홉 곳, 유효 권한 계산이
다섯 벌이었고 서로 달랐다(어떤 곳은 [읽기 전용 모드]를, 어떤 곳은 [배포 효력 공유]를 안 봤다).
목록에는 보이는데 열면 403 인 카드가 그래서 생겼다. 이제 판정과 SQL 조각은 여기 하나다.

이 모듈은 **순수하다.** DB 를 직접 부르지 않는다. 목록 SQL 은 조각과 매개변수를 돌려주고,
사용자 검증은 조회할 문장과 결과를 거르는 함수를 준다. 서비스마다 DB 클라이언트가 달라서다.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: 권한 값.
READ = "read"
READ_WRITE = "read_write"
PERMISSIONS = (READ, READ_WRITE)
_RANK = {READ: 1, READ_WRITE: 2}

#: [공유 정책] 설정 값(SHARE_POLICY / SHARE_PERMISSION_MODE / SHARE_ON_UNDEPLOY).
POLICY_INDEPENDENT = "INDEPENDENT"
POLICY_DEPLOY_GATED = "DEPLOY_GATED"
PERM_READ_ONLY = "READ_ONLY"
PERM_READ_WRITE_ALLOWED = "READ_WRITE_ALLOWED"
UNDEPLOY_SUSPEND = "SUSPEND"
UNDEPLOY_REVOKE = "REVOKE"
UNDEPLOY_RETAIN = "RETAIN"
STAGE_DEPLOYED = "deployed"

#: 설정을 못 읽었을 때: 지금까지의 기본 동작과 같다.
DEFAULT_SETTINGS: Mapping[str, str] = {
    "policy": POLICY_INDEPENDENT,
    "permission_mode": PERM_READ_ONLY,
    "on_undeploy": UNDEPLOY_SUSPEND,
}

#: 옛 마이그레이션이 역할 자리에 남긴 열쇠. 역할이 아니다.
_LEGACY_KEYS = frozenset({"_default"})

#: 공유 대상(역할 권한, 사용자 권한). 둘 다 ``{열쇠: 권한}``.
Grants = Tuple[Dict[str, str], Dict[str, str]]


# ── 값 다루기 ─────────────────────────────────────────────────────────


def normalize_permission(value: Any) -> Optional[str]:
    """권한 하나. 모르는 값은 None. 옛 화면의 ``read_only`` 는 ``read`` 다."""
    text = str(value or "").strip().lower()
    if text in (READ, "read_only"):
        return READ
    if text == READ_WRITE:
        return READ_WRITE
    return None


def _as_mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else {}
        except (TypeError, ValueError):
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def parse_user_grants(value: Any) -> Dict[str, str]:
    """``share_users`` → ``{"사용자 id": 권한}``. 숫자가 아닌 열쇠와 모르는 권한은 버린다."""
    out: Dict[str, str] = {}
    for key, perm in _as_mapping(value).items():
        text = str(key).strip()
        if not text.isdigit():
            continue
        permission = normalize_permission(perm)
        if permission:
            out[str(int(text))] = permission
    return out


def parse_role_grants(share_permissions: Any, share_roles: Any = None) -> Dict[str, str]:
    """역할 공유 → ``{"역할 이름": 권한}``.

    ``share_permissions`` 가 사전이면 그것이 정본이다(``share_roles`` 는 그 열쇠로 다시 만들어진다).
    사전이 아니면(옛 행) ``share_roles`` 의 역할마다 ``read`` 다. 옛 이전이 남긴 ``_default`` 는 역할이 아니다.
    """
    perms = _as_mapping(share_permissions)
    out: Dict[str, str] = {}
    if perms:
        for role, perm in perms.items():
            name = str(role or "").strip()
            permission = normalize_permission(perm)
            if name and name not in _LEGACY_KEYS and permission:
                out[name] = permission
        return out
    for role in share_roles or []:
        name = str(role or "").strip()
        if name and name not in _LEGACY_KEYS:
            out[name] = READ
    return out


def grants_of(row: Mapping[str, Any]) -> Grants:
    """워크플로우 행의 공유 대상(역할, 사용자).

    공유가 꺼진 행(``is_shared`` 가 거짓)은 대상이 없다. 옛 화면은 공유를 끄면서 역할을 남기기도 했는데,
    그것을 대상으로 읽으면 다음 저장 때 그 역할이 결재 없이 되살아난다(넓히기로 보이지 않으므로).
    """
    if "is_shared" in row and not row.get("is_shared"):
        return {}, {}
    return (
        parse_role_grants(row.get("share_permissions"), row.get("share_roles")),
        parse_user_grants(row.get("share_users")),
    )


def has_targets(grants: Grants) -> bool:
    roles, users = grants
    return bool(roles) or bool(users)


def higher(*permissions: Optional[str]) -> Optional[str]:
    """권한 여럿 중 높은 것. 모두 None 이면 None."""
    best: Optional[str] = None
    for permission in permissions:
        if permission and (best is None or _RANK.get(permission, 0) > _RANK.get(best, 0)):
            best = permission
    return best


# ── 정책 ─────────────────────────────────────────────────────────────


def settings_from(read: Any) -> Dict[str, str]:
    """설정 셋을 읽는다. ``read(name, default) -> str``. 모르는 값은 기본값."""
    def _get(name: str, default: str, allowed: Sequence[str]) -> str:
        try:
            value = str(read(name, default) or "").strip().upper()
        except Exception:  # noqa: BLE001 - 못 읽으면 기본값
            value = ""
        return value if value in allowed else default

    return {
        "policy": _get("SHARE_POLICY", POLICY_INDEPENDENT, (POLICY_INDEPENDENT, POLICY_DEPLOY_GATED)),
        "permission_mode": _get(
            "SHARE_PERMISSION_MODE", PERM_READ_ONLY, (PERM_READ_ONLY, PERM_READ_WRITE_ALLOWED)),
        "on_undeploy": _get(
            "SHARE_ON_UNDEPLOY", UNDEPLOY_SUSPEND, (UNDEPLOY_SUSPEND, UNDEPLOY_REVOKE, UNDEPLOY_RETAIN)),
    }


def share_effect_gated(settings: Optional[Mapping[str, str]]) -> bool:
    """공유 효력에 배포 게이트가 걸리는 조합인가: DEPLOY_GATED 이고 RETAIN 이 아닐 때."""
    s = settings or DEFAULT_SETTINGS
    return s.get("policy") == POLICY_DEPLOY_GATED and s.get("on_undeploy") != UNDEPLOY_RETAIN


def is_share_effective(is_shared: Any, deployment_stage: Optional[str],
                       settings: Optional[Mapping[str, str]]) -> bool:
    """공유가 지금 효력이 있는가. 게이트가 걸리면 배포(deployed) 중일 때만."""
    if not bool(is_shared):
        return False
    if not share_effect_gated(settings):
        return True
    return str(deployment_stage or "").strip() == STAGE_DEPLOYED


def write_allowed(settings: Optional[Mapping[str, str]]) -> bool:
    """[읽기 · 쓰기] 공유를 쓸 수 있는 설정인가."""
    return (settings or DEFAULT_SETTINGS).get("permission_mode") == PERM_READ_WRITE_ALLOWED


# ── 판정 ─────────────────────────────────────────────────────────────


def effective_permission(
    row: Mapping[str, Any],
    *,
    user_id: Any,
    user_roles: Iterable[str] = (),
    deployment_stage: Optional[str] = None,
    settings: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """이 사람의 **유효 공유 권한**: ``read`` · ``read_write`` · None(공유 대상이 아니거나 효력 없음).

    소유자 여부는 보지 않는다(호출자가 먼저 가른다). 사용자 권한과 맞는 역할 권한 중 높은 것이고,
    [읽기 전용 모드]면 ``read_write`` 도 ``read`` 로 읽는다(저장값을 고치지 않고 내린다).
    """
    if not is_share_effective(row.get("is_shared"), deployment_stage, settings):
        return None
    roles, users = grants_of(row)
    best = users.get(str(user_id).strip()) if user_id not in (None, "") else None
    for role in set(str(r) for r in (user_roles or ())):
        best = higher(best, roles.get(role))
    if best == READ_WRITE and not write_allowed(settings):
        return READ
    return best


def sql_shared_with(
    alias: str = "wm",
    *,
    user_id: Any,
    user_roles: Iterable[str] = (),
    gated: bool = False,
) -> Tuple[str, List[Any]]:
    """"나에게 공유된 워크플로우" 를 거르는 SQL 조각과 매개변수(PostgreSQL).

    역할이 겹치거나 사용자 id 가 ``share_users`` 에 있으면 참이다. ``gated`` (배포 효력 공유)면
    소유자의 배포가 지금 deployed 일 때만: 목록과 열기가 같은 판정을 해야 "보이는데 열면 403" 이
    생기지 않는다.
    """
    a = alias
    clause = (
        f"({a}.is_shared = TRUE AND ({a}.share_roles && %s::TEXT[]"
        f" OR COALESCE({a}.share_users, '{{}}'::jsonb) ? %s))"
    )
    params: List[Any] = [list(str(r) for r in (user_roles or ())), str(user_id)]
    if gated:
        clause = (
            f"({clause} AND EXISTS (SELECT 1 FROM deploy_meta share_dm"
            f" WHERE share_dm.workflow_id = {a}.workflow_id AND share_dm.user_id = {a}.user_id"
            f" AND share_dm.deployment_stage = '{STAGE_DEPLOYED}'))"
        )
    return clause, params


# ── 바꾸기 ───────────────────────────────────────────────────────────


def widened(before: Grants, after: Grants) -> Grants:
    """넓어지는 대상만: 새로 생기거나 권한이 올라가는 대상과 그 새 권한.

    새 대상과 같은 검사(내 범위의 역할인가, 로그인할 수 있는 사람인가)는 이것에 건다. 이미 있는 대상의
    권한을 올리는 것도 새로 주는 것과 같다.
    """
    out: List[Dict[str, str]] = []
    for old, new in zip(before, after):
        out.append({key: permission for key, permission in new.items()
                    if _RANK.get(permission, 0) > _RANK.get(old.get(key), 0)})
    return out[0], out[1]


def is_widening(before: Grants, after: Grants) -> bool:
    """바꾼 뒤가 **더 넓은가**: 새 대상이 생기거나 권한이 올라가면 참.

    결재([에이전트 공유])는 넓히는 변경에만 걸린다. 빼기와 권한 내림은 곧바로 반영한다.
    """
    return has_targets(widened(before, after))


def narrowed(before: Grants, after: Grants) -> Grants:
    """넓히는 부분을 뺀 **지금 반영할** 대상: 둘 다에 있는 대상의 낮은 권한.

    넓히는 변경을 결재에 올릴 때, 빼기와 내림은 기다리지 않고 이것으로 바로 쓴다.
    """
    out: List[Dict[str, str]] = []
    for old, new in zip(before, after):
        kept: Dict[str, str] = {}
        for key, permission in new.items():
            if key in old:
                kept[key] = permission if _RANK[permission] <= _RANK.get(old[key], 0) else old[key]
        out.append(kept)
    return out[0], out[1]


def columns(grants: Grants) -> Dict[str, Any]:
    """``workflow_meta`` 에 쓸 칸들. 대상이 없으면 공유가 꺼진다."""
    roles, users = grants
    return {
        "is_shared": has_targets(grants),
        "share_roles": sorted(roles),
        "share_permissions": dict(sorted(roles.items())),
        "share_users": dict(sorted(users.items(), key=lambda kv: int(kv[0]))),
    }


def check_permissions(grants: Grants, settings: Optional[Mapping[str, str]]) -> Optional[str]:
    """쓰기 전에 권한 값을 본다. 문제가 있으면 사람이 읽을 사유, 없으면 None.

    [읽기 전용 모드]에서 ``read_write`` 가 오면 거절한다(조용히 낮추면 화면과 어긋난다).
    """
    if write_allowed(settings):
        return None
    roles, users = grants
    if READ_WRITE in roles.values() or READ_WRITE in users.values():
        return "지금 공유 설정은 읽기만 허용합니다. 읽기 · 쓰기 공유는 관리자가 공유 권한 범위를 넓혀야 쓸 수 있습니다."
    return None


#: 공유받을 수 있는 사람: **로그인할 수 있는 사람만**(결재자 목록과 같은 기준).
ACTIVE_USERS_SQL = (
    "SELECT u.id, u.username, u.full_name, u.department FROM users u "
    "WHERE u.id = ANY(%s) AND u.is_active = TRUE AND u.status = '1'"
)


def check_users(users: Mapping[str, str], found: Iterable[Mapping[str, Any]], *,
                owner_id: Any) -> Optional[str]:
    """사용자 대상 검증. ``found`` 는 :data:`ACTIVE_USERS_SQL` 결과. 문제면 사유, 없으면 None."""
    if owner_id is not None and str(owner_id) in users:
        return "에이전트 주인에게는 공유하지 않아도 됩니다."
    active = {str(r.get("id")) for r in found or ()}
    missing = sorted((k for k in users if k not in active), key=int)
    if missing:
        return f"공유할 수 없는 사용자입니다(없거나 비활성): {', '.join(missing)}"
    return None


__all__ = [
    "READ", "READ_WRITE", "PERMISSIONS", "Grants",
    "POLICY_INDEPENDENT", "POLICY_DEPLOY_GATED", "PERM_READ_ONLY", "PERM_READ_WRITE_ALLOWED",
    "UNDEPLOY_SUSPEND", "UNDEPLOY_REVOKE", "UNDEPLOY_RETAIN", "STAGE_DEPLOYED", "DEFAULT_SETTINGS",
    "normalize_permission", "parse_user_grants", "parse_role_grants", "grants_of", "has_targets", "higher",
    "settings_from", "share_effect_gated", "is_share_effective", "write_allowed",
    "effective_permission", "sql_shared_with",
    "widened", "is_widening", "narrowed", "columns", "check_permissions", "ACTIVE_USERS_SQL", "check_users",
]
