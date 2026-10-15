"""Strict Agent Session turn-status parser and read-client tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from xgen_sdk.agent_session import (
    AgentSessionTurnStatus,
    AgentSessionTurnStatusClient,
    AgentSessionTurnStatusValidationError,
    parse_agent_session_turn_status,
)


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "agent-session-turn-status.json").read_text()
)
SESSION_ID = "11111111-1111-4111-8111-111111111111"
TURN_ID = "22222222-2222-4222-8222-222222222222"
ERROR = "Invalid canonical Agent Session turn status"
FIXTURE_FIELDS = {"name", "session_id", "turn_id", "response"}


def response(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "turn_id": TURN_ID,
        "agent_session_id": SESSION_ID,
        "status": "running",
        "progress": "running_or_unknown",
        "accepted_sequence": 10,
        "started_sequence": 11,
        "terminal_sequence": None,
        "result_available": False,
        "state_version": 4,
    }
    value.update(changes)
    return value


def test_public_fixture_has_a_stable_sanitized_cross_client_shape() -> None:
    assert set(FIXTURE) == {"valid", "invalid"}
    assert FIXTURE["valid"] and FIXTURE["invalid"]
    for case in (*FIXTURE["valid"], *FIXTURE["invalid"]):
        assert set(case) == FIXTURE_FIELDS
        serialized = json.dumps(case)
        assert "prompt" not in serialized
        assert "cipher" not in serialized


@pytest.mark.parametrize("case", FIXTURE["valid"], ids=lambda case: case["name"])
def test_public_fixture_valid_cases(case: dict[str, object]) -> None:
    parsed = parse_agent_session_turn_status(
        case["response"],
        expected_session_id=case["session_id"],
        expected_turn_id=case["turn_id"],
    )
    assert parsed.agent_session_id == UUID(case["session_id"])
    assert parsed.turn_id == UUID(case["turn_id"])


@pytest.mark.parametrize("case", FIXTURE["invalid"], ids=lambda case: case["name"])
def test_public_fixture_invalid_cases(case: dict[str, object]) -> None:
    with pytest.raises(AgentSessionTurnStatusValidationError, match=f"^{ERROR}$"):
        parse_agent_session_turn_status(
            case["response"],
            expected_session_id=case["session_id"],
            expected_turn_id=case["turn_id"],
        )


def test_dto_is_frozen_exact_and_round_trips_as_wire_json() -> None:
    status = AgentSessionTurnStatus.model_validate(response())
    assert status.model_dump(mode="json") == response()
    with pytest.raises(ValidationError):
        status.status = "completed"
    with pytest.raises(ValidationError):
        AgentSessionTurnStatus.model_validate(response(extra="rejected"))


def test_parser_revalidates_construct_copy_and_subclass_bypasses() -> None:
    status = AgentSessionTurnStatus.model_validate(response())
    parsed = parse_agent_session_turn_status(
        status, expected_session_id=SESSION_ID, expected_turn_id=TURN_ID
    )
    assert parsed == status
    assert parsed is not status

    forged = AgentSessionTurnStatus.model_construct(
        **response(accepted_sequence=True)
    )
    injected = status.model_copy(update={"private_token": "not-a-real-token"})
    inconsistent = status.model_copy(update={"started_sequence": 10})

    class StatusSubclass(AgentSessionTurnStatus):
        pass

    for unsafe in (forged, injected, inconsistent, StatusSubclass(**response())):
        with pytest.raises(AgentSessionTurnStatusValidationError):
            parse_agent_session_turn_status(
                unsafe, expected_session_id=SESSION_ID, expected_turn_id=TURN_ID
            )


@pytest.mark.parametrize(
    "session_id,turn_id",
    [
        (None, TURN_ID),
        ("00000000-0000-0000-0000-000000000000", TURN_ID),
        ("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", TURN_ID),
        (SESSION_ID, "not-a-uuid"),
    ],
)
def test_client_rejects_invalid_ids_before_transport(
    session_id: object, turn_id: object
) -> None:
    calls: list[str] = []

    async def get(path: str) -> object:
        calls.append(path)
        return response()

    async def scenario() -> None:
        with pytest.raises(AgentSessionTurnStatusValidationError):
            await AgentSessionTurnStatusClient(get).read(session_id, turn_id)  # type: ignore[arg-type]

    asyncio.run(scenario())
    assert calls == []


def test_client_uses_one_exact_queryless_get_path_and_binds_first_response() -> None:
    calls: list[str] = []

    async def get(path: str) -> object:
        calls.append(path)
        return response(turn_id="33333333-3333-4333-8333-333333333333")

    async def scenario() -> None:
        with pytest.raises(AgentSessionTurnStatusValidationError):
            await AgentSessionTurnStatusClient(get).read(
                UUID(SESSION_ID), UUID(TURN_ID)
            )

    asyncio.run(scenario())
    assert calls == [
        f"/api/agentflow/agent-sessions/{SESSION_ID}/turns/{TURN_ID}"
    ]


def test_client_returns_a_revalidated_status_from_the_caller_transport() -> None:
    async def get(path: str) -> object:
        assert "?" not in path
        return response()

    parsed = asyncio.run(AgentSessionTurnStatusClient(get).read(SESSION_ID, TURN_ID))
    assert parsed.status == "running"
    assert parsed.agent_session_id == UUID(SESSION_ID)
    assert parsed.turn_id == UUID(TURN_ID)


def test_client_does_not_retry_or_mutate_after_invalid_response() -> None:
    calls = 0

    async def get(path: str) -> object:
        nonlocal calls
        calls += 1
        assert path.endswith(f"/{TURN_ID}")
        return response(status="queued")

    async def scenario() -> None:
        with pytest.raises(AgentSessionTurnStatusValidationError):
            await AgentSessionTurnStatusClient(get).read(SESSION_ID, TURN_ID)

    asyncio.run(scenario())
    assert calls == 1


def test_client_propagates_cancellation_without_retry() -> None:
    calls = 0

    async def get(path: str) -> object:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await AgentSessionTurnStatusClient(get).read(SESSION_ID, TURN_ID)

    asyncio.run(scenario())
    assert calls == 1


def test_public_validation_errors_are_generic_and_hide_raw_values() -> None:
    unsafe = response(turn_id="private-secret", private_token="private-secret")
    with pytest.raises(AgentSessionTurnStatusValidationError) as caught:
        parse_agent_session_turn_status(
            unsafe,
            expected_session_id=SESSION_ID,
            expected_turn_id=TURN_ID,
        )
    assert str(caught.value) == ERROR
    assert "private" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert caught.value.__suppress_context__ is True


def test_direct_model_validation_hides_input_values() -> None:
    with pytest.raises(ValidationError) as caught:
        AgentSessionTurnStatus.model_validate(response(turn_id="private-secret"))
    rendered = str(caught.value)
    assert "private-secret" not in rendered
    assert "input_value" not in rendered
