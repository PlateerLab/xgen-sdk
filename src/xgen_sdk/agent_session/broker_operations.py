"""Strict clients for canonical Agent Session broker-operation controls.

The caller supplies authenticated JSON transports.  This module validates only
credential-free references, metadata, and cancellation acceptance; it never
receives a broker grant, lease, request body, or cached result.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


AGENT_SESSION_BROKER_OPERATION_MAX_COUNT = 128
AGENT_SESSION_BROKER_OPERATION_MAX_EXPIRY = 9_007_199_254_740_991

_ERROR_MESSAGE = "Invalid canonical Agent Session broker operation"
AGENT_SESSION_BROKER_OPERATIONS = frozenset(
    {
        "documents.collections.list",
        "documents.collection.documents.list",
        "workflow.actor.authorize",
        "documents.attachment.index",
        "workflow.code.execute",
        "workflow.attachments.stage",
    }
)
_SANDBOX_OPERATIONS = frozenset(
    {"workflow.code.execute", "workflow.attachments.stage"}
)
_REFERENCE_FIELDS = frozenset({"invocation_id", "operation_id"})
_METADATA_FIELDS = frozenset(
    {
        "invocation_id",
        "operation_id",
        "state",
        "cancel_requested",
        "target_cleanup",
        "result_available",
        "expires_at",
    }
)
_CANCELLATION_FIELDS = frozenset({"invocation_id", "cancel_requested"})


class AgentSessionBrokerOperationValidationError(ValueError):
    """The supplied value is not a canonical public broker-operation value."""


def _canonical_uuid(value: Any) -> UUID:
    if type(value) is UUID:
        rendered = str(value)
    elif type(value) is str:
        rendered = value
    else:
        raise ValueError("identifier must be a UUID")
    parsed = UUID(rendered)
    if parsed.int == 0 or str(parsed) != rendered:
        raise ValueError("identifier must be canonical and nonzero")
    return parsed


class _ExactFrozenBrokerModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    _wire_fields: ClassVar[frozenset[str]]

    @classmethod
    def model_construct(
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> _ExactFrozenBrokerModel:
        if set(values) - cls._wire_fields or (
            _fields_set is not None and set(_fields_set) - cls._wire_fields
        ):
            raise AgentSessionBrokerOperationValidationError(_ERROR_MESSAGE) from None
        return super().model_construct(_fields_set=_fields_set, **values)


class AgentSessionBrokerOperationReference(_ExactFrozenBrokerModel):
    """Credential-free reference retained with an owned Agent Session turn."""

    _wire_fields = _REFERENCE_FIELDS

    invocation_id: UUID
    operation_id: Literal[
        "documents.collections.list",
        "documents.collection.documents.list",
        "workflow.actor.authorize",
        "documents.attachment.index",
        "workflow.code.execute",
        "workflow.attachments.stage",
    ]

    @field_validator("invocation_id", mode="before")
    @classmethod
    def canonical_invocation_id(cls, value: Any) -> UUID:
        return _canonical_uuid(value)


class AgentSessionBrokerOperationMetadata(_ExactFrozenBrokerModel):
    """Public point-in-time metadata with no result, grant, or lease material."""

    _wire_fields = _METADATA_FIELDS

    invocation_id: UUID
    operation_id: Literal[
        "documents.collections.list",
        "documents.collection.documents.list",
        "workflow.actor.authorize",
        "documents.attachment.index",
        "workflow.code.execute",
        "workflow.attachments.stage",
    ]
    state: Literal["running", "completed", "failed", "cancelled", "unknown"]
    cancel_requested: bool = Field(strict=True)
    target_cleanup: Literal["not_applicable", "pending", "complete", "unknown"]
    result_available: bool = Field(strict=True)
    expires_at: int = Field(
        ge=1,
        le=AGENT_SESSION_BROKER_OPERATION_MAX_EXPIRY,
        strict=True,
    )

    @field_validator("invocation_id", mode="before")
    @classmethod
    def canonical_invocation_id(cls, value: Any) -> UUID:
        return _canonical_uuid(value)

    @model_validator(mode="after")
    def coherent_public_metadata(self) -> AgentSessionBrokerOperationMetadata:
        sandbox = self.operation_id in _SANDBOX_OPERATIONS
        if sandbox == (self.target_cleanup == "not_applicable"):
            raise ValueError("operation and cleanup projection are inconsistent")
        if self.result_available and (not sandbox or self.state != "completed"):
            raise ValueError("result availability is inconsistent")
        return self


class AgentSessionBrokerOperationCancellation(_ExactFrozenBrokerModel):
    """Cancellation acceptance; this is not final target-cleanup evidence."""

    _wire_fields = _CANCELLATION_FIELDS

    invocation_id: UUID
    cancel_requested: Literal[True]

    @field_validator("invocation_id", mode="before")
    @classmethod
    def canonical_invocation_id(cls, value: Any) -> UUID:
        return _canonical_uuid(value)

    @field_validator("cancel_requested", mode="before")
    @classmethod
    def exact_cancellation_acknowledgement(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("cancellation acknowledgement must be true")
        return True


class AgentSessionBrokerOperationsGetTransport(Protocol):
    """Caller-owned authenticated JSON GET callback."""

    async def __call__(self, path: str, /) -> object: ...


class AgentSessionBrokerOperationsPostTransport(Protocol):
    """Caller-owned authenticated empty-body JSON POST callback."""

    async def __call__(self, path: str, /) -> object: ...


def _identifier(value: Any) -> UUID:
    try:
        return _canonical_uuid(value)
    except Exception:
        pass
    raise AgentSessionBrokerOperationValidationError(_ERROR_MESSAGE) from None


def _model_input(
    value: Any,
    *,
    model_type: type[_ExactFrozenBrokerModel],
    fields: frozenset[str],
) -> dict[str, Any]:
    if type(value) is dict:
        if set(value) != fields:
            raise ValueError("response fields are not exact")
        return {field: value[field] for field in fields}
    if type(value) is not model_type:
        raise ValueError("unexpected broker-operation model type")
    if set(value.__dict__) != fields:
        raise ValueError("broker-operation model fields are not exact")
    extra = getattr(value, "__pydantic_extra__", None)
    if extra not in (None, {}):
        raise ValueError("broker-operation model has extra fields")
    fields_set = getattr(value, "__pydantic_fields_set__", set())
    if not isinstance(fields_set, set) or not fields_set <= fields:
        raise ValueError("broker-operation model field set is invalid")
    return {field: value.__dict__[field] for field in fields}


def _parse_reference(value: Any) -> AgentSessionBrokerOperationReference:
    return AgentSessionBrokerOperationReference.model_validate(
        _model_input(
            value,
            model_type=AgentSessionBrokerOperationReference,
            fields=_REFERENCE_FIELDS,
        ),
        strict=True,
    )


def _expected_reference(value: Any) -> AgentSessionBrokerOperationReference:
    try:
        return _parse_reference(value)
    except Exception:
        pass
    raise AgentSessionBrokerOperationValidationError(_ERROR_MESSAGE) from None


def parse_agent_session_broker_operation_references(
    value: Any,
) -> tuple[AgentSessionBrokerOperationReference, ...]:
    """Revalidate one bare array of at most 128 unique public references."""
    try:
        if type(value) is not list or len(value) > AGENT_SESSION_BROKER_OPERATION_MAX_COUNT:
            raise ValueError("operation reference list is invalid")
        parsed = tuple(_parse_reference(item) for item in value)
        invocation_ids = {item.invocation_id for item in parsed}
        if len(invocation_ids) != len(parsed):
            raise ValueError("operation invocation identifiers must be unique")
        return parsed
    except Exception:
        pass
    raise AgentSessionBrokerOperationValidationError(_ERROR_MESSAGE) from None


def parse_agent_session_broker_operation_metadata(
    value: Any,
    *,
    expected_reference: AgentSessionBrokerOperationReference,
) -> AgentSessionBrokerOperationMetadata:
    """Revalidate exact metadata and bind invocation and operation identifiers."""
    try:
        reference = _parse_reference(expected_reference)
        parsed = AgentSessionBrokerOperationMetadata.model_validate(
            _model_input(
                value,
                model_type=AgentSessionBrokerOperationMetadata,
                fields=_METADATA_FIELDS,
            ),
            strict=True,
        )
        if (
            parsed.invocation_id != reference.invocation_id
            or parsed.operation_id != reference.operation_id
        ):
            raise ValueError("metadata does not match the expected reference")
        return parsed
    except Exception:
        pass
    raise AgentSessionBrokerOperationValidationError(_ERROR_MESSAGE) from None


def parse_agent_session_broker_operation_cancellation(
    value: Any,
    *,
    expected_reference: AgentSessionBrokerOperationReference,
) -> AgentSessionBrokerOperationCancellation:
    """Revalidate an exact cancellation acknowledgement and bind invocation."""
    try:
        reference = _parse_reference(expected_reference)
        parsed = AgentSessionBrokerOperationCancellation.model_validate(
            _model_input(
                value,
                model_type=AgentSessionBrokerOperationCancellation,
                fields=_CANCELLATION_FIELDS,
            ),
            strict=True,
        )
        if parsed.invocation_id != reference.invocation_id:
            raise ValueError("cancellation does not match the expected reference")
        return parsed
    except Exception:
        pass
    raise AgentSessionBrokerOperationValidationError(_ERROR_MESSAGE) from None


class AgentSessionBrokerOperationsClient:
    """List, read, and cancel through caller-authenticated fixed-path callbacks."""

    __slots__ = ("_get", "_post")

    def __init__(
        self,
        get: AgentSessionBrokerOperationsGetTransport,
        post: AgentSessionBrokerOperationsPostTransport,
    ) -> None:
        if not callable(get):
            raise TypeError("get transport must be callable")
        if not callable(post):
            raise TypeError("post transport must be callable")
        self._get = get
        self._post = post

    @staticmethod
    def _base_path(session_id: Any, turn_id: Any) -> str:
        session = _identifier(session_id)
        turn = _identifier(turn_id)
        return (
            f"/api/agentflow/agent-sessions/{session}"
            f"/turns/{turn}/operations"
        )

    async def list(
        self, session_id: str | UUID, turn_id: str | UUID
    ) -> tuple[AgentSessionBrokerOperationReference, ...]:
        path = self._base_path(session_id, turn_id)
        value = await self._get(path)
        return parse_agent_session_broker_operation_references(value)

    async def read(
        self,
        session_id: str | UUID,
        turn_id: str | UUID,
        reference: AgentSessionBrokerOperationReference,
    ) -> AgentSessionBrokerOperationMetadata:
        path = self._base_path(session_id, turn_id)
        expected = _expected_reference(reference)
        value = await self._get(f"{path}/{expected.invocation_id}")
        return parse_agent_session_broker_operation_metadata(
            value, expected_reference=expected
        )

    async def cancel(
        self,
        session_id: str | UUID,
        turn_id: str | UUID,
        reference: AgentSessionBrokerOperationReference,
    ) -> AgentSessionBrokerOperationCancellation:
        path = self._base_path(session_id, turn_id)
        expected = _expected_reference(reference)
        value = await self._post(f"{path}/{expected.invocation_id}/cancel")
        return parse_agent_session_broker_operation_cancellation(
            value, expected_reference=expected
        )


__all__ = [
    "AGENT_SESSION_BROKER_OPERATION_MAX_COUNT",
    "AGENT_SESSION_BROKER_OPERATION_MAX_EXPIRY",
    "AGENT_SESSION_BROKER_OPERATIONS",
    "AgentSessionBrokerOperationCancellation",
    "AgentSessionBrokerOperationMetadata",
    "AgentSessionBrokerOperationReference",
    "AgentSessionBrokerOperationValidationError",
    "AgentSessionBrokerOperationsClient",
    "AgentSessionBrokerOperationsGetTransport",
    "AgentSessionBrokerOperationsPostTransport",
    "parse_agent_session_broker_operation_cancellation",
    "parse_agent_session_broker_operation_metadata",
    "parse_agent_session_broker_operation_references",
]
