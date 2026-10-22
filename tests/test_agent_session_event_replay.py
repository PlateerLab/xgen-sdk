"""Workflow HTTP and WebSocket Agent Session replay contract fixtures."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from xgen_sdk.agent_session import (
    AgentSessionEventFrame,
    AgentSessionEventPage,
    AgentSessionReplayCursor,
    AgentSessionReplayGap,
    AgentSessionSnapshot,
    SessionCursorConflictResponse,
    apply_session_event_page,
)


SID = "018f1240-0000-7000-8000-000000000001"
EVENT1 = "018f1240-0000-7000-8000-000000000002"
EVENT2 = "018f1240-0000-7000-8000-000000000003"
EVENT3 = "018f1240-0000-7000-8000-000000000004"
TURN = "018f1240-0000-7000-8000-000000000005"


def event(sequence: int, event_id: str) -> dict:
    return {
        "event_id": event_id,
        "sequence": sequence,
        "event_type": "turn.accepted",
        "turn_id": TURN,
        "platform_session_id": SID,
        "device_id": SID,
        "origin_id": "cli-1",
        "idempotency_key": "submission-1",
        "payload_ref": None,
        "created_at": "2026-09-29T04:00:00+00:00",
    }


def page(events: list[dict], next_cursor: int, snapshot_sequence: int,
         *, state_version: int = 3, has_more: bool = False) -> AgentSessionEventPage:
    return AgentSessionEventPage.model_validate({
        "events": events,
        "next_cursor": next_cursor,
        "snapshot_sequence": snapshot_sequence,
        "state_version": state_version,
        "has_more": has_more,
    })


def initial() -> AgentSessionReplayCursor:
    return AgentSessionReplayCursor(sequence=0, event_id=None, state_version=1)


def test_snapshot_frame_and_cursor_conflict_match_workflow_wire_shape():
    snapshot = AgentSessionSnapshot.model_validate({
        "id": SID, "workflow_id": "owned", "title": "My session",
        "current_sequence": 2, "state_version": 3,
        "latest_turn": {"id": TURN, "status": "accepted", "accepted_sequence": 2},
        "message_history_complete": False,
    })
    assert snapshot.id == UUID(SID)
    assert snapshot.latest_turn.accepted_sequence == snapshot.current_sequence
    frame = AgentSessionEventFrame.model_validate({
        "type": "agent_session.events", **page([event(1, EVENT1)], 1, 2, has_more=True).model_dump(mode="json"),
    })
    assert frame.events[0].event_id == UUID(EVENT1)
    assert frame.has_more is True
    conflict = SessionCursorConflictResponse.model_validate({
        "detail": {"code": "SESSION_CURSOR_GAP", "current_sequence": 2, "state_version": 3},
    })
    assert conflict.detail.current_sequence == 2
    ahead = SessionCursorConflictResponse.model_validate({
        "detail": {"code": "SESSION_CURSOR_AHEAD", "current_sequence": 2, "state_version": 3},
    })
    assert ahead.detail.code == "SESSION_CURSOR_AHEAD"
    with pytest.raises(ValidationError):
        AgentSessionEventFrame.model_validate({
            **frame.model_dump(mode="json"), "type": "agent_session.focus_changed",
        })


def test_paginated_replay_and_overlapping_retry_emit_each_event_once():
    first = page([event(1, EVENT1)], 1, 2, has_more=True)
    cursor, accepted = apply_session_event_page(initial(), first, after_sequence=0)
    assert cursor.sequence == 1
    assert [item.event_id for item in accepted] == [UUID(EVENT1)]

    duplicate_cursor, duplicate = apply_session_event_page(cursor, first, after_sequence=0)
    assert duplicate_cursor == cursor
    assert duplicate == ()

    overlap = page([event(1, EVENT1), event(2, EVENT2)], 2, 2)
    cursor, accepted = apply_session_event_page(cursor, overlap, after_sequence=0)
    assert cursor.sequence == 2
    assert cursor.event_id == UUID(EVENT2)
    assert [item.sequence for item in accepted] == [2]

    empty = page([], 2, 2)
    assert apply_session_event_page(cursor, empty, after_sequence=2) == (cursor, ())
    newer_state = page([], 2, 2, state_version=4)
    cursor, accepted = apply_session_event_page(cursor, newer_state, after_sequence=2)
    assert accepted == ()
    assert cursor.state_version == 4
    next_frame = AgentSessionEventFrame.model_validate({
        "type": "agent_session.events", **page([event(3, EVENT3)], 3, 3, state_version=4).model_dump(mode="json"),
    })
    cursor, accepted = apply_session_event_page(cursor, next_frame, after_sequence=2)
    assert (cursor.sequence, cursor.state_version, len(accepted)) == (3, 4, 1)


@pytest.mark.parametrize("broken,after", [
    (lambda: page([event(2, EVENT1)], 2, 2), 0),
    (lambda: page([event(1, EVENT1)], 0, 1), 0),
    (lambda: page([event(1, EVENT1)], 1, 0), 0),
    (lambda: page([event(1, EVENT1)], 1, 2), 0),
    (lambda: page([event(1, EVENT1)], 1, 1, has_more=True), 0),
    (lambda: page([], 0, 0), True),
])
def test_replay_rejects_gap_bad_cursor_or_inconsistent_page(broken, after):
    with pytest.raises(AgentSessionReplayGap):
        apply_session_event_page(initial(), broken(), after_sequence=after)


def test_replay_rejects_changed_duplicate_and_state_downgrade():
    cursor = AgentSessionReplayCursor(sequence=1, event_id=UUID(EVENT1), state_version=3)
    with pytest.raises(AgentSessionReplayGap):
        apply_session_event_page(cursor, page([event(1, EVENT2)], 1, 1), after_sequence=0)
    with pytest.raises(AgentSessionReplayGap):
        apply_session_event_page(
            cursor, page([event(2, EVENT2)], 2, 2, state_version=2), after_sequence=1,
        )
    with pytest.raises(AgentSessionReplayGap):
        apply_session_event_page(cursor, page([event(3, EVENT3)], 3, 3), after_sequence=2)


def test_wire_models_reject_incomplete_or_ill_typed_events():
    with pytest.raises(ValidationError):
        page([dict(event(1, EVENT1), sequence=True)], 1, 1)
    with pytest.raises(ValidationError):
        page([dict(event(1, EVENT1), secret="unexpected")], 1, 1)
    with pytest.raises(ValidationError):
        AgentSessionSnapshot.model_validate({
            "id": SID, "workflow_id": "owned", "title": "", "current_sequence": -1,
            "state_version": 1, "latest_turn": None, "message_history_complete": True,
        })
