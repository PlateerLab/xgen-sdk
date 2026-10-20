"""P1 fail-closed contracts at policy and permission authority boundaries."""
from __future__ import annotations

import traceback

import pytest

from xgen_sdk import control_policy as cp
from xgen_sdk.auth import PermissionResolutionError, resolve_user_permissions


RULE = {
    "id": 1,
    "policy_name": "phone",
    "regex_pattern": r"010-\d{4}-\d{4}",
    "masking": True,
    "is_active": True,
    "version": 7,
}


class PolicyDb:
    def __init__(self, tables=None, *, fail_table=None, none_table=None):
        self.tables = tables or {}
        self.fail_table = fail_table
        self.none_table = none_table

    def find_records_by_condition(self, table, conditions, **_kwargs):
        if table == self.fail_table:
            raise RuntimeError("private policy store detail")
        if table == self.none_table:
            return None
        return [
            row for row in self.tables.get(table, [])
            if all(row.get(key) == value for key, value in conditions.items())
        ]


def _flags(values):
    return lambda name, default: values[name]


def test_strict_policy_load_distinguishes_disabled_and_loaded_empty():
    disabled = cp.load_scope_rules_strict(
        PolicyDb(), cp.SCOPE_MODEL_INPUT,
        flag=_flags({"IS_AVAILABLE_PIIS": False, "IS_AVAILABLE_FORBIDDEN_WORDS": False}),
    )
    assert disabled.state is cp.PolicyLoadState.DISABLED

    loaded = cp.load_scope_rules_strict(
        PolicyDb({"piis": []}), cp.SCOPE_MODEL_INPUT,
        flag=_flags({
            "IS_AVAILABLE_PIIS": True,
            "PII_BLOCK_MODEL_INPUT": True,
            "IS_AVAILABLE_FORBIDDEN_WORDS": False,
        }),
    )
    assert loaded.state is cp.PolicyLoadState.LOADED
    assert loaded.rules == ()
    assert loaded.scope == cp.SCOPE_MODEL_INPUT


def test_strict_policy_load_preserves_revision_and_rejects_invalid_regex():
    loaded = cp.load_rules_strict(PolicyDb({"piis": [RULE]}), cp.KINDS[0], scope="model")
    assert loaded.state is cp.PolicyLoadState.LOADED
    assert loaded.revision == "1:7"
    assert len(loaded.rules) == 1

    invalid = cp.load_rules_strict(
        PolicyDb({"piis": [{**RULE, "regex_pattern": "("}]}), cp.KINDS[0], request_id="req-7",
    )
    assert invalid.state is cp.PolicyLoadState.INVALID
    assert invalid.rules == ()
    assert invalid.error_code == "policy_data_invalid"
    assert invalid.request_id == "req-7"

    invalid_replacement = cp.load_rules_strict(
        PolicyDb({"piis": [{**RULE, "masking_pattern": r"\2"}]}), cp.KINDS[0],
    )
    assert invalid_replacement.state is cp.PolicyLoadState.INVALID


@pytest.mark.parametrize(
    "db, workflow_id, code",
    [
        (None, None, "policy_store_unavailable"),
        (PolicyDb(fail_table="piis"), None, "policy_store_unavailable"),
        (PolicyDb(none_table="piis"), None, "policy_store_unavailable"),
        (
            PolicyDb({"piis": [RULE]}, fail_table=cp.EXCEPTIONS_TABLE),
            "workflow-1",
            "policy_exceptions_unavailable",
        ),
    ],
)
def test_policy_store_failures_never_become_loaded_empty(db, workflow_id, code):
    result = cp.load_rules_strict(db, cp.KINDS[0], workflow_id=workflow_id)
    assert result.state is cp.PolicyLoadState.UNAVAILABLE
    assert result.rules == ()
    assert result.error_code == code
    assert result.retryable is True


def test_an_excluded_malformed_policy_is_still_invalid_authority_data():
    db = PolicyDb({
        "piis": [{**RULE, "regex_pattern": "("}],
        cp.EXCEPTIONS_TABLE: [{"policy_id": 1, "is_applied": False}],
    })
    result = cp.load_rules_strict(db, cp.KINDS[0], workflow_id="workflow-1")
    assert result.state is cp.PolicyLoadState.INVALID
    assert result.rules == ()


def test_strict_config_requires_an_explicit_valid_value(monkeypatch):
    class Config:
        def __init__(self, value):
            self.value = value

        def get_config_by_name(self, _name):
            return type("Stored", (), {"value": self.value})()

    monkeypatch.delenv("IS_AVAILABLE_PIIS", raising=False)
    with pytest.raises(cp.PolicyUnavailableError):
        cp.config_flag_strict("IS_AVAILABLE_PIIS", config=Config(None))
    with pytest.raises(cp.PolicyInvalidError):
        cp.config_flag_strict("IS_AVAILABLE_PIIS", config=Config("sometimes"))
    assert cp.config_flag_strict("IS_AVAILABLE_PIIS", config=Config(False)) is False


def test_policy_outcome_exposes_only_safe_failure_metadata():
    outcome = cp.PolicyOutcome(
        cp.PolicyDecision.ERROR,
        error_code="policy_store_unavailable",
        request_id="req-9",
        retryable=True,
    )
    assert outcome.request_id == "req-9" and outcome.retryable is True
    with pytest.raises(ValueError):
        cp.PolicyOutcome(cp.PolicyDecision.ERROR, error_code="postgres password=canary")


class PermissionDb:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def execute_raw_query(self, _query, _params):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def _ok(*rows):
    return {"success": True, "data": [{"perm": permission, "source": source} for permission, source in rows]}


def test_direct_deny_failure_cannot_widen_resolved_permissions():
    db = PermissionDb([{"success": False, "data": []}])
    with pytest.raises(PermissionResolutionError) as caught:
        resolve_user_permissions(db, 7, False, request_id="login-7")
    assert caught.value.code == "permission_store_unavailable"
    assert caught.value.request_id == "login-7"


def test_permission_exception_is_redacted_and_legitimate_empty_is_success(caplog):
    secret = "password=" + "canary"
    with pytest.raises(PermissionResolutionError) as caught:
        resolve_user_permissions(
            PermissionDb([RuntimeError(secret)]), 7, False, request_id="login-8",
        )
    assert "canary" not in str(caught.value)
    rendered = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert "canary" not in rendered
    assert "canary" not in caplog.text

    assert resolve_user_permissions(PermissionDb([_ok()]), 7, False) == set()


def test_successful_direct_deny_still_subtracts_a_grant():
    db = PermissionDb([_ok(
        ("workflow:read", "role"),
        ("workflow:create", "role"),
        ("document:read", "grant"),
        ("workflow:create", "deny"),
    )])
    resolved = resolve_user_permissions(db, 7, False)
    assert resolved == {"workflow:read", "document:read"}
    assert db.calls == 1, "authority must come from one database snapshot"
