"""행위 목록은 **한 곳에 선언된다** — 세 레포가 같은 것을 본다.

왜 이 검사가 필요한가
---------------------
행위를 실제로 여는 코드는 흩어져 있는데(배포는 workflow, 지식은 documents,
클라우드·DB·도구는 workflow), 관리자가 "무엇을 결재 필수로 할까" 를 고르는
화면은 core 에 있다. 목록이 어긋나면 **화면에 없는 행위는 영영 켤 수 없고**,
그 사실은 아무 오류도 내지 않는다.
"""
from __future__ import annotations

import pytest

from xgen_sdk.approval import catalog, registry


def test_action_types_are_unique():
    keys = [s.action_type for s in catalog.CATALOG]
    assert len(keys) == len(set(keys)), "같은 action_type 이 두 번 선언됐다"


def test_gated_actions_cannot_be_raised_by_hand():
    """게이트 행위를 사람이 직접 올릴 수 있으면 **게이트가 아니다.**

    payload 를 손으로 적어 올릴 수 있으면 "남의 워크플로우 id" 를 넣어 결재를
    올리고, 승인되는 순간 그 워크플로우가 배포된다.
    """
    for sp in catalog.gated_specs():
        assert not sp.user_submittable, f"{sp.action_type} 을 사람이 직접 올릴 수 있다"


def test_free_actions_are_not_in_the_policy_screen():
    """자유 결재(일반·테스트)는 관리자가 켜고 끌 대상이 아니다."""
    for key in (catalog.GENERIC, catalog.TEST):
        sp = catalog.spec(key)
        assert sp is not None and not sp.gated and sp.user_submittable


def test_every_catalogued_action_is_registered():
    """등록되지 않은 종류로는 결재가 올라가지 않는다 — 목록에 있는데 못 올리면
    화면은 선택지를 보여 주고 상신은 실패한다."""
    for sp in catalog.CATALOG:
        assert registry.is_registered(sp.action_type), f"{sp.action_type} 미등록"


def test_the_gated_set_is_what_xgen_actually_gates_today():
    """지금 XGEN 이 실제로 승인을 받는 행위 전부 — 전수 조사(2026-09-11)의 결론.

    배포 1 + RAG 통제 13. 여기서 무엇이 빠지면 그 행위는 결재로 옮겨지지 못한
    채 옛 게이트에 남거나, 아무 통제 없이 열린다.
    """
    assert {s.action_type for s in catalog.gated_specs()} == {
        "agent.deploy",
        "collection.create", "collection.upload", "collection.update",
        "filestore.embed",
        "cloud.storage_create", "cloud.upload", "cloud.update", "cloud.share",
        "cloud.device_link", "cloud.agent_link",
        "db.create", "db.share",
        "tool.publish",
    }


def test_apply_owners_are_real_services():
    for sp in catalog.CATALOG:
        assert sp.owner in (catalog.OWNER_CORE, catalog.OWNER_WORKFLOW)


def test_owned_by_splits_the_catalog():
    """워커는 자기 것만 가져가야 한다 — 남의 것을 가져가면 훅이 없어서
    '적용됨' 으로 찍고 아무 일도 일어나지 않는다."""
    core = set(catalog.owned_by(catalog.OWNER_CORE))
    wf = set(catalog.owned_by(catalog.OWNER_WORKFLOW))
    assert core and wf and not (core & wf)
    assert core | wf == {s.action_type for s in catalog.CATALOG}


def test_labels_are_human_readable():
    for sp in catalog.CATALOG:
        assert sp.label and sp.label != sp.action_type, f"{sp.action_type} 에 이름표가 없다"
        assert sp.description, f"{sp.action_type} 에 설명이 없다"


def test_registry_hands_out_the_catalog_labels():
    known = registry.known_actions()
    for sp in catalog.CATALOG:
        assert known[sp.action_type] == sp.label


def test_only_free_actions_are_offered_for_hand_submission():
    assert set(registry.submittable_actions()) == {catalog.GENERIC, catalog.TEST}


def test_an_ad_hoc_registration_keeps_working():
    """카탈로그에 없는 종류도 등록할 수 있다 — 테스트·임시 배선이 쓴다.

    이때는 소유를 알 수 없으므로 **등록한 그 자리(core)** 에서 적용된다.
    """
    registry.register_action("t.catalog_adhoc", lambda p, q: None, label="즉석")
    assert registry.is_registered("t.catalog_adhoc")
    assert registry.owner_of("t.catalog_adhoc") == "core"
    assert registry.known_actions()["t.catalog_adhoc"] == "즉석"


def test_registering_an_apply_does_not_erase_the_label():
    """카탈로그가 먼저 이름표를 달고, 소유 서비스가 나중에 훅만 꽂는다."""
    registry.register_action("agent.deploy", lambda p, q: None)
    assert registry.known_actions()["agent.deploy"] == catalog.spec("agent.deploy").label
    assert registry.has_apply("agent.deploy")
