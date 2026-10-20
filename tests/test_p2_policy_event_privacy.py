"""P2 retained-evidence producer contract."""
from __future__ import annotations

import base64
import json
import logging

import httpx
import pytest

from xgen_sdk import control_policy as cp

REF = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
KEY = "sdk-evidence-test-signing-key-000000000000"


@pytest.fixture(autouse=True)
def service_identity(monkeypatch):
    monkeypatch.setenv("XGEN_SERVICE_NAME", "xgen-workflow")
    monkeypatch.setenv("XGEN_SERVICE_SIGNING_KEY", KEY)
    monkeypatch.delenv("XGEN_SERVICE_KEY_ID", raising=False)


def _response(request: httpx.Request, *, event_id: int = 41) -> httpx.Response:
    body = json.loads(request.content)
    safe = [{
        "policy_id": item["policy_id"], "policy_name": item["policy_name"],
        "count": item["count"], "enforced": item["enforced"], "samples": [],
        "evidence_ref": REF,
    } for item in body["detections"]]
    return httpx.Response(200, json={"evidence_ref": REF, "event_id": event_id, "detections": safe})


def _log(sink, **overrides):
    values = {
        "source": cp.SOURCE_CHAT_INPUT,
        "policy_type": cp.PII,
        "policy_ids": [1],
        "policy_names": ["secret"],
        "total_detected": 1,
        "detections": cp.build_detections([{
            "policy_id": 1, "policy_name": "secret", "count": 1,
            "samples": [{"original": "canary-" + "x" * 240, "masked": "***"}],
        }]),
        "workflow_id": "wf-1",
        "user_id": 7,
        "username": "worker",
        "subject_type": "workflow",
        "subject_id": 19,
        "evidence_sink": sink,
    }
    values.update(overrides)
    return cp.log_policy_event(object(), **values)


def test_http_wire_retains_exact_original_and_uses_finite_signed_identity():
    seen = []

    def handler(request):
        seen.append(request)
        return _response(request)

    result = _log(cp.HttpEvidenceSink("https://core.internal/base", transport=httpx.MockTransport(handler)))
    assert result.evidence_ref == REF and result.event_id == 41
    request = seen[0]
    body = json.loads(request.content)
    assert request.url == "https://core.internal/base/api/internal/governance/evidence"
    assert body["purpose"] == "policy-audit"
    assert body["subject_type"] == "workflow" and body["subject_id"] == 19
    assert body["detections"][0]["samples"][0]["original"] == "canary-" + "x" * 240
    assert set(body["event"]) == {
        "source", "policy_type", "policy_ids", "policy_names", "total_detected",
        "workflow_id", "execution_id", "interaction_id", "node_id", "collection_name",
        "collection_display_name", "document_name", "user_id", "username", "session_id",
    }
    assert "metadata" not in body["event"]
    proof = request.headers["x-xgen-principal"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(proof + "=" * (-len(proof) % 4)))
    assert claims["iss"] == "xgen-workflow" and claims["aud"] == "xgen-core"
    assert claims["headers"]["x-user-id"] == "7"
    assert claims["headers"]["x-user-permissions"] == cp.INGEST_PERMISSION
    assert claims["headers"]["x-user-superuser"] == "false"


def test_sample_and_detection_repr_hide_original_and_masked():
    from xgen_sdk.control_policy.evidence import EvidenceSample, EvidenceDetection
    sample = EvidenceSample("private-original-canary", "partially-private-mask")
    detection = EvidenceDetection(1, "PII", 1, (sample,))
    assert "private-original-canary" not in repr(sample) + repr(detection)
    assert "partially-private-mask" not in repr(sample) + repr(detection)


def test_unicode_actor_name_is_encoded_in_signed_http_header():
    seen = []
    def handler(request):
        seen.append(request)
        return _response(request)
    _log(cp.HttpEvidenceSink("https://core.internal", transport=httpx.MockTransport(handler)), username="검증 사용자")
    assert seen[0].headers["x-user-name"].startswith("%")


