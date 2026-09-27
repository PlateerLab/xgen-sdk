"""Wire contract and ordered replay for Workflow's account focus API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateAgentSession(_WireModel):
    workflow_id: str = Field(min_length=1, max_length=256)
    expected_version: int = Field(ge=0, strict=True)
    title: str = Field(default="", max_length=256)
    origin_id: str | None = Field(default=None, min_length=1, max_length=128)


class SwitchFocus(_WireModel):
    active_agent_session_id: UUID | None
    expected_version: int = Field(ge=0, strict=True)
    origin_id: str | None = Field(default=None, min_length=1, max_length=128)


class FocusState(_WireModel):
    active_agent_session_id: UUID | None
    version: int = Field(ge=0, strict=True)
    event_id: UUID | None


class AgentSessionCreated(_WireModel):
    id: UUID
    workflow_id: str
    focus: FocusState

    @model_validator(mode="after")
    def created_session_is_focused(self) -> AgentSessionCreated:
        if self.focus.active_agent_session_id != self.id:
            raise ValueError("created Agent Session must be the account focus")
        return self


class AccountFocusEvent(_WireModel):
    event_id: UUID
    sequence: int = Field(ge=1, strict=True)
    event_type: Literal["agent_session.focus_changed"]
    previous_agent_session_id: UUID | None
    active_agent_session_id: UUID | None
    origin_id: str | None
    created_at: datetime


class AccountEventPage(_WireModel):
    events: list[AccountFocusEvent]
    next_cursor: int = Field(ge=0, strict=True)
    snapshot_version: int = Field(ge=0, strict=True)
    has_more: bool = Field(strict=True)


class _FocusConflictDetail(_WireModel):
    code: Literal["FOCUS_VERSION_CONFLICT"]
    current: FocusState


class FocusConflictResponse(_WireModel):
    detail: _FocusConflictDetail


class _CursorConflictDetail(_WireModel):
    code: Literal["ACCOUNT_CURSOR_AHEAD", "ACCOUNT_CURSOR_GAP"]
    current: FocusState


class CursorConflictResponse(_WireModel):
    detail: _CursorConflictDetail


class FocusReplayGap(ValueError):
    """Discard the cursor and GET /me/agent-state again before replaying."""


def apply_event_page(
    current: FocusState, page: AccountEventPage, *, after_sequence: int
) -> FocusState:
    """Apply a replay page without accepting a missing or reordered focus event.

    ``after_sequence`` is the cursor sent to GET /me/agent-events. A duplicate
    response is harmless; a new event must follow the current version and
    previous session pointer exactly. The returned version is at least the page
    cursor, including when a delayed duplicate page arrives after newer events.
    When this raises, fetch a fresh focus snapshot rather than guessing state.
    """
    if type(after_sequence) is not int or after_sequence < 0 or after_sequence > current.version:
        raise FocusReplayGap("invalid replay cursor")

    expected = after_sequence
    for event in page.events:
        expected += 1
        if event.sequence != expected:
            raise FocusReplayGap("account event sequence gap")
    if page.next_cursor != expected or page.snapshot_version < expected:
        raise FocusReplayGap("inconsistent replay cursor")
    if page.has_more != (page.next_cursor < page.snapshot_version):
        raise FocusReplayGap("inconsistent replay continuation")

    state = current
    for event in page.events:
        if event.sequence < state.version:
            continue
        if event.sequence == state.version:
            if (
                (state.event_id is not None and event.event_id != state.event_id)
                or event.active_agent_session_id != state.active_agent_session_id
            ):
                raise FocusReplayGap("conflicting duplicate account event")
            continue
        if event.sequence != state.version + 1 or event.previous_agent_session_id != state.active_agent_session_id:
            raise FocusReplayGap("account focus diverged")
        state = FocusState(
            active_agent_session_id=event.active_agent_session_id,
            version=event.sequence,
            event_id=event.event_id,
        )
    return state
