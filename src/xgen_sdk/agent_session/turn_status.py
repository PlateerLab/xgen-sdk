"""Strict read contract for one canonical Agent Session turn status.

The transport is supplied by the caller so the SDK never owns credentials or
authentication policy.  A status is a point-in-time server projection: a
``running`` value does not prove that a worker is live, and ``cancelled`` does
not prove that target-side cleanup has completed.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_ERROR_MESSAGE = "Invalid canonical Agent Session turn status"
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_FIELDS = frozenset(
    {
        "turn_id",
        "agent_session_id",
        "status",
        "progress",
        "accepted_sequence",
        "started_sequence",
        "terminal_sequence",
        "result_available",
        "state_version",
    }
)
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class AgentSessionTurnStatusValidationError(ValueError):
    """The supplied value is not a canonical turn-status response."""


def _canonical_uuid(value: Any) -> UUID:
    if type(value) is UUID:
        rendered = str(value)
    elif type(value) is str:
        rendered = value
    else:
        raise ValueError("identifier must be a UUID")
    if not _UUID.fullmatch(rendered):
        raise ValueError("identifier must be a canonical non-nil UUID")
    return UUID(rendered)


class AgentSessionTurnStatus(BaseModel):
    """Frozen, exact DTO returned by the single-turn status endpoint."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    turn_id: UUID
    agent_session_id: UUID
    status: Literal["accepted", "running", "completed", "failed", "cancelled"]
    progress: Literal[
        "awaiting_claim",
        "running_or_unknown",
        "awaiting_reconciliation",
        "final",
    ]
    accepted_sequence: int = Field(ge=1, strict=True)
    started_sequence: int | None = Field(ge=1, strict=True)
    terminal_sequence: int | None = Field(ge=1, strict=True)
    result_available: bool = Field(strict=True)
    state_version: int = Field(ge=1, strict=True)

    @field_validator("turn_id", "agent_session_id", mode="before")
    @classmethod
    def canonical_identifier(cls, value: Any) -> UUID:
        return _canonical_uuid(value)

    @model_validator(mode="after")
    def coherent_projection(self) -> AgentSessionTurnStatus:
        allowed_progress = {
            "accepted": {"awaiting_claim", "awaiting_reconciliation"},
            "running": {"running_or_unknown", "awaiting_reconciliation"},
            "completed": {"final"},
            "failed": {"final"},
            "cancelled": {"final"},
        }
        if self.progress not in allowed_progress[self.status]:
            raise ValueError("status and progress are inconsistent")

        if self.status == "accepted" and self.started_sequence is not None:
            raise ValueError("an accepted turn cannot have a start sequence")
        if (
            self.started_sequence is not None
            and self.started_sequence <= self.accepted_sequence
        ):
            raise ValueError("start sequence must follow acceptance")
        if self.terminal_sequence is not None:
            if self.terminal_sequence <= self.accepted_sequence:
                raise ValueError("terminal sequence must follow acceptance")
            if (
                self.started_sequence is not None
                and self.terminal_sequence <= self.started_sequence
            ):
                raise ValueError("terminal sequence must follow start")
        if self.result_available and (
            self.status not in _TERMINAL_STATUSES
            or self.terminal_sequence is None
        ):
            raise ValueError("available results require terminal evidence")
        return self

    @classmethod
    def model_construct(
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> AgentSessionTurnStatus:
        if set(values) - _FIELDS or (
            _fields_set is not None and set(_fields_set) - _FIELDS
        ):
            raise AgentSessionTurnStatusValidationError(_ERROR_MESSAGE) from None
        return super().model_construct(_fields_set=_fields_set, **values)


class AgentSessionTurnStatusGetTransport(Protocol):
    """Caller-owned authenticated JSON GET callback."""

    async def __call__(self, path: str, /) -> object: ...


def _identifier(value: Any) -> UUID:
    try:
        return _canonical_uuid(value)
    except Exception:
        pass
    raise AgentSessionTurnStatusValidationError(_ERROR_MESSAGE) from None


def _model_input(value: Any) -> dict[str, Any]:
    if type(value) is dict:
        if set(value) != _FIELDS:
            raise ValueError("response fields are not exact")
        return {field: value[field] for field in _FIELDS}
    if type(value) is not AgentSessionTurnStatus:
        raise ValueError("unexpected turn-status model type")
    if set(value.__dict__) != _FIELDS:
        raise ValueError("turn-status model fields are not exact")
    extra = getattr(value, "__pydantic_extra__", None)
    if extra not in (None, {}):
        raise ValueError("turn-status model has extra fields")
    fields_set = getattr(value, "__pydantic_fields_set__", set())
    if not isinstance(fields_set, set) or not fields_set <= _FIELDS:
        raise ValueError("turn-status model field set is invalid")
    return {field: value.__dict__[field] for field in _FIELDS}


def parse_agent_session_turn_status(
    value: Any,
    *,
    expected_session_id: str | UUID,
    expected_turn_id: str | UUID,
) -> AgentSessionTurnStatus:
    """Fully revalidate an exact response and bind it to the requested IDs."""
    expected_session = _identifier(expected_session_id)
    expected_turn = _identifier(expected_turn_id)
    try:
        parsed = AgentSessionTurnStatus.model_validate(_model_input(value), strict=True)
        if (
            parsed.agent_session_id != expected_session
            or parsed.turn_id != expected_turn
        ):
            raise ValueError("response identity does not match the request")
        return parsed
    except Exception:
        pass
    raise AgentSessionTurnStatusValidationError(_ERROR_MESSAGE) from None


class AgentSessionTurnStatusClient:
    """Read one turn through a caller-authenticated, fixed-path GET callback."""

    __slots__ = ("_get",)

    def __init__(self, get: AgentSessionTurnStatusGetTransport) -> None:
        if not callable(get):
            raise TypeError("get transport must be callable")
        self._get = get

    async def read(
        self, session_id: str | UUID, turn_id: str | UUID
    ) -> AgentSessionTurnStatus:
        canonical_session = _identifier(session_id)
        canonical_turn = _identifier(turn_id)
        path = (
            f"/api/agentflow/agent-sessions/{canonical_session}"
            f"/turns/{canonical_turn}"
        )
        value = await self._get(path)
        return parse_agent_session_turn_status(
            value,
            expected_session_id=canonical_session,
            expected_turn_id=canonical_turn,
        )


__all__ = [
    "AgentSessionTurnStatus",
    "AgentSessionTurnStatusClient",
    "AgentSessionTurnStatusGetTransport",
    "AgentSessionTurnStatusValidationError",
    "parse_agent_session_turn_status",
]