@pytest.mark.parametrize("user_id,expected", [(7, "actor-7"), (None, "anonymous")])
def test_missing_actor_display_name_preserves_signed_identity(user_id, expected):
    seen = []
    def handler(request):
        seen.append(request)
        return _response(request)
    _log(cp.HttpEvidenceSink("https://core.internal", transport=httpx.MockTransport(handler)),
         user_id=user_id, username=None)
    assert seen[0].headers["x-user-name"] == expected
    assert seen[0].headers["x-user-id"] == str(user_id or 0)
    assert json.loads(seen[0].content)["event"]["username"] is None


def test_unsafe_server_response_is_rejected_instead_of_echoed():
    def handler(request):
        body = _response(request).json()
        body["detections"][0]["original"] = "server-private-canary"
        return httpx.Response(200, json=body)
    with pytest.raises(cp.EvidenceWriteError, match="EVIDENCE_SDK_RESPONSE_INVALID") as err:
        _log(cp.HttpEvidenceSink("https://core.internal", transport=httpx.MockTransport(handler)))
    assert "server-private-canary" not in str(err.value)


def test_legacy_string_projection_and_exception_are_evidence_safe(caplog):
    raw = "private-canary-" + "z" * 150
    detections = cp.build_detections([{"samples": [{"original": raw, "masked": "***"}]}])
    assert json.loads(detections)[0]["samples"] == []
    assert raw not in str(detections) and raw not in repr(detections)

    caplog.set_level(logging.DEBUG)
    request = httpx.Request("POST", "https://core.internal")

    def timeout(_request):
        raise httpx.ReadTimeout(raw, request=request)

    with pytest.raises(cp.EvidenceWriteError) as exc:
        _log(cp.HttpEvidenceSink("https://core.internal", transport=httpx.MockTransport(timeout)),
             detections=detections)
    assert exc.value.code == "EVIDENCE_SDK_TIMEOUT" and exc.value.retryable is True
    assert raw not in str(exc.value) and raw not in caplog.text


@pytest.mark.parametrize("bad", [
    '[{"samples":[{"original":"raw"}]}]',
    [{"samples": [{"original": {"nested": "raw"}, "masked": "***"}]}],
    [{"samples": [{"original": "raw", "masked": "***", "extra": "raw"}]}],
])
def test_raw_string_and_nested_evidence_bypasses_are_rejected(bad):
    if isinstance(bad, str):
        with pytest.raises(cp.EvidenceWriteError, match="EVIDENCE_REQUEST_INVALID"):
            _log(object(), detections=bad)
    else:
        with pytest.raises(cp.EvidenceWriteError, match="EVIDENCE_REQUEST_INVALID"):
            cp.build_detections(bad)


def test_metadata_body_override_is_rejected():
    with pytest.raises(cp.EvidenceWriteError, match="EVIDENCE_REQUEST_INVALID"):
        _log(object(), metadata={"credentials": "attacker"})


@pytest.mark.parametrize("bad", [{}, "", False, 0, None])
def test_falsy_samples_are_rejected_without_silently_losing_evidence(bad):
    with pytest.raises(cp.EvidenceWriteError, match="EVIDENCE_REQUEST_INVALID"):
        cp.build_detections([{"samples": bad}])


@pytest.mark.parametrize("field,bad", [("policy_ids", False), ("policy_ids", ""),
                                        ("policy_names", {}), ("idempotency_key", ""),
                                        ("idempotency_key", 1)])
def test_falsy_context_types_are_not_reinterpreted(field, bad):
    with pytest.raises(cp.EvidenceWriteError, match="EVIDENCE_REQUEST_INVALID"):
        _log(object(), **{field: bad})


def test_detection_iterator_is_bounded_before_consuming_extra_entries():
    consumed = 0
    def entries():
        nonlocal consumed
        for _ in range(102):
            consumed += 1
            yield {"samples": []}
    with pytest.raises(cp.EvidenceWriteError, match="EVIDENCE_REQUEST_INVALID"):
        cp.build_detections(entries())
    assert consumed == 101


