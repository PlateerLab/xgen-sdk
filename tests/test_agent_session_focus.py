"""Workflow account focus response fixtures and replay safety checks."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from xgen_sdk.agent_session import (
    AccountEventPage,
    AgentSessionCreated,
    CreateAgentSession,
    CursorConflictResponse,
    FocusConflictResponse,
    FocusReplayGap,
    FocusState,
    SwitchFocus,
    apply_event_page,
)


SID = "018f1240-0000-7000-8000-000000000001"
EVENT1 = "018f1240-0000-7000-8000-000000000002"
EVENT2 = "018f1240-0000-7000-8000-000000000003"


def event(sequence, event_id, previous, active):
    return {
        "event_id": event_id,
        "sequence": sequence,
        "event_type": "agent_session.focus_changed",
        "previous_agent_session_id": previous,
        "active_agent_session_id": active,
        "origin_id": "web-tab",
        "created_at": "2026-09-27T12:00:00+00:00",
    }


def page(events, next_cursor, snapshot_version, has_more=False):
    return AccountEventPage.model_validate({
        "events": events,
        "next_cursor": next_cursor,
        "snapshot_version": snapshot_version,
        "has_more": has_more,
    })


def test_workflow_create_focus_and_conflict_contract():
    request = CreateAgentSession(workflow_id="owned", expected_version=0)
    assert request.model_dump(mode="json") == {
        "workflow_id": "owned", "expected_version": 0, "title": "", "origin_id": None,
    }
    created = AgentSessionCreated.model_validate({
        "id": SID, "workflow_id": "owned",
        "focus": {"active_agent_session_id": SID, "version": 1, "event_id": EVENT1},
    })
    assert created.id == UUID(SID)
    assert created.focus.active_agent_session_id == created.id
    with pytest.raises(ValidationError):
        AgentSessionCreated.model_validate({
            "id": EVENT2, "workflow_id": "owned",
            "focus": created.focus.model_dump(mode="json"),
        })
    assert SwitchFocus(active_agent_session_id=None, expected_version=1).model_dump(mode="json") == {
        "active_agent_session_id": None, "expected_version": 1, "origin_id": None,
    }
    conflict = FocusConflictResponse.model_validate({
        "detail": {"code": "FOCUS_VERSION_CONFLICT", "current": created.focus.model_dump(mode="json")},
    })
    assert conflict.detail.current.version == 1
    cursor_conflict = CursorConflictResponse.model_validate({
        "detail": {"code": "ACCOUNT_CURSOR_GAP", "current": created.focus.model_dump(mode="json")},
    })
    assert cursor_conflict.detail.current == created.focus


def test_replay_pages_resume_and_ignore_identical_retry():
    initial = FocusState(active_agent_session_id=None, version=0, event_id=None)
    first = page([event(1, EVENT1, None, SID)], 1, 2, True)
    focused = apply_event_page(initial, first, after_sequence=0)
    assert focused.active_agent_session_id == UUID(SID)
    assert focused.version == 1
    assert apply_event_page(focused, first, after_sequence=0) == focused

    second = page([event(2, EVENT2, SID, None)], 2, 2)
    cleared = apply_event_page(focused, second, after_sequence=1)
    assert cleared.active_agent_session_id is None
    assert cleared.version == 2
    assert apply_event_page(cleared, page([], 2, 2), after_sequence=2) == cleared


@pytest.mark.parametrize("broken", [
    lambda: page([event(2, EVENT1, None, SID)], 2, 2),
    lambda: page([event(1, EVENT1, SID, SID)], 1, 1),
    lambda: page([event(1, EVENT1, None, SID)], 0, 1),
    lambda: page([event(1, EVENT1, None, SID)], 1, 1, True),
])
def test_replay_rejects_gap_pointer_or_cursor_inconsistency(broken):
    initial = FocusState(active_agent_session_id=None, version=0, event_id=None)
    with pytest.raises(FocusReplayGap):
        apply_event_page(initial, broken(), after_sequence=0)


def test_replay_rejects_changed_duplicate_and_bad_models():
    initial = FocusState(active_agent_session_id=UUID(SID), version=1, event_id=UUID(EVENT1))
    changed = page([event(1, EVENT2, None, SID)], 1, 1)
    with pytest.raises(FocusReplayGap):
        apply_event_page(initial, changed, after_sequence=0)
    # GET /me/agent-state has no historical event_id; its pointer is authoritative.
    snapshot = FocusState(active_agent_session_id=UUID(SID), version=1, event_id=None)
    assert apply_event_page(snapshot, changed, after_sequence=0) == snapshot
    with pytest.raises(ValidationError):
        CreateAgentSession(workflow_id="owned", expected_version=True)
    with pytest.raises(ValidationError):
        page([dict(event(1, EVENT1, None, SID), event_type="turn.started")], 1, 1)
