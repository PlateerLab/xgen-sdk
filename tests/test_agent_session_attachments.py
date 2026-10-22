from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from xgen_sdk.agent_session import (
    AGENT_ATTACHMENT_MAX_BYTES,
    AGENT_ATTACHMENT_MAX_COUNT,
    AGENT_ATTACHMENT_MAX_TOTAL_BYTES,
    AgentAttachmentReceipt,
    AgentAttachmentReference,
    AgentAttachmentScope,
    AgentAttachmentValidationError,
    parse_attachment_receipt,
    parse_attachment_scope,
    prepare_attachment_references,
)


FIXTURE = Path(__file__).parent / "fixtures" / "agent-session-attachments.json"
SESSION_ID = "11111111-1111-4111-8111-111111111111"
ATTACHMENT_ID = "22222222-2222-4222-8222-222222222222"
DIGEST = "a" * 64
ERROR = "Invalid canonical Agent Session attachment metadata"


def scope(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "origin": "https://xgen.example.com",
        "user_id": "7",
        "session_id": SESSION_ID,
        "workflow_id": "workflow-한글",
    }
    value.update(changes)
    return value


def receipt(index: int = 0, **changes: object) -> dict[str, object]:
    value = {
        **scope(),
        "attachment_id": f"{index:08x}-1111-4111-8111-111111111111",
        "filename": "자료.txt",
        "size_bytes": 1,
        "media_type": "text/plain",
        "sha256": DIGEST,
    }
    value.update(changes)
    return value


