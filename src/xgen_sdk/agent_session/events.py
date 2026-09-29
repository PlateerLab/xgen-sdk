"""Wire models and ordered replay for Workflow Agent Session events.

Authentication and ownership are enforced by Gateway and Workflow. Callers
must obtain these responses through an authorized Platform Session.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentSessionTurnSummary(_WireModel):
    id: UUID
    status: str
    accepted_sequence: int = Field(ge=1, strict=True)


class AgentSessionSnapshot(_WireModel):
    id: UUID
    workflow_id: str
    title: str
    current_sequence: int = Field(ge=0, strict=True)
    state_version: int = Field(ge=1, strict=True)
    latest_turn: AgentSessionTurnSummary | None
    message_history_complete: bool = Field(strict=True)


class AgentSessionEvent(_WireModel):
    event_id: UUID
    sequence: int = Field(ge=1, strict=True)
    event_type: str = Field(min_length=1)
    turn_id: UUID | None
    platform_session_id: UUID | None
    device_id: UUID | None
    origin_id: str | None
    idempotency_key: str | None
    payload_ref: str | None
    created_at: datetime


class AgentSessionEventPage(_WireModel):
    events: list[AgentSessionEvent]
    next_cursor: int = Field(ge=0, strict=True)
    snapshot_sequence: int = Field(ge=0, strict=True)
    state_version: int = Field(ge=1, strict=True)
    has_more: bool = Field(strict=True)


class AgentSessionEventFrame(AgentSessionEventPage):
    type: Literal["agent_session.events"]


class _SessionCursorConflictDetail(_WireModel):
    code: Literal["SESSION_CURSOR_AHEAD", "SESSION_CURSOR_GAP"]
    current_sequence: int = Field(ge=0, strict=True)
    state_version: int = Field(ge=1, strict=True)


class SessionCursorConflictResponse(_WireModel):
    detail: _SessionCursorConflictDetail


class AgentSessionReplayCursor(_WireModel):
    """Last verified event, persisted by a client for reconnection."""

    sequence: int = Field(ge=0, strict=True)
    event_id: UUID | None
    state_version: int = Field(ge=1, strict=True)


class AgentSessionReplayGap(ValueError):
    """Stop replay; fetch a fresh snapshot and reconcile local state."""


def apply_session_event_page(
    current: AgentSessionReplayCursor,
    page: AgentSessionEventPage,
    *, after_sequence: int,
) -> tuple[AgentSessionReplayCursor, tuple[AgentSessionEvent, ...]]:
    """Accept only a continuous page and return events not previously applied.

    ``after_sequence`` is the cursor sent to HTTP ``after_sequence`` or WS
    ``after_seq``. A delayed duplicate page is harmless. A new event must
    follow the last verified sequence; on a gap or cursor conflict, callers
    must stop rather than advance the cursor.
    """
    if type(after_sequence) is not int or not 0 <= after_sequence <= current.sequence:
        raise AgentSessionReplayGap("invalid replay cursor")

    expected = after_sequence
    for event in page.events:
        expected += 1
        if event.sequence != expected:
            raise AgentSessionReplayGap("Agent Session event sequence gap")
    if page.next_cursor != expected or page.snapshot_sequence < expected:
        raise AgentSessionReplayGap("inconsistent replay cursor")
    if page.has_more != (page.next_cursor < page.snapshot_sequence):
        raise AgentSessionReplayGap("inconsistent replay continuation")

    cursor = current
    accepted: list[AgentSessionEvent] = []
    for event in page.events:
        if event.sequence < cursor.sequence:
            continue
        if event.sequence == cursor.sequence:
            if cursor.event_id is not None and cursor.event_id != event.event_id:
                raise AgentSessionReplayGap("conflicting duplicate Agent Session event")
            continue
        if event.sequence != cursor.sequence + 1 or page.state_version < cursor.state_version:
            raise AgentSessionReplayGap("Agent Session event state diverged")
        cursor = AgentSessionReplayCursor(
            sequence=event.sequence,
            event_id=event.event_id,
            state_version=page.state_version,
        )
        accepted.append(event)

    return AgentSessionReplayCursor(
        sequence=cursor.sequence,
        event_id=cursor.event_id,
        state_version=max(cursor.state_version, page.state_version),
    ), tuple(accepted)
