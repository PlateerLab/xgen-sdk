"""에이전트 공유 판정: 사용자 · 역할 대상, 읽기 · 읽기 쓰기, 정책, 넓히기 · 좁히기 (2026-10-28).

판정이 core 와 workflow 에 따로 있어 목록과 열기가 어긋났다. 여기서 못박는 것:

  * 사용자 권한과 역할 권한이 함께 걸리면 높은 쪽이다.
  * [읽기 전용 모드]면 저장된 read_write 도 read 로 읽는다. [배포 효력 공유]면 배포 중일 때만 효력이 있다.
  * 넓히는 변경(새 대상, 권한 올림)만 결재로 가고, 지금 반영할 것은 넓히는 부분을 뺀 것이다.
"""
from xgen_sdk import agent_share as share
from xgen_sdk.approval import catalog

WRITE_OK = {"policy": "INDEPENDENT", "permission_mode": "READ_WRITE_ALLOWED", "on_undeploy": "SUSPEND"}
GATED = {"policy": "DEPLOY_GATED", "permission_mode": "READ_WRITE_ALLOWED", "on_undeploy": "SUSPEND"}


def _row(**kw):
    base = {"is_shared": True, "share_roles": ["영업"], "share_permissions": {"영업": "read"},
            "share_users": {"7": "read_write"}}
    base.update(kw)
    return base


def test_the_higher_of_user_and_role_permission_wins():
    row = _row()
    assert share.effective_permission(row, user_id=7, user_roles=["영업"], settings=WRITE_OK) == "read_write"
    assert share.effective_permission(row, user_id=8, user_roles=["영업"], settings=WRITE_OK) == "read"
    assert share.effective_permission(row, user_id=9, user_roles=["개발"], settings=WRITE_OK) is None


def test_read_only_mode_reads_read_write_as_read_and_gating_needs_a_deployment():
    row = _row()
    assert share.effective_permission(row, user_id=7, settings=share.DEFAULT_SETTINGS) == "read"
    assert share.effective_permission(row, user_id=7, deployment_stage="idle", settings=GATED) is None
    assert share.effective_permission(row, user_id=7, deployment_stage="deployed", settings=GATED) == "read_write"
    assert share.effective_permission(_row(is_shared=False), user_id=7, settings=WRITE_OK) is None


def test_old_rows_and_legacy_keys_are_read_safely():
    # share_permissions 가 사전이 아닌 옛 행은 역할마다 read, 옛 이전이 남긴 _default 는 역할이 아니다.
    old = {"is_shared": True, "share_roles": ["영업"], "share_permissions": "read"}
    assert share.effective_permission(old, user_id=1, user_roles=["영업"], settings=WRITE_OK) == "read"
    legacy = {"is_shared": True, "share_roles": ["_default"], "share_permissions": {"_default": "read_write"}}
    assert share.effective_permission(legacy, user_id=1, user_roles=["_default"], settings=WRITE_OK) is None
    assert share.parse_user_grants('{"7": "read_only", "x": "read", "8": "admin"}') == {"7": "read"}
    # 공유를 끈 행에 남은 역할은 대상이 아니다: 다시 켜는 저장이 넓히기로 보여야 결재를 탄다.
    off = {"is_shared": False, "share_roles": ["영업"], "share_permissions": {"영업": "read"}}
    assert share.grants_of(off) == ({}, {})
    assert share.is_widening(share.grants_of(off), ({"영업": "read"}, {})) is True


def test_widening_and_what_is_applied_now():
    before = ({"영업": "read_write"}, {"7": "read"})
    after = ({"영업": "read", "개발": "read"}, {"7": "read_write"})
    assert share.is_widening(before, after) is True
    # 지금은 내림(영업 read)만, 새 역할(개발)과 올림(7 read_write)은 결재를 기다린다.
    assert share.narrowed(before, after) == ({"영업": "read"}, {"7": "read"})
    assert share.is_widening(before, ({"영업": "read"}, {})) is False
    # 넓어지는 것만: 새 역할과 올린 사용자. 그대로이거나 내린 대상은 없다.
    assert share.widened(({"영업": "read_write", "개발": "read"}, {"7": "read"}),
                         ({"영업": "read", "개발": "read", "기획": "read"}, {"7": "read_write"})) == (
        {"기획": "read"}, {"7": "read_write"})


def test_columns_turn_sharing_off_when_nobody_is_left():
    assert share.columns(({}, {}))["is_shared"] is False
    cols = share.columns(({"영업": "read"}, {"12": "read", "7": "read_write"}))
    assert cols["is_shared"] is True and cols["share_roles"] == ["영업"]
    assert list(cols["share_users"]) == ["7", "12"]


def test_the_list_clause_matches_roles_or_the_user_and_can_require_a_deployment():
    clause, params = share.sql_shared_with("wm", user_id=7, user_roles=["영업"])
    assert "wm.share_roles && %s::TEXT[]" in clause and "share_users, '{}'::jsonb) ? %s" in clause
    assert params == [["영업"], "7"]
    gated, _ = share.sql_shared_with("wm", user_id=7, user_roles=[], gated=True)
    assert "share_dm.deployment_stage = 'deployed'" in gated


def test_permission_and_user_checks():
    assert share.check_permissions(({"영업": "read_write"}, {}), share.DEFAULT_SETTINGS)
    assert share.check_permissions(({"영업": "read_write"}, {}), WRITE_OK) is None
    found = [{"id": 7}]
    assert share.check_users({"7": "read"}, found, owner_id=1) is None
    assert "주인" in share.check_users({"1": "read"}, found, owner_id=1)
    assert "8" in share.check_users({"7": "read", "8": "read"}, found, owner_id=1)


def test_settings_reader_falls_back_to_the_defaults():
    values = {"SHARE_POLICY": "deploy_gated", "SHARE_PERMISSION_MODE": "nonsense"}
    s = share.settings_from(lambda name, default: values.get(name, default))
    assert s == {"policy": "DEPLOY_GATED", "permission_mode": "READ_ONLY", "on_undeploy": "SUSPEND"}


def test_agent_share_is_a_gated_action_applied_by_workflow():
    sp = catalog.spec(catalog.AGENT_SHARE)
    assert sp is not None and sp.gated and sp.owner == catalog.OWNER_WORKFLOW
    assert catalog.AGENT_SHARE in catalog.owned_by(catalog.OWNER_WORKFLOW)
