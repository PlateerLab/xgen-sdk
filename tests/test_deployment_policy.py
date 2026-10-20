from __future__ import annotations

import pytest

from xgen_sdk.auth import DeploymentPolicyDigestError, deployment_policy_digest


ROW = {
    "id": 42,
    "user_id": 7,
    "workflow_id": "workflow-1",
    "published_workflow_id": "published-3",
    "is_deployed": True,
    "is_accepted": True,
    "deployment_stage": "production",
    "deploy_key": "stable-key",
    "require_api_key": True,
    "public_policy": {"visibility": "private", "operations": ["fetch", "metadata"]},
}


def test_digest_is_stable_and_ignores_non_policy_columns():
    with_dates = {**ROW, "created_at": "2026-10-20T00:00:00Z", "updated_at": object()}

    assert deployment_policy_digest(with_dates) == deployment_policy_digest(ROW)
    assert deployment_policy_digest(ROW) == (
        "2e7274a709dfe6c424ad93a8262856e1a564d05d5d4836dc3b7b7833feb61626"
    )


def test_public_policy_json_text_and_native_value_have_the_same_digest():
    text_row = {
        **ROW,
        "public_policy": '{ "operations" : ["fetch", "metadata"], "visibility": "private" }',
    }

    assert deployment_policy_digest(text_row) == deployment_policy_digest(ROW)


@pytest.mark.parametrize("updates", [
    {"deploy_key": "rotated-key"},
    {"published_workflow_id": "published-4"},
    {"is_deployed": False, "deployment_stage": "idle"},
    {"is_accepted": False},
    {"require_api_key": False},
    {"user_id": 8},
    {"public_policy": {"visibility": "public", "operations": ["fetch"]}},
])
def test_security_changes_revoke_the_previous_binding(updates):
    assert deployment_policy_digest({**ROW, **updates}) != deployment_policy_digest(ROW)


@pytest.mark.parametrize(
    "row",
    [
        {key: value for key, value in ROW.items() if key != "deploy_key"},
        {**ROW, "public_policy": '{"visibility":"private"'},
        {**ROW, "public_policy": '{"visibility":"private","visibility":"public"}'},
        {**ROW, "public_policy": {"limit": float("nan")}},
    ],
)
def test_invalid_or_incomplete_policy_rows_are_rejected(row):
    with pytest.raises(DeploymentPolicyDigestError):
        deployment_policy_digest(row)
