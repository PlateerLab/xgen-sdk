"""Workflow linked-message wire shape and sparse pagination safety."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from xgen_sdk.agent_session import (
    AgentSessionMessageConflictResponse,
    AgentSessionMessageCursor,
    AgentSessionMessageGap,
    AgentSessionMessagePage,
    apply_message_page,
)


SID = UUID("018f1240-0000-7000-8000-000000000001")
OTHER_SID = UUID("018f1240-0000-7000-8000-000000000002")
TURN1 = UUID("018f1240-0000-7000-8000-000000000003")
TURN2 = UUID("018f1240-0000-7000-8000-000000000004")
TURN3 = UUID("018f1240-0000-7000-8000-000000000005")


def message(sequence: int, turn_id: UUID, **changes: object) -> dict:
    item = {
        "turn_id": str(turn_id), "sequence": sequence, "status": "completed",
        "input_text": "hello", "output_text": "answer", "content_complete": True,
        "source": "user",
    }
    item.update(changes)
    return item


def page(messages: list[dict], next_cursor: int, snapshot_sequence: int,
         *, state_version: int = 3, has_more: bool = False) -> AgentSessionMessagePage:
    return AgentSessionMessagePage.model_validate({
        "messages": messages, "next_cursor": next_cursor,
        "snapshot_sequence": snapshot_sequence, "state_version": state_version,
        "has_more": has_more,
    })


def initial() -> AgentSessionMessageCursor:
    return AgentSessionMessageCursor(session_id=SID, sequence=0, turn_id=None, state_version=1)


def test_workflow_wire_shape_preserves_sparse_pages_and_missing_text():
    first = page([message(2, TURN1)], 2, 6, has_more=True)
    assert first.messages[0].turn_id == TURN1
    assert first.snapshot_sequence == 6
    incomplete = page([
        message(5, TURN2, input_text=None, output_text=None,
                content_complete=False, source="unknown", status="failed"),
    ], 5, 6, state_version=5)
    assert incomplete.messages[0].source == "unknown"
    assert incomplete.messages[0].content_complete is False
    assert incomplete.next_cursor != incomplete.snapshot_sequence
    conflict = AgentSessionMessageConflictResponse.model_validate({
        "detail": {"code": "SESSION_MESSAGE_LINK_INVALID", "current_sequence": 6, "state_version": 5},
    })
    assert conflict.detail.code == "SESSION_MESSAGE_LINK_INVALID"
    assert AgentSessionMessageConflictResponse.model_validate({
        "detail": {"code": "SESSION_CURSOR_AHEAD", "current_sequence": 6, "state_version": 5},
    }).detail.current_sequence == 6


def test_sparse_pagination_and_overlapping_retries_do_not_duplicate_messages():
    first = page([message(2, TURN1)], 2, 6, has_more=True)
    cursor, accepted = apply_message_page(initial(), first, session_id=SID, after_sequence=0)
    assert (cursor.sequence, cursor.turn_id) == (2, TURN1)
    assert [item.turn_id for item in accepted] == [TURN1]

    second = page([message(5, TURN2, source="subagent_report")], 5, 6, state_version=5)
    cursor, accepted = apply_message_page(cursor, second, session_id=SID, after_sequence=2)
    assert (cursor.sequence, cursor.turn_id) == (5, TURN2)
    assert [item.sequence for item in accepted] == [5]

    assert apply_message_page(cursor, first, session_id=SID, after_sequence=0) == (cursor, ())
    overlap = page([message(2, TURN1), message(5, TURN2)], 5, 6, state_version=5)
    assert apply_message_page(cursor, overlap, session_id=SID, after_sequence=0) == (cursor, ())
    empty = page([], 5, 7, state_version=6)
    cursor, accepted = apply_message_page(cursor, empty, session_id=SID, after_sequence=5)
    assert accepted == ()
    assert (cursor.sequence, cursor.state_version) == (5, 6)


@pytest.mark.parametrize("broken", [
    lambda: page([message(2, TURN1, content_complete=False)], 2, 6),
    lambda: page([message(2, TURN1, input_text=None, content_complete=False, source="user")], 2, 6),
    lambda: page([message(2, TURN1, secret="must not be exposed")], 2, 6),
    lambda: page([message(2, TURN1, status="running")], 2, 6),
    lambda: page([message(2, TURN1, output_text="x" * 262145)], 2, 6),
    lambda: page([message(5, TURN1), message(2, TURN2)], 2, 6),
    lambda: page([message(2, TURN1), message(5, TURN1)], 5, 6),
    lambda: page([message(2, TURN1)], 3, 6),
    lambda: page([message(2, TURN1)], 2, 1),
    lambda: page([], 0, 6, has_more=True),
    lambda: page([message(2, TURN1)], 2, 2, has_more=True),
    lambda: page([message(index + 1, UUID(int=index + 1)) for index in range(21)], 21, 25),
])
def test_wire_model_rejects_inconsistent_or_excessive_content(broken):
    with pytest.raises(ValidationError):
        broken()


def test_apply_rejects_foreign_session_cursor_changed_anchor_and_state_downgrade():
    cursor = AgentSessionMessageCursor(session_id=SID, sequence=2, turn_id=TURN1, state_version=3)
    valid = page([message(5, TURN2)], 5, 6, state_version=4)
    for session_id, after_sequence, response in (
        (OTHER_SID, 2, valid),
        (SID, True, valid),
        (SID, 3, valid),
        (SID, 0, valid),
        (SID, 0, page([message(2, TURN2), message(5, TURN3)], 5, 6, state_version=4)),
        (SID, 2, page([message(5, TURN2)], 5, 6, state_version=2)),
        (SID, 2, page([message(5, TURN1)], 5, 6, state_version=4)),
        (SID, 2, page([], 3, 6, state_version=4)),
    ):
        with pytest.raises(AgentSessionMessageGap):
            apply_message_page(cursor, response, session_id=session_id, after_sequence=after_sequence)
