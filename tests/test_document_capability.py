from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping

import pytest

from xgen_sdk.auth import (
    DocumentCapabilityConfigurationError,
    DocumentCapabilityValidationError,
    sign_document_capability,
    verify_document_capability,
)


NOW = 2_000_000_000
CURRENT_KEY = "current-document-capability-key-at-least-32-bytes"
PREVIOUS_KEY = "previous-document-capability-key-at-least-32-bytes"
CLAIMS = {
    "deployment_id": 42,
    "workflow_id": "workflow-7",
    "policy_digest": "a" * 64,
    "resource_id": "resource-9",
    "resource_digest": "b" * 64,
    "operations": ["metadata", "download"],
}


@pytest.fixture(autouse=True)
def _capability_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XGEN_SERVICE_KEY_ID", "current")
    monkeypatch.setenv("XGEN_SERVICE_SIGNING_KEY", CURRENT_KEY)
    monkeypatch.delenv("XGEN_DOCUMENT_CAPABILITY_KEYS", raising=False)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(segment: str) -> dict[str, object]:
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def _encode(value: Mapping[str, object]) -> str:
    return _b64(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())


def _resign(token: str, *, key: str = CURRENT_KEY, header=None, payload=None) -> str:
    encoded_header, encoded_payload, _ = token.split(".")
    if header is not None:
        encoded_header = _encode(header)
    if payload is not None:
        encoded_payload = _encode(payload)
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = hmac.new(key.encode(), signing_input, hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_payload}.{_b64(signature)}"


def test_round_trip_normalizes_deployment_id_and_binds_exact_claims():
    token = sign_document_capability(CLAIMS, now=NOW)

    payload = verify_document_capability(token, now=NOW)

    assert payload == {
        "iss": "xgen-documents",
        "aud": "xgen-documents",
        "type": "document-capability",
        "iat": NOW,
        "exp": NOW + 60,
        "jti": payload["jti"],
        **{**CLAIMS, "deployment_id": "42"},
    }
    assert isinstance(payload["jti"], str) and payload["jti"]


@pytest.mark.parametrize(
    "claims",
    [
        {**CLAIMS, "unexpected": "field"},
        {key: value for key, value in CLAIMS.items() if key != "workflow_id"},
        {**CLAIMS, "deployment_id": True},
        {**CLAIMS, "policy_digest": "A" * 64},
        {**CLAIMS, "resource_digest": "short"},
        {**CLAIMS, "operations": []},
        {**CLAIMS, "operations": ["fetch", "fetch"]},
        {**CLAIMS, "operations": ["fetch", "delete"]},
    ],
)
def test_signer_rejects_invalid_or_non_finite_claim_schema(claims):
    with pytest.raises(DocumentCapabilityValidationError):
        sign_document_capability(claims, now=NOW)


def test_verifier_rejects_algorithm_confusion_wrong_audience_and_expiry():
    token = sign_document_capability(CLAIMS, now=NOW)
    header_segment, payload_segment, _ = token.split(".")
    header = _decode(header_segment)
    payload = _decode(payload_segment)

    wrong_algorithm = _resign(token, header={**header, "alg": "RS256"})
    wrong_audience = _resign(token, payload={**payload, "aud": "xgen-workflow"})

    with pytest.raises(DocumentCapabilityValidationError):
        verify_document_capability(wrong_algorithm, now=NOW)
    with pytest.raises(DocumentCapabilityValidationError):
        verify_document_capability(wrong_audience, now=NOW)
    with pytest.raises(DocumentCapabilityValidationError):
        verify_document_capability(token, now=NOW + 60)


def test_verifier_rejects_duplicate_json_members_even_with_valid_signature():
    token = sign_document_capability(CLAIMS, now=NOW)
    header_segment, payload_segment, _ = token.split(".")
    payload_text = base64.urlsafe_b64decode(
        payload_segment + "=" * (-len(payload_segment) % 4)
    ).decode()
    duplicate_payload = "{\"aud\":\"xgen-documents\"," + payload_text[1:]
    encoded_payload = _b64(duplicate_payload.encode())
    signing_input = f"{header_segment}.{encoded_payload}".encode("ascii")
    signature = hmac.new(CURRENT_KEY.encode(), signing_input, hashlib.sha256).digest()
    duplicate_token = f"{header_segment}.{encoded_payload}.{_b64(signature)}"

    with pytest.raises(DocumentCapabilityValidationError):
        verify_document_capability(duplicate_token, now=NOW)


def test_keyring_accepts_current_and_bounded_previous_then_revokes_by_removal():
    keyring = {
        "current": {"key": CURRENT_KEY},
        "previous": {"key": PREVIOUS_KEY, "not_after": NOW + 60},
    }
    current = sign_document_capability(CLAIMS, now=NOW)
    previous = sign_document_capability(
        CLAIMS,
        signing_key=PREVIOUS_KEY,
        key_id="previous",
        ttl_seconds=30,
        now=NOW,
    )

    assert verify_document_capability(current, trusted_keys=keyring, now=NOW)["jti"]
    assert verify_document_capability(previous, trusted_keys=keyring, now=NOW)["jti"]
    with pytest.raises(DocumentCapabilityValidationError, match="unknown"):
        verify_document_capability(
            previous,
            trusted_keys={"current": {"key": CURRENT_KEY}},
            now=NOW,
        )


def test_keyring_rejects_unknown_kid_and_token_extending_past_retirement():
    keyring = {
        "current": {"key": CURRENT_KEY},
        "previous": {"key": PREVIOUS_KEY, "not_after": NOW + 30},
    }
    unknown = sign_document_capability(CLAIMS, key_id="unknown", now=NOW)
    overlaps_cutoff = sign_document_capability(
        CLAIMS,
        signing_key=PREVIOUS_KEY,
        key_id="previous",
        ttl_seconds=60,
        now=NOW,
    )

    with pytest.raises(DocumentCapabilityValidationError, match="unknown"):
        verify_document_capability(unknown, trusted_keys=keyring, now=NOW)
    with pytest.raises(DocumentCapabilityValidationError, match="retirement"):
        verify_document_capability(overlaps_cutoff, trusted_keys=keyring, now=NOW)


def test_configured_environment_keyring_forbids_current_key_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    token = sign_document_capability(CLAIMS, now=NOW)
    monkeypatch.setenv(
        "XGEN_DOCUMENT_CAPABILITY_KEYS",
        json.dumps({"replacement": {"key": PREVIOUS_KEY}}),
    )

    with pytest.raises(DocumentCapabilityValidationError, match="unknown"):
        verify_document_capability(token, now=NOW)


@pytest.mark.parametrize(
    "keyring",
    [
        {},
        {"one": {"key": CURRENT_KEY}, "two": {"key": PREVIOUS_KEY}},
        {"one": {"key": CURRENT_KEY, "not_after": NOW + 60}},
        {"one": {"key": CURRENT_KEY, "extra": "field"}},
    ],
)
def test_keyring_schema_is_strict(keyring):
    token = sign_document_capability(CLAIMS, now=NOW)
    with pytest.raises(DocumentCapabilityConfigurationError):
        verify_document_capability(token, trusted_keys=keyring, now=NOW)
