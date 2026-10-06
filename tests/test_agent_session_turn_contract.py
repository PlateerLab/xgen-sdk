"""Strict canonical Agent Session turn submission contract tests."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from xgen_sdk.agent_session import (
    AgentAttachmentReference,
    AgentAttachmentValidationError,
    AgentSessionTurnValidationError,
    SubmitAgentSessionTurn,
    parse_attachment_references,
    parse_turn_submission,
)


ATTACHMENT_1 = "11111111-1111-4111-8111-111111111111"
ATTACHMENT_2 = "22222222-2222-4222-8222-222222222222"
DIGEST_1 = "a" * 64
DIGEST_2 = "b" * 64
ATTACHMENT_ERROR = "Invalid canonical Agent Session attachment metadata"
TURN_ERROR = "Invalid canonical Agent Session turn submission"


def reference(attachment_id: str = ATTACHMENT_1, sha256: str = DIGEST_1) -> dict[str, str]:
    return {"attachment_id": attachment_id, "sha256": sha256}


def submission(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "input_text": "hello",
        "expected_state_version": 1,
        "idempotency_key": "request-1",
    }
    value.update(changes)
    return value


def test_reference_parser_copies_preserves_order_and_rejects_duplicates_and_count() -> None:
    source = [reference(), reference(ATTACHMENT_2, DIGEST_2)]
    parsed = parse_attachment_references(source)
    assert isinstance(parsed, tuple)
    assert [item.attachment_id for item in parsed] == [ATTACHMENT_1, ATTACHMENT_2]
    source[0]["attachment_id"] = ATTACHMENT_2
    assert parsed[0].attachment_id == ATTACHMENT_1

    with pytest.raises(AgentAttachmentValidationError, match=f"^{ATTACHMENT_ERROR}$"):
        parse_attachment_references([reference(), reference(ATTACHMENT_1, DIGEST_2)])
    with pytest.raises(AgentAttachmentValidationError):
        parse_attachment_references([reference(f"{index:08x}-1111-4111-8111-111111111111") for index in range(11)])


@pytest.mark.parametrize("unsafe", [None, reference(), "value", iter([])])
def test_reference_parser_requires_an_exact_list_or_tuple(unsafe: object) -> None:
    with pytest.raises(AgentAttachmentValidationError):
        parse_attachment_references(unsafe)


def test_reference_parser_revalidates_forged_models_and_rejects_subclasses() -> None:
    forged = AgentAttachmentReference.model_construct(attachment_id=ATTACHMENT_1, sha256=True)
    with pytest.raises(AgentAttachmentValidationError):
        parse_attachment_references([forged])

    copied = AgentAttachmentReference(**reference()).model_copy(update={"private": "secret"})
    with pytest.raises(AgentAttachmentValidationError):
        parse_attachment_references([copied])

    class ReferenceSubclass(AgentAttachmentReference):
        pass

    with pytest.raises(AgentAttachmentValidationError):
        parse_attachment_references([ReferenceSubclass(**reference())])

    class ListSubclass(list):
        pass

    with pytest.raises(AgentAttachmentValidationError):
        parse_attachment_references(ListSubclass([reference()]))


def test_turn_shape_is_frozen_and_serializes_attachments_as_a_json_array() -> None:
    turn = SubmitAgentSessionTurn(**submission(attachments=[reference(), reference(ATTACHMENT_2, DIGEST_2)]))
    assert isinstance(turn.attachments, tuple)
    assert json.loads(turn.model_dump_json()) == {
        "input_text": "hello",
        "expected_state_version": 1,
        "idempotency_key": "request-1",
        "origin_id": None,
        "attachments": [reference(), reference(ATTACHMENT_2, DIGEST_2)],
    }
    with pytest.raises(ValidationError):
        turn.input_text = "changed"
    with pytest.raises(ValidationError):
        turn.attachments[0].sha256 = DIGEST_2


def test_turn_accepts_fastapi_list_and_empty_or_omitted_attachments() -> None:
    listed = SubmitAgentSessionTurn.model_validate(submission(attachments=[reference()]))
    empty = SubmitAgentSessionTurn.model_validate(submission(attachments=[]))
    omitted = SubmitAgentSessionTurn.model_validate(submission())
    assert listed.attachments == (AgentAttachmentReference(**reference()),)
    assert empty.attachments == omitted.attachments == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"input_text": ""},
        {"input_text": "x" * 262_145},
        {"input_text": 1},
        {"expected_state_version": 0},
        {"expected_state_version": True},
        {"expected_state_version": "1"},
        {"idempotency_key": ""},
        {"idempotency_key": "x" * 129},
        {"idempotency_key": 1},
        {"origin_id": ""},
        {"origin_id": "x" * 129},
        {"origin_id": 1},
        {"attachments": iter([reference()])},
        {"attachments": [reference(), reference()]},
        {"private_token": "private-secret"},
    ],
)
def test_turn_rejects_invalid_primitives_extras_and_attachment_shapes(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SubmitAgentSessionTurn.model_validate(submission(**changes))


def test_turn_parser_requires_exact_dto_and_revalidates_construct_and_copy_bypasses() -> None:
    turn = SubmitAgentSessionTurn(**submission(attachments=[reference()]))
    copied = parse_turn_submission(turn)
    assert copied == turn
    assert copied is not turn
    assert copied.attachments[0] is not turn.attachments[0]

    with pytest.raises(AgentSessionTurnValidationError):
        parse_turn_submission(submission())
    forged = SubmitAgentSessionTurn.model_construct(**submission(expected_state_version=True), attachments=())
    with pytest.raises(AgentSessionTurnValidationError):
        parse_turn_submission(forged)
    invalid_copy = turn.model_copy(update={"expected_state_version": True})
    with pytest.raises(AgentSessionTurnValidationError):
        parse_turn_submission(invalid_copy)
    injected = turn.model_copy(update={"private_token": "private-secret"})
    with pytest.raises(AgentSessionTurnValidationError):
        parse_turn_submission(injected)
    injected_reference = turn.attachments[0].model_copy(update={"private_token": "private-secret"})
    forged_nested = turn.model_copy(update={"attachments": (injected_reference,)})
    with pytest.raises(AgentSessionTurnValidationError):
        parse_turn_submission(forged_nested)

    class TurnSubclass(SubmitAgentSessionTurn):
        pass

    with pytest.raises(AgentSessionTurnValidationError):
        parse_turn_submission(TurnSubclass(**submission()))


@pytest.mark.parametrize(
    "operation,error_type,message",
    [
        (
            lambda: parse_attachment_references([reference(sha256="private-secret")]),
            AgentAttachmentValidationError,
            ATTACHMENT_ERROR,
        ),
        (
            lambda: parse_turn_submission(
                SubmitAgentSessionTurn.model_construct(
                    **submission(input_text="private-secret", expected_state_version=True),
                    attachments=(),
                )
            ),
            AgentSessionTurnValidationError,
            TURN_ERROR,
        ),
    ],
)
def test_public_helper_errors_are_generic_without_raw_input_or_context(operation, error_type, message) -> None:
    with pytest.raises(error_type) as caught:
        operation()
    assert str(caught.value) == message
    assert "private" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert caught.value.__suppress_context__ is True


def test_direct_model_validation_hides_input_values() -> None:
    with pytest.raises(ValidationError) as caught:
        SubmitAgentSessionTurn.model_validate(submission(input_text="private-secret", expected_state_version=True))
    rendered = str(caught.value)
    assert "private-secret" not in rendered
    assert "input_value" not in rendered