def test_timeout_retry_reuses_identical_idempotency_key_and_body():
    bodies = []

    def handler(request):
        bodies.append(request.content)
        if len(bodies) == 1:
            raise httpx.ReadTimeout("ambiguous", request=request)
        return _response(request)

    result = _log(cp.HttpEvidenceSink("https://core.internal", transport=httpx.MockTransport(handler)))
    assert result.event_id == 41
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert json.loads(bodies[0])["idempotency_key"] == json.loads(bodies[1])["idempotency_key"]


def test_key_and_remote_auth_failures_have_stable_redacted_codes(monkeypatch):
    monkeypatch.delenv("XGEN_SERVICE_SIGNING_KEY")
    with pytest.raises(cp.EvidenceWriteError) as key_error:
        _log(cp.HttpEvidenceSink("https://core.internal", transport=httpx.MockTransport(_response)))
    assert key_error.value.code == "EVIDENCE_SDK_AUTH_UNAVAILABLE"

    monkeypatch.setenv("XGEN_SERVICE_SIGNING_KEY", KEY)
    denied = cp.HttpEvidenceSink(
        "https://core.internal",
        transport=httpx.MockTransport(lambda _r: httpx.Response(401, text="private server stack")),
    )
    with pytest.raises(cp.EvidenceWriteError) as auth_error:
        _log(denied)
    assert auth_error.value.code == "EVIDENCE_SDK_AUTH_DENIED"
    assert "private server stack" not in str(auth_error.value)


class RowsDb:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def find_records_by_condition(self, table, conditions, **kwargs):
        self.queries.append((table, conditions, kwargs))
        return self.rows


class CaptureSink:
    def __init__(self):
        self.request = None

    def ingest(self, request, *, actor):
        self.request = request
        return cp.EvidenceIngestResult(REF, 2, ())


def test_subject_resolution_requires_exactly_one_stable_row():
    sink = CaptureSink()
    db = RowsDb([{"id": 73}])
    cp.log_policy_event(
        db, source=cp.SOURCE_CHAT_INPUT, policy_type=cp.PII,
        detections=cp.build_detections([]), workflow_id="wf-shared", user_id=7,
        evidence_sink=sink,
    )
    assert sink.request.subject_type == "workflow" and sink.request.subject_id == 73
    assert db.queries == [("workflow_meta", {"workflow_id": "wf-shared"}, {"limit": 2, "select_columns": ["id"]})]

    for rows in ([], [{"id": 1}, {"id": 2}]):
        with pytest.raises(cp.EvidenceWriteError) as exc:
            cp.log_policy_event(
                RowsDb(rows), source=cp.SOURCE_CHAT_INPUT, policy_type=cp.PII,
                detections=cp.build_detections([]), workflow_id="wf-shared", user_id=7,
                evidence_sink=CaptureSink(),
            )
        assert exc.value.code == "EVIDENCE_SDK_SUBJECT_NOT_UNIQUE"


def test_collection_and_actor_subject_resolution_are_deterministic():
    collection_sink = CaptureSink()
    cp.log_policy_event(
        RowsDb([{"id": 22}]), source=cp.SOURCE_DOCUMENT_UPLOAD, policy_type=cp.PII,
        detections=cp.build_detections([]), collection_name="collection-a", user_id=7,
        evidence_sink=collection_sink,
    )
    assert (collection_sink.request.subject_type, collection_sink.request.subject_id) == ("collection", 22)

    actor_sink = CaptureSink()
    cp.log_policy_event(
        object(), source=cp.SOURCE_FILESTORE_UPLOAD, policy_type=cp.PII,
        detections=cp.build_detections([]), user_id=7, evidence_sink=actor_sink,
    )
    assert (actor_sink.request.subject_type, actor_sink.request.subject_id) == ("actor", 7)