def load_cases() -> list[dict[str, object]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["version"] == 1
    return data["cases"]


@pytest.mark.parametrize("case", load_cases(), ids=lambda case: str(case["name"]))
def test_shared_cross_language_vectors(case: dict[str, object]) -> None:
    operation = lambda: prepare_attachment_references(case["receipts"], case["scope"])
    if case["valid"]:
        references = operation()
        assert [item.model_dump() for item in references] == case["references"]
    else:
        with pytest.raises(AgentAttachmentValidationError, match=f"^{ERROR}$"):
            operation()


def test_scope_and_receipt_helpers_copy_and_match_all_scope_fields() -> None:
    raw_scope = scope()
    parsed_scope = parse_attachment_scope(raw_scope)
    copied_scope = parse_attachment_scope(parsed_scope)
    assert parsed_scope == copied_scope
    assert parsed_scope is not copied_scope

    raw_receipt = receipt(2)
    parsed_receipt = parse_attachment_receipt(raw_receipt, parsed_scope)
    copied_receipt = parse_attachment_receipt(parsed_receipt, raw_scope)
    assert parsed_receipt == copied_receipt
    assert parsed_receipt is not copied_receipt

    raw_scope["workflow_id"] = "mutated"
    raw_receipt["filename"] = "mutated.txt"
    assert parsed_scope.workflow_id == "workflow-한글"
    assert parsed_receipt.filename == "자료.txt"

    for field, different in {
        "origin": "https://other.example.com",
        "user_id": "8",
        "session_id": "33333333-3333-4333-8333-333333333333",
        "workflow_id": "other",
    }.items():
        with pytest.raises(AgentAttachmentValidationError):
            parse_attachment_receipt(receipt(2, **{field: different}), scope())


def test_models_are_frozen_exact_and_constructed_models_are_fully_revalidated() -> None:
    parsed_scope = parse_attachment_scope(scope())
    parsed_receipt = parse_attachment_receipt(receipt(2), parsed_scope)
    reference = AgentAttachmentReference(attachment_id=ATTACHMENT_ID, sha256=DIGEST)
    for model, field in [
        (parsed_scope, "workflow_id"),
        (parsed_receipt, "filename"),
        (reference, "sha256"),
    ]:
        with pytest.raises(ValidationError):
            setattr(model, field, "mutated")

    with pytest.raises(ValidationError):
        AgentAttachmentReference.model_validate(
            {"attachment_id": ATTACHMENT_ID, "sha256": DIGEST, "url": "https://untrusted.example"}
        )

    forged_scope = AgentAttachmentScope.model_construct(**scope(origin="http://xgen.example.com"))
    with pytest.raises(AgentAttachmentValidationError):
        parse_attachment_scope(forged_scope)
    forged_receipt = AgentAttachmentReceipt.model_construct(**receipt(2, size_bytes=True))
    with pytest.raises(AgentAttachmentValidationError):
        parse_attachment_receipt(forged_receipt, scope())
    with pytest.raises(AgentAttachmentValidationError):
        prepare_attachment_references([forged_receipt], scope())

    with pytest.raises(AgentAttachmentValidationError, match=f"^{ERROR}$"):
        AgentAttachmentScope.model_construct(**scope(), tenant_id="private-secret")

    injected = AgentAttachmentReceipt.model_construct(**receipt(2))
    injected.__dict__["url"] = "https://private-secret.example"
    with pytest.raises(AgentAttachmentValidationError, match=f"^{ERROR}$"):
        parse_attachment_receipt(injected, scope())

    injected_extra = AgentAttachmentReceipt.model_construct(**receipt(2))
    object.__setattr__(injected_extra, "__pydantic_extra__", {"url": "private-secret"})
    with pytest.raises(AgentAttachmentValidationError, match=f"^{ERROR}$"):
        prepare_attachment_references([injected_extra], scope())

    class ScopeSubclass(AgentAttachmentScope):
        def model_dump(self, *args, **kwargs):
            return scope()

    with pytest.raises(AgentAttachmentValidationError, match=f"^{ERROR}$"):
        parse_attachment_scope(ScopeSubclass(**scope()))


def test_references_preserve_order_are_immutable_and_do_not_alias_receipts() -> None:
    first = receipt(2, sha256="a" * 64)
    second = receipt(3, sha256="b" * 64)
    references = prepare_attachment_references([first, second], scope())
    assert isinstance(references, tuple)
    assert [item.attachment_id for item in references] == [
        first["attachment_id"],
        second["attachment_id"],
    ]
    first["attachment_id"] = "44444444-4444-4444-8444-444444444444"
    first["sha256"] = "c" * 64
    assert references[0].attachment_id == "00000002-1111-4111-8111-111111111111"
    assert references[0].sha256 == "a" * 64
    with pytest.raises(ValidationError):
        references[0].sha256 = "d" * 64


def test_count_size_and_aggregate_boundaries() -> None:
    maximum = receipt(0, size_bytes=AGENT_ATTACHMENT_MAX_BYTES)
    zero = receipt(1, size_bytes=0)
    references = prepare_attachment_references([maximum, zero], scope())
    assert len(references) == 2
    assert AGENT_ATTACHMENT_MAX_BYTES == AGENT_ATTACHMENT_MAX_TOTAL_BYTES == 104_857_600

    ten = [receipt(index) for index in range(AGENT_ATTACHMENT_MAX_COUNT)]
    assert len(prepare_attachment_references(ten, scope())) == 10
    with pytest.raises(AgentAttachmentValidationError):
        prepare_attachment_references([*ten, receipt(10)], scope())
    with pytest.raises(AgentAttachmentValidationError):
        prepare_attachment_references(
            [maximum, receipt(1, size_bytes=1)],
            scope(),
        )
    with pytest.raises(AgentAttachmentValidationError):
        parse_attachment_receipt(receipt(0, size_bytes=AGENT_ATTACHMENT_MAX_BYTES + 1), scope())


def test_utf8_byte_boundaries_mime_boundaries_and_strict_primitives() -> None:
    exact_workflow = "é" * 64
    assert len(exact_workflow.encode()) == 128
    assert parse_attachment_scope(scope(workflow_id=exact_workflow)).workflow_id == exact_workflow
    with pytest.raises(AgentAttachmentValidationError):
        parse_attachment_scope(scope(workflow_id=exact_workflow + "é"))

    exact_filename = "é" * 127 + "a"
    assert len(exact_filename.encode()) == 255
    assert parse_attachment_receipt(receipt(2, filename=exact_filename), scope()).filename == exact_filename
    with pytest.raises(AgentAttachmentValidationError):
        parse_attachment_receipt(receipt(2, filename=exact_filename + "a"), scope())

    exact_mime = f"{'a' * 127}/{'b' * 127}"
    assert len(exact_mime) == 255
    assert parse_attachment_receipt(receipt(2, media_type=exact_mime), scope()).media_type == exact_mime
    with pytest.raises(AgentAttachmentValidationError):
        parse_attachment_receipt(receipt(2, media_type=f"{'a' * 128}/b"), scope())

    decomposed = "e\u0301"
    decomposed_scope = scope(workflow_id=f"workflow-{decomposed}")
    decomposed_receipt = receipt(2, workflow_id=f"workflow-{decomposed}", filename=f"{decomposed}.txt")
    parsed = parse_attachment_receipt(decomposed_receipt, decomposed_scope)
    assert parsed.workflow_id == f"workflow-{decomposed}"
    assert parsed.filename == f"{decomposed}.txt"
    assert prepare_attachment_references([parsed], decomposed_scope)[0].attachment_id == parsed.attachment_id
    with pytest.raises(AgentAttachmentValidationError):
        parse_attachment_receipt(decomposed_receipt, scope(workflow_id="workflow-é"))

    for field, value in [
        ("origin", 7),
        ("user_id", 7),
        ("session_id", 7),
        ("workflow_id", 7),
    ]:
        with pytest.raises(AgentAttachmentValidationError):
            parse_attachment_scope(scope(**{field: value}))
    for field, value in [
        ("attachment_id", 7),
        ("filename", 7),
        ("size_bytes", True),
        ("size_bytes", "1"),
        ("media_type", 7),
        ("sha256", 7),
    ]:
        with pytest.raises(AgentAttachmentValidationError):
            parse_attachment_receipt(receipt(2, **{field: value}), scope())


@pytest.mark.parametrize(
    "origin",
    [
        "https://xgen.example.com",
        "https://localhost:3443",
        "https://127.0.0.1:3443",
        "https://[::1]:3443",
        "https://[2001:db8::1]",
        "https://[::ffff:c000:201]",
        "https://[2001::1:0:0:1:1]",
        "https://foo.0xffg",
        "https://" + ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61]),
    ],
)
def test_canonical_origins(origin: str) -> None:
    assert parse_attachment_scope(scope(origin=origin)).origin == origin


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.1",
        "https://127.0.0.01",
        "https://2130706433",
        "https://0x7f.1",
        "https://foo.123",
        "https://foo.0x1",
        "https://foo.0x",
        "https://0x",
        "https://[2001:0db8::1]",
        "https://[::ffff:192.0.2.128]",
        "https://[::ffff:192.0.2.1]",
        "https://[2001:0:0:1:0:0:1:1]",
        "https://[2001:0:0:1::1:1]",
        "https://localhost:03443",
        "https://localhost:443",
        "https://localhost:0",
        "https://localhost:65536",
        "https://a_b.example",
        "https://-a.example",
        "https://example.com.",
        "https://bücher.example",
        "https://example.com/",
        "https://example.com?x=1",
        "https://example.com#x",
        "https://example.com\\path",
        "https://EXAMPLE.com",
        "https://xn--bcher-kva.example",
        "https://example.xn--p1ai",
        "https://xn--invalid-.example",
        "https://a" + ".".join(["a" * 63] * 4),
        "https://" + "a" * 64 + ".example",
    ],
)
def test_noncanonical_origins(origin: str) -> None:
    with pytest.raises(AgentAttachmentValidationError):
        parse_attachment_scope(scope(origin=origin))


