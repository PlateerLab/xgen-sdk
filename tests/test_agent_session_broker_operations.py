"""Strict Agent Session broker-operation parser and client tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import traceback
from uuid import UUID

import pytest
from pydantic import ValidationError

from xgen_sdk.agent_session import (
    AGENT_SESSION_BROKER_OPERATION_MAX_COUNT,
    AGENT_SESSION_BROKER_OPERATION_MAX_EXPIRY,
    AGENT_SESSION_BROKER_OPERATIONS,
    AgentSessionBrokerOperationCancellation,
    AgentSessionBrokerOperationMetadata,
    AgentSessionBrokerOperationReference,
    AgentSessionBrokerOperationValidationError,
    AgentSessionBrokerOperationsClient,
    parse_agent_session_broker_operation_cancellation,
    parse_agent_session_broker_operation_metadata,
    parse_agent_session_broker_operation_references,
)


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "agent-session-broker-operations.json"
)
FIXTURE_BYTES = FIXTURE_PATH.read_bytes()
FIXTURE = json.loads(FIXTURE_BYTES)
SESSION_ID = "11111111-1111-4111-8111-111111111111"
TURN_ID = "22222222-2222-4222-8222-222222222222"
INVOCATION_ID = "77777777-7777-4777-8777-777777777777"
ERROR = "Invalid canonical Agent Session broker operation"
FIXTURE_SHA256 = "bce5c59f4a1d4d4f3e579d94576c7fdd8847cfaaa79e9ce5e4af713c396a3c29"


def reference(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "invocation_id": INVOCATION_ID,
        "operation_id": "workflow.code.execute",
    }
    value.update(changes)
    return value


def metadata(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        **reference(),
        "state": "completed",
        "cancel_requested": False,
        "target_cleanup": "complete",
        "result_available": True,
        "expires_at": 1_770_000_030,
    }
    value.update(changes)
    return value


def parsed_reference() -> AgentSessionBrokerOperationReference:
    return AgentSessionBrokerOperationReference.model_validate(reference())


def test_public_fixture_is_byte_stable_exact_and_secret_free() -> None:
    assert hashlib.sha256(FIXTURE_BYTES).hexdigest() == FIXTURE_SHA256
    assert set(FIXTURE) == {"valid", "invalid"}
    for group in FIXTURE.values():
        assert set(group) == {"identifiers", "lists", "metadata", "cancellations"}
    assert set(FIXTURE["valid"]["identifiers"][0]) == {
        "name",
        "session_id",
        "turn_id",
    }
    assert all(
        set(case) == {"name", "field", "value"}
        for case in FIXTURE["invalid"]["identifiers"]
    )
    assert all(
        set(case) == {"name", "response"}
        for validity in FIXTURE.values()
        for case in validity["lists"]
    )
    assert all(
        set(case) == {"name", "reference", "response"}
        for validity in FIXTURE.values()
        for category in ("metadata", "cancellations")
        for case in validity[category]
    )
    serialized = FIXTURE_BYTES.decode()
    for forbidden in ("grant", "lease", "token", "password", "cipher", "body"):
        assert forbidden not in serialized.lower()


def test_public_fixture_identifiers_match_canonical_request_context() -> None:
    case = FIXTURE["valid"]["identifiers"][0]
    assert case["session_id"] == SESSION_ID
    assert case["turn_id"] == TURN_ID


@pytest.mark.parametrize(
    "case", FIXTURE["invalid"]["identifiers"], ids=lambda c: c["name"]
)
def test_public_fixture_invalid_identifiers_fail_before_transport(
    case: dict[str, object],
) -> None:
    calls: list[str] = []

    async def transport(path: str) -> object:
        calls.append(path)
        return []

    async def scenario() -> None:
        client = AgentSessionBrokerOperationsClient(transport, transport)
        field = case["field"]
        value = case["value"]
        with pytest.raises(AgentSessionBrokerOperationValidationError):
            if field == "session_id":
                await client.list(value, TURN_ID)  # type: ignore[arg-type]
            elif field == "turn_id":
                await client.list(SESSION_ID, value)  # type: ignore[arg-type]
            else:
                forged = AgentSessionBrokerOperationReference.model_construct(
                    **reference(invocation_id=value)
                )
                await client.read(SESSION_ID, TURN_ID, forged)

    asyncio.run(scenario())
    assert calls == []


@pytest.mark.parametrize("case", FIXTURE["valid"]["lists"], ids=lambda c: c["name"])
def test_public_fixture_valid_lists(case: dict[str, object]) -> None:
    parsed = parse_agent_session_broker_operation_references(case["response"])
    assert len(parsed) == len(case["response"])  # type: ignore[arg-type]
    assert len({item.invocation_id for item in parsed}) == len(parsed)


@pytest.mark.parametrize("case", FIXTURE["invalid"]["lists"], ids=lambda c: c["name"])
def test_public_fixture_invalid_lists(case: dict[str, object]) -> None:
    with pytest.raises(AgentSessionBrokerOperationValidationError, match=f"^{ERROR}$"):
        parse_agent_session_broker_operation_references(case["response"])


@pytest.mark.parametrize(
    "case", FIXTURE["valid"]["metadata"], ids=lambda c: c["name"]
)
def test_public_fixture_valid_metadata(case: dict[str, object]) -> None:
    expected = AgentSessionBrokerOperationReference.model_validate(case["reference"])
    parsed = parse_agent_session_broker_operation_metadata(
        case["response"], expected_reference=expected
    )
    assert parsed.invocation_id == expected.invocation_id
    assert parsed.operation_id == expected.operation_id


@pytest.mark.parametrize(
    "case", FIXTURE["invalid"]["metadata"], ids=lambda c: c["name"]
)
def test_public_fixture_invalid_metadata(case: dict[str, object]) -> None:
    expected = AgentSessionBrokerOperationReference.model_validate(case["reference"])
    with pytest.raises(AgentSessionBrokerOperationValidationError, match=f"^{ERROR}$"):
        parse_agent_session_broker_operation_metadata(
            case["response"], expected_reference=expected
        )


@pytest.mark.parametrize(
    "case", FIXTURE["valid"]["cancellations"], ids=lambda c: c["name"]
)
def test_public_fixture_valid_cancellations(case: dict[str, object]) -> None:
    expected = AgentSessionBrokerOperationReference.model_validate(case["reference"])
    parsed = parse_agent_session_broker_operation_cancellation(
        case["response"], expected_reference=expected
    )
    assert parsed.invocation_id == expected.invocation_id
    assert parsed.cancel_requested is True


@pytest.mark.parametrize(
    "case", FIXTURE["invalid"]["cancellations"], ids=lambda c: c["name"]
)
def test_public_fixture_invalid_cancellations(case: dict[str, object]) -> None:
    expected = AgentSessionBrokerOperationReference.model_validate(case["reference"])
    with pytest.raises(AgentSessionBrokerOperationValidationError, match=f"^{ERROR}$"):
        parse_agent_session_broker_operation_cancellation(
            case["response"], expected_reference=expected
        )


def test_constants_match_server_bounds_and_allowlist() -> None:
    assert AGENT_SESSION_BROKER_OPERATION_MAX_COUNT == 128
    assert AGENT_SESSION_BROKER_OPERATION_MAX_EXPIRY == 9_007_199_254_740_991
    assert AGENT_SESSION_BROKER_OPERATIONS == {
        "documents.collections.list",
        "documents.collection.documents.list",
        "workflow.actor.authorize",
        "documents.attachment.index",
        "workflow.code.execute",
        "workflow.attachments.stage",
    }


def test_dtos_are_frozen_exact_and_round_trip_as_public_wire_json() -> None:
    ref = parsed_reference()
    observed = AgentSessionBrokerOperationMetadata.model_validate(metadata())
    accepted = AgentSessionBrokerOperationCancellation.model_validate(
        {"invocation_id": INVOCATION_ID, "cancel_requested": True}
    )
    assert ref.model_dump(mode="json") == reference()
    assert observed.model_dump(mode="json") == metadata()
    assert accepted.model_dump(mode="json") == {
        "invocation_id": INVOCATION_ID,
        "cancel_requested": True,
    }
    for value in (ref, observed, accepted):
        with pytest.raises(ValidationError):
            value.invocation_id = UUID(SESSION_ID)
    with pytest.raises(ValidationError):
        AgentSessionBrokerOperationMetadata.model_validate(metadata(result={}))


def test_parsers_revalidate_construct_copy_and_subclass_bypasses() -> None:
    ref = parsed_reference()
    parsed_refs = parse_agent_session_broker_operation_references([ref])
    assert parsed_refs == (ref,)
    assert parsed_refs[0] is not ref

    forged_ref = AgentSessionBrokerOperationReference.model_construct(
        **reference(operation_id="documents.private.read")
    )
    injected_ref = ref.model_copy(update={"grant": "not-a-real-grant"})

    class ReferenceSubclass(AgentSessionBrokerOperationReference):
        pass

    for unsafe in (forged_ref, injected_ref, ReferenceSubclass(**reference())):
        with pytest.raises(AgentSessionBrokerOperationValidationError):
            parse_agent_session_broker_operation_references([unsafe])

    observed = AgentSessionBrokerOperationMetadata.model_validate(metadata())
    forged_metadata = AgentSessionBrokerOperationMetadata.model_construct(
        **metadata(expires_at=True)
    )
    injected_metadata = observed.model_copy(update={"result": {"private": "value"}})
    inconsistent_metadata = observed.model_copy(update={"state": "running"})

    class MetadataSubclass(AgentSessionBrokerOperationMetadata):
        pass

    for unsafe in (
        forged_metadata,
        injected_metadata,
        inconsistent_metadata,
        MetadataSubclass(**metadata()),
    ):
        with pytest.raises(AgentSessionBrokerOperationValidationError):
            parse_agent_session_broker_operation_metadata(
                unsafe, expected_reference=ref
            )

    cancellation = AgentSessionBrokerOperationCancellation.model_validate(
        {"invocation_id": INVOCATION_ID, "cancel_requested": True}
    )
    forged_cancellation = cancellation.model_copy(update={"cancel_requested": False})
    with pytest.raises(AgentSessionBrokerOperationValidationError):
        parse_agent_session_broker_operation_cancellation(
            forged_cancellation, expected_reference=ref
        )


@pytest.mark.parametrize(
    "session_id,turn_id",
    [
        (None, TURN_ID),
        ("00000000-0000-0000-0000-000000000000", TURN_ID),
        ("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", TURN_ID),
        (SESSION_ID, "not-a-uuid"),
        (True, TURN_ID),
    ],
)
def test_client_rejects_invalid_session_and_turn_ids_before_transport(
    session_id: object, turn_id: object
) -> None:
    calls: list[str] = []

    async def get(path: str) -> object:
        calls.append(path)
        return []

    async def post(path: str) -> object:
        calls.append(path)
        return {}

    async def scenario() -> None:
        with pytest.raises(AgentSessionBrokerOperationValidationError):
            await AgentSessionBrokerOperationsClient(get, post).list(  # type: ignore[arg-type]
                session_id, turn_id
            )

    asyncio.run(scenario())
    assert calls == []


@pytest.mark.parametrize("method", ["read", "cancel"])
def test_client_revalidates_reference_before_transport_with_secret_free_error(
    method: str,
) -> None:
    calls: list[str] = []

    async def transport(path: str) -> object:
        calls.append(path)
        return {}

    forged = AgentSessionBrokerOperationReference.model_construct(
        **reference(invocation_id="private-canary")
    )

    async def scenario() -> None:
        client = AgentSessionBrokerOperationsClient(transport, transport)
        with pytest.raises(AgentSessionBrokerOperationValidationError) as caught:
            if method == "read":
                await client.read(SESSION_ID, TURN_ID, forged)
            else:
                await client.cancel(SESSION_ID, TURN_ID, forged)
        assert str(caught.value) == ERROR
        assert "canary" not in repr(caught.value)
        assert "canary" not in "".join(traceback.format_exception(caught.value))
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert caught.value.__suppress_context__ is True

    asyncio.run(scenario())
    assert calls == []


def test_client_uses_exact_queryless_paths_and_empty_post_signature() -> None:
    calls: list[tuple[str, str]] = []

    async def get(path: str) -> object:
        calls.append(("GET", path))
        if path.endswith("/operations"):
            return [reference()]
        return metadata()

    async def post(path: str) -> object:
        calls.append(("POST", path))
        return {"invocation_id": INVOCATION_ID, "cancel_requested": True}

    async def scenario() -> None:
        client = AgentSessionBrokerOperationsClient(get, post)
        listed = await client.list(UUID(SESSION_ID), UUID(TURN_ID))
        assert listed == (parsed_reference(),)
        observed = await client.read(SESSION_ID, TURN_ID, listed[0])
        assert observed.result_available is True
        cancelled = await client.cancel(SESSION_ID, TURN_ID, listed[0])
        assert cancelled.cancel_requested is True

    asyncio.run(scenario())
    base = f"/api/agentflow/agent-sessions/{SESSION_ID}/turns/{TURN_ID}/operations"
    assert calls == [
        ("GET", base),
        ("GET", f"{base}/{INVOCATION_ID}"),
        ("POST", f"{base}/{INVOCATION_ID}/cancel"),
    ]
    assert all("?" not in path for _, path in calls)


def test_read_binds_both_invocation_and_expected_operation() -> None:
    calls = 0

    async def get(path: str) -> object:
        nonlocal calls
        calls += 1
        assert path.endswith(f"/{INVOCATION_ID}")
        return metadata(operation_id="workflow.attachments.stage")

    async def post(path: str) -> object:
        raise AssertionError(path)

    async def scenario() -> None:
        with pytest.raises(AgentSessionBrokerOperationValidationError):
            await AgentSessionBrokerOperationsClient(get, post).read(
                SESSION_ID, TURN_ID, parsed_reference()
            )

    asyncio.run(scenario())
    assert calls == 1


@pytest.mark.parametrize("method", ["list", "read", "cancel"])
def test_client_does_not_retry_invalid_responses(method: str) -> None:
    calls = 0

    async def get(path: str) -> object:
        nonlocal calls
        calls += 1
        return None

    async def post(path: str) -> object:
        nonlocal calls
        calls += 1
        return None

    async def scenario() -> None:
        client = AgentSessionBrokerOperationsClient(get, post)
        with pytest.raises(AgentSessionBrokerOperationValidationError):
            if method == "list":
                await client.list(SESSION_ID, TURN_ID)
            elif method == "read":
                await client.read(SESSION_ID, TURN_ID, parsed_reference())
            else:
                await client.cancel(SESSION_ID, TURN_ID, parsed_reference())

    asyncio.run(scenario())
    assert calls == 1


@pytest.mark.parametrize("method", ["list", "read", "cancel"])
def test_client_propagates_cancellation_without_retry(method: str) -> None:
    calls = 0

    async def get(path: str) -> object:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    async def post(path: str) -> object:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    async def scenario() -> None:
        client = AgentSessionBrokerOperationsClient(get, post)
        with pytest.raises(asyncio.CancelledError):
            if method == "list":
                await client.list(SESSION_ID, TURN_ID)
            elif method == "read":
                await client.read(SESSION_ID, TURN_ID, parsed_reference())
            else:
                await client.cancel(SESSION_ID, TURN_ID, parsed_reference())

    asyncio.run(scenario())
    assert calls == 1


def test_client_propagates_transport_errors_unchanged() -> None:
    failure = RuntimeError("transport-owned failure")

    async def get(path: str) -> object:
        raise failure

    async def post(path: str) -> object:
        raise AssertionError(path)

    async def scenario() -> None:
        with pytest.raises(RuntimeError) as caught:
            await AgentSessionBrokerOperationsClient(get, post).list(
                SESSION_ID, TURN_ID
            )
        assert caught.value is failure

    asyncio.run(scenario())


def test_public_validation_errors_are_fixed_and_hide_raw_values() -> None:
    unsafe = metadata(
        invocation_id="private-secret",
        result={"private": "private-secret"},
    )
    with pytest.raises(AgentSessionBrokerOperationValidationError) as caught:
        parse_agent_session_broker_operation_metadata(
            unsafe, expected_reference=parsed_reference()
        )
    assert str(caught.value) == ERROR
    assert "private" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert caught.value.__suppress_context__ is True


def test_direct_model_validation_hides_input_values() -> None:
    with pytest.raises(ValidationError) as caught:
        AgentSessionBrokerOperationMetadata.model_validate(
            metadata(invocation_id="private-secret")
        )
    rendered = str(caught.value)
    assert "private-secret" not in rendered
    assert "input_value" not in rendered


def test_client_requires_callable_transports() -> None:
    async def transport(path: str) -> object:
        return path

    with pytest.raises(TypeError, match="get transport must be callable"):
        AgentSessionBrokerOperationsClient(None, transport)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="post transport must be callable"):
        AgentSessionBrokerOperationsClient(transport, None)  # type: ignore[arg-type]
