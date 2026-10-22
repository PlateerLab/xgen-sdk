"""Typed producer for Core-owned governance evidence.

Original samples live only in the request object. String conversion and
``repr`` expose metadata and opaque references only, so routine logging cannot
disclose retained evidence.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx

from xgen_sdk.auth.principal import (
    PrincipalConfigurationError,
    PrincipalValidationError,
    service_principal_headers,
)

INGEST_PERMISSION = "internal.governance-evidence:ingest"
CORE_AUDIENCE = "xgen-core"
CORE_URL_ENV = "CORE_SERVICE_BASE_URL"
DEFAULT_CORE_URL = "http://xgen-core:8000"
EVIDENCE_PATH = "/api/internal/governance/evidence"
MAX_DETECTIONS = 100
MAX_SAMPLES = 100
MAX_SAMPLE_TEXT = 16_384
_SOURCES = {
    "workflow_agent", "workflow_execution_meta", "workflow_guarder", "workflow_rag",
    "workflow_tool", "document_upload", "document_ocr_supplement", "chat_input",
    "chat_upload", "filestore_upload",
}
_POLICY_TYPES = {"pii", "forbidden_word", "guarder"}
_ERROR_CODES = {
    "EVIDENCE_ACCESS_DENIED", "EVIDENCE_ACTOR_MISMATCH", "EVIDENCE_AUTH_UNAVAILABLE", "EVIDENCE_EXPIRED",
    "EVIDENCE_IDEMPOTENCY_CONFLICT", "EVIDENCE_POLICY_INVALID",
    "EVIDENCE_PRODUCER_REQUIRED", "EVIDENCE_PURPOSE_DENIED",
    "EVIDENCE_REQUEST_INVALID", "EVIDENCE_RETENTION_UNCONFIGURED",
    "EVIDENCE_ROUNDTRIP_FAILED", "EVIDENCE_STORE_UNAVAILABLE",
    "EVIDENCE_SUBJECT_MISMATCH", "EVIDENCE_SUBJECT_UNAVAILABLE", "EVIDENCE_TOO_LARGE",
}


class EvidenceWriteError(RuntimeError):
    """Stable failure whose printable form never contains evidence or server text."""

    def __init__(self, code: str, *, request_id: str | None = None, retryable: bool = False) -> None:
        self.code = code if code in _ERROR_CODES or code.startswith("EVIDENCE_SDK_") else "EVIDENCE_SDK_UNAVAILABLE"
        try:
            self.request_id = str(UUID(str(request_id))) if request_id else uuid4().hex
        except (TypeError, ValueError):
            self.request_id = uuid4().hex
        self.retryable = bool(retryable)
        super().__init__(f"{self.code} (request_id={self.request_id})")


def _fail(code: str, *, retryable: bool = False) -> EvidenceWriteError:
    return EvidenceWriteError(code, retryable=retryable)


def _text(value: Any, *, maximum: int, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise _fail("EVIDENCE_REQUEST_INVALID")
    return value


@dataclass(frozen=True)
class EvidenceSample:
    original: str = field(repr=False)
    masked: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        _text(self.original, maximum=MAX_SAMPLE_TEXT)
        _text(self.masked, maximum=MAX_SAMPLE_TEXT)

    def wire(self) -> dict[str, Any]:
        return {"original": self.original, "masked": self.masked}


@dataclass(frozen=True)
class EvidenceDetection:
    policy_id: int | None
    policy_name: str
    count: int
    samples: tuple[EvidenceSample, ...] = ()
    enforced: bool = True

    def __post_init__(self) -> None:
        if self.policy_id is not None and type(self.policy_id) is not int:
            raise _fail("EVIDENCE_REQUEST_INVALID")
        _text(self.policy_name, maximum=100)
        if type(self.count) is not int or self.count < 0 or type(self.enforced) is not bool:
            raise _fail("EVIDENCE_REQUEST_INVALID")
        if len(self.samples) > MAX_SAMPLES or not all(isinstance(item, EvidenceSample) for item in self.samples):
            raise _fail("EVIDENCE_REQUEST_INVALID")

    def wire(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id, "policy_name": self.policy_name,
            "count": self.count, "enforced": self.enforced,
            "samples": [item.wire() for item in self.samples],
        }

    def safe(self, evidence_ref: str | None = None) -> dict[str, Any]:
        value: dict[str, Any] = {
            "policy_id": self.policy_id, "policy_name": self.policy_name,
            "count": self.count, "enforced": self.enforced, "samples": [],
        }
        if evidence_ref is not None:
            value["evidence_ref"] = evidence_ref
        return value


class EvidenceDetections(str):
    """Safe legacy JSON string carrying a private typed evidence payload."""

    def __new__(cls, values: Sequence[EvidenceDetection]):
        typed = tuple(values)
        if len(typed) > MAX_DETECTIONS or not all(isinstance(item, EvidenceDetection) for item in typed):
            raise _fail("EVIDENCE_REQUEST_INVALID")
        safe = json.dumps([item.safe() for item in typed], ensure_ascii=False, separators=(",", ":"))
        obj = super().__new__(cls, safe)
        obj._typed = typed
        return obj

    def wire(self) -> list[dict[str, Any]]:
        return [item.wire() for item in self._typed]


@dataclass(frozen=True)
class EvidenceEvent:
    source: str
    policy_type: str
    policy_ids: tuple[int, ...]
    policy_names: tuple[str, ...]
    total_detected: int
    workflow_id: str | None = None
    execution_id: str | None = None
    interaction_id: str | None = None
    node_id: str | None = None
    collection_name: str | None = None
    collection_display_name: str | None = None
    document_name: str | None = None
    user_id: int | None = None
    username: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.source not in _SOURCES or self.policy_type not in _POLICY_TYPES:
            raise _fail("EVIDENCE_REQUEST_INVALID")
        if (len(self.policy_ids) > 100 or any(type(value) is not int for value in self.policy_ids)
                or len(self.policy_names) > 100
                or any(not isinstance(value, str) or len(value) > 100 for value in self.policy_names)
                or type(self.total_detected) is not int or self.total_detected < 0):
            raise _fail("EVIDENCE_REQUEST_INVALID")
        for value, maximum in (
            (self.workflow_id, 255), (self.execution_id, 255), (self.interaction_id, 255),
            (self.node_id, 128), (self.collection_name, 255),
            (self.collection_display_name, 255), (self.document_name, 1024),
            (self.username, 255), (self.session_id, 128),
        ):
            _text(value, maximum=maximum, optional=True)
        if self.user_id is not None and (type(self.user_id) is not int or self.user_id <= 0):
            raise _fail("EVIDENCE_REQUEST_INVALID")

    def wire(self) -> dict[str, Any]:
        return {
            "source": self.source, "policy_type": self.policy_type,
            "policy_ids": list(self.policy_ids), "policy_names": list(self.policy_names),
            "total_detected": self.total_detected, "workflow_id": self.workflow_id,
            "execution_id": self.execution_id, "interaction_id": self.interaction_id,
            "node_id": self.node_id, "collection_name": self.collection_name,
            "collection_display_name": self.collection_display_name,
            "document_name": self.document_name, "user_id": self.user_id,
            "username": self.username, "session_id": self.session_id,
        }


@dataclass(frozen=True)
class EvidenceIngestRequest:
    idempotency_key: str
    subject_type: str
    subject_id: int
    event: EvidenceEvent
    detections: EvidenceDetections
    purpose: str = "policy-audit"

    def __post_init__(self) -> None:
        try:
            if not isinstance(self.idempotency_key, str):
                raise ValueError()
            UUID(self.idempotency_key)
        except (TypeError, ValueError):
            raise _fail("EVIDENCE_REQUEST_INVALID") from None
        if (self.purpose != "policy-audit" or self.subject_type not in {"workflow", "collection", "actor"}
                or type(self.subject_id) is not int or self.subject_id <= 0
                or not isinstance(self.event, EvidenceEvent)
                or type(self.detections) is not EvidenceDetections):
            raise _fail("EVIDENCE_REQUEST_INVALID")

    def wire(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key, "purpose": self.purpose,
            "subject_type": self.subject_type, "subject_id": self.subject_id,
            "event": self.event.wire(), "detections": self.detections.wire(),
        }


@dataclass(frozen=True)
class EvidenceIngestResult:
    evidence_ref: str
    event_id: int
    detections: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class EvidenceActor:
    user_id: int | None
    username: str | None = None

    def __post_init__(self) -> None:
        if self.user_id is not None and (type(self.user_id) is not int or self.user_id <= 0):
            raise _fail("EVIDENCE_REQUEST_INVALID")
        _text(self.username, maximum=255, optional=True)


class EvidenceSink(Protocol):
    def ingest(self, request: EvidenceIngestRequest, *, actor: EvidenceActor) -> EvidenceIngestResult: ...


def _core_url(value: str | None = None) -> str:
    raw = (value if value is not None else os.getenv(CORE_URL_ENV, DEFAULT_CORE_URL)).strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise _fail("EVIDENCE_SDK_CONFIGURATION")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _actor_headers(actor: EvidenceActor) -> dict[str, str]:
    # Some existing policy callers know the immutable actor ID but have no
    # display name. The principal transport still requires a nonempty label;
    # this label conveys no permissions and never replaces the bound ID.
    label = actor.username or (f"actor-{actor.user_id}" if actor.user_id else "anonymous")
    delegated = {
        "X-User-ID": str(actor.user_id or 0), "X-User-Name": quote(label, safe=""),
        "X-User-Superuser": "false", "X-User-Roles": "",
        "X-User-Permissions": INGEST_PERMISSION, "X-User-Supervision-Full": "",
    }
    try:
        return service_principal_headers(delegated, audience=CORE_AUDIENCE)
    except (PrincipalConfigurationError, PrincipalValidationError, ValueError, TypeError):
        raise _fail("EVIDENCE_SDK_AUTH_UNAVAILABLE") from None


def _server_error(response: httpx.Response) -> EvidenceWriteError:
    code = "EVIDENCE_SDK_REMOTE_FAILURE"
    request_id = None
    try:
        detail = response.json().get("detail")
        if isinstance(detail, dict):
            if detail.get("code") in _ERROR_CODES:
                code = detail["code"]
            request_id = detail.get("request_id")
        elif detail in _ERROR_CODES:
            code = detail
    except (ValueError, AttributeError, TypeError):
        pass
    if response.status_code in {401, 403} and code == "EVIDENCE_SDK_REMOTE_FAILURE":
        code = "EVIDENCE_SDK_AUTH_DENIED"
    return EvidenceWriteError(code, request_id=request_id, retryable=response.status_code >= 500)


def _result(response: httpx.Response) -> EvidenceIngestResult:
    try:
        body = response.json()
        ref = str(UUID(body["evidence_ref"]))
        event_id = body["event_id"]
        detections = body["detections"]
        if type(event_id) is not int or event_id <= 0 or not isinstance(detections, list) or len(detections) > MAX_DETECTIONS:
            raise ValueError()
        for item in detections:
            if (not isinstance(item, dict)
                    or set(item) != {"policy_id", "policy_name", "count", "enforced", "samples", "evidence_ref"}
                    or item.get("samples") != [] or item.get("evidence_ref") != ref
                    or type(item.get("count")) is not int or item["count"] < 0
                    or type(item.get("enforced")) is not bool
                    or (item.get("policy_id") is not None and type(item["policy_id"]) is not int)
                    or not isinstance(item.get("policy_name"), str) or len(item["policy_name"]) > 100):
                raise ValueError()
        return EvidenceIngestResult(ref, event_id, tuple(detections))
    except (KeyError, TypeError, ValueError):
        raise _fail("EVIDENCE_SDK_RESPONSE_INVALID") from None


class HttpEvidenceSink:
    """Synchronous internal HTTP transport with one idempotent retry."""

    def __init__(self, base_url: str | None = None, *, timeout: float = 5.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
            raise _fail("EVIDENCE_SDK_CONFIGURATION")
        self.url = _core_url(base_url) + EVIDENCE_PATH
        self.timeout = float(timeout)
        self.transport = transport

    def ingest(self, request: EvidenceIngestRequest, *, actor: EvidenceActor) -> EvidenceIngestResult:
        headers = _actor_headers(actor)
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False, follow_redirects=False,
                              transport=self.transport) as client:
                response = None
                for attempt in range(2):
                    try:
                        response = client.post(self.url, headers=headers, json=request.wire())
                        break
                    except httpx.TimeoutException:
                        if attempt:
                            raise _fail("EVIDENCE_SDK_TIMEOUT", retryable=True) from None
                assert response is not None
        except EvidenceWriteError:
            raise
        except (httpx.HTTPError, httpx.InvalidURL):
            raise _fail("EVIDENCE_SDK_UNAVAILABLE", retryable=True) from None
        if not 200 <= response.status_code < 300:
            raise _server_error(response)
        return _result(response)


__all__ = [
    "CORE_URL_ENV", "DEFAULT_CORE_URL", "EVIDENCE_PATH", "INGEST_PERMISSION",
    "EvidenceActor", "EvidenceDetection", "EvidenceDetections", "EvidenceEvent",
    "EvidenceIngestRequest", "EvidenceIngestResult", "EvidenceSample", "EvidenceSink",
    "EvidenceWriteError", "HttpEvidenceSink", "MAX_DETECTIONS", "MAX_SAMPLES", "MAX_SAMPLE_TEXT",
]