def test_context_extras_non_sequences_duplicates_and_aggregate_are_rejected() -> None:
    with pytest.raises(AgentAttachmentValidationError):
        prepare_attachment_references([], scope(tenant_id="private"))
    with pytest.raises(AgentAttachmentValidationError):
        prepare_attachment_references((item for item in [receipt(1)]), scope())
    duplicate = receipt(2)
    with pytest.raises(AgentAttachmentValidationError):
        prepare_attachment_references([duplicate, {**duplicate, "sha256": "b" * 64}], scope())


@pytest.mark.parametrize(
    "operation",
    [
        lambda: parse_attachment_scope(scope(workflow_id="private/secret")),
        lambda: parse_attachment_receipt(receipt(2, filename="private/secret"), scope()),
        lambda: prepare_attachment_references(
            [receipt(2, filename="private/secret", private_token="private-secret-token")],
            scope(),
        ),
    ],
)
def test_public_errors_are_generic_and_do_not_expose_raw_content(operation) -> None:
    with pytest.raises(AgentAttachmentValidationError) as caught:
        operation()
    assert str(caught.value) == ERROR
    assert "private" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert caught.value.__suppress_context__ is True


def test_model_validation_errors_hide_input_values() -> None:
    with pytest.raises(ValidationError) as caught:
        AgentAttachmentReceipt.model_validate(receipt(2, filename="private/secret"))
    rendered = str(caught.value)
    assert "private/secret" not in rendered
    assert "input_value" not in rendered
