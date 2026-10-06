"""Wire contract for Workflow's journal-linked completed message pages.

Messages are a sparse projection of Agent Session events. A page is not a
complete conversation snapshot and must never advance the event replay cursor.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentSessionMessage(_WireModel):
    turn_id: UUID
    sequence: int = Field(ge=1, strict=True)
    status: Literal["completed", "failed", "cancelled"]
    input_text: str | None = Field(max_length=262144)
    output_text: str | None = Field(max_length=262144)
    content_complete: bool = Field(strict=True)
    source: Literal["unknown", "user", "subagent_report"]

    @model_validator(mode="after")
    def consistent_content_flags(self) -> AgentSessionMessage:
        if self.content_complete != (self.input_text is not None and self.output_text is not None):
            raise ValueError("content_complete does not match available text")
        if (self.input_text is None) != (self.source == "unknown"):
            raise ValueError("source does not match available input text")
        return self


class AgentSessionMessagePage(_WireModel):
    messages: list[AgentSessionMessage] = Field(max_length=20)
    next_cursor: int = Field(ge=0, strict=True)
    snapshot_sequence: int = Field(ge=0, strict=True)
    state_version: int = Field(ge=1, strict=True)
    has_more: bool = Field(strict=True)

    @model_validator(mode="after")
    def ordered_page(self) -> AgentSessionMessagePage:
        previous = 0
        seen: set[UUID] = set()
        for message in self.messages:
            if message.sequence <= previous or message.turn_id in seen:
                raise ValueError("duplicate or unordered linked message")
            previous = message.sequence
            seen.add(message.turn_id)
        if self.next_cursor > self.snapshot_sequence:
            raise ValueError("message cursor exceeds snapshot sequence")
        if self.messages and self.next_cursor != previous:
            raise ValueError("message cursor is not the last message sequence")
        if self.has_more and (not self.messages or self.next_cursor >= self.snapshot_sequence):
            raise ValueError("invalid linked message continuation")
        return self


class _MessageConflictDetail(_WireModel):
    code: Literal["SESSION_CURSOR_AHEAD", "SESSION_MESSAGE_LINK_INVALID"]
    current_sequence: int = Field(ge=0, strict=True)
    state_version: int = Field(ge=1, strict=True)


class AgentSessionMessageConflictResponse(_WireModel):
    detail: _MessageConflictDetail


class AgentSessionMessageCursor(_WireModel):
    """Last verified linked message for one Agent Session."""

    session_id: UUID
    sequence: int = Field(ge=0, strict=True)
    turn_id: UUID | None
    state_version: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def anchored_cursor(self) -> AgentSessionMessageCursor:
        if (self.sequence == 0) != (self.turn_id is None):
            raise ValueError("linked message cursor requires a turn anchor")
        return self


class AgentSessionMessageGap(ValueError):
    """Stop paging; do not infer missing content or a complete history."""


def apply_message_page(
    current: AgentSessionMessageCursor,
    page: AgentSessionMessagePage,
    *,
    session_id: UUID,
    after_sequence: int,
) -> tuple[AgentSessionMessageCursor, tuple[AgentSessionMessage, ...]]:
    """Apply a page using sparse message sequence and an exact turn anchor.

    ``after_sequence`` is the cursor sent to the messages endpoint. Delayed
    overlapping pages may repeat the last verified turn. Unlike event replay,
    message sequence numbers need not be adjacent, and ``has_more=false`` does
    not imply ``next_cursor == snapshot_sequence``.
    """
    if (
        session_id != current.session_id
        or type(after_sequence) is not int
        or not 0 <= after_sequence <= current.sequence
    ):
        raise AgentSessionMessageGap("invalid linked message request cursor")

    last = after_sequence
    for message in page.messages:
        if message.sequence <= last:
            raise AgentSessionMessageGap("linked message sequence did not advance")
        last = message.sequence
    if page.next_cursor != last or page.snapshot_sequence < last:
        raise AgentSessionMessageGap("inconsistent linked message cursor")

    if page.next_cursor < current.sequence:
        return current, ()
    if after_sequence < current.sequence and not any(
        message.sequence == current.sequence and message.turn_id == current.turn_id
        for message in page.messages
    ):
        raise AgentSessionMessageGap("linked message overlap lost its turn anchor")

    cursor = current
    accepted: list[AgentSessionMessage] = []
    for message in page.messages:
        if message.sequence <= cursor.sequence:
            continue
        if page.state_version < cursor.state_version or message.turn_id == cursor.turn_id:
            raise AgentSessionMessageGap("linked message state diverged")
        cursor = AgentSessionMessageCursor(
            session_id=session_id, sequence=message.sequence,
            turn_id=message.turn_id, state_version=page.state_version,
        )
        accepted.append(message)
    return AgentSessionMessageCursor(
        session_id=session_id, sequence=cursor.sequence, turn_id=cursor.turn_id,
        state_version=max(cursor.state_version, page.state_version),
    ), tuple(accepted)
