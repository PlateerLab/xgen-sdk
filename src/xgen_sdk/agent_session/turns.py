"""Pure canonical Agent Session turn submission wire contract.

This module validates inert request data only. It does not authenticate a
caller, upload content, verify a receipt or HMAC, or prove server ownership of
an attachment.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .attachments import AGENT_ATTACHMENT_MAX_COUNT, AgentAttachmentReference, parse_attachment_references


_ERROR_MESSAGE = "Invalid canonical Agent Session turn submission"


class AgentSessionTurnValidationError(ValueError):
    """The supplied value is not a safe canonical turn submission."""


class SubmitAgentSessionTurn(BaseModel):
    """Strict immutable request body for one canonical Agent Session turn."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    input_text: str = Field(min_length=1, max_length=262_144)
    expected_state_version: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=128)
    origin_id: str | None = Field(default=None, min_length=1, max_length=128)
    attachments: tuple[AgentAttachmentReference, ...] = Field(default=(), max_length=AGENT_ATTACHMENT_MAX_COUNT)

    @field_validator("attachments", mode="before")
    @classmethod
    def canonical_attachments(cls, value: Any) -> tuple[AgentAttachmentReference, ...]:
        return parse_attachment_references(value)

    @classmethod
    def model_construct(
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> SubmitAgentSessionTurn:
        declared = set(cls.model_fields)
        if set(values) - declared or (_fields_set is not None and set(_fields_set) - declared):
            raise AgentSessionTurnValidationError(_ERROR_MESSAGE) from None
        return super().model_construct(_fields_set=_fields_set, **values)


def _model_input(value: Any) -> dict[str, Any]:
    if type(value) is not SubmitAgentSessionTurn:
        raise ValueError("unexpected turn submission model type")
    declared = set(SubmitAgentSessionTurn.model_fields)
    if set(value.__dict__) != declared:
        raise ValueError("turn submission model fields are not exact")
    extra = getattr(value, "__pydantic_extra__", None)
    if extra not in (None, {}):
        raise ValueError("turn submission model has extra fields")
    fields_set = getattr(value, "__pydantic_fields_set__", set())
    if not isinstance(fields_set, set) or not fields_set <= declared:
        raise ValueError("turn submission model field set is invalid")
    # Preserve the raw nested reference instances for the before-validator.
    # Serializing here could silently discard an injected field from a forged
    # reference before ``parse_attachment_references`` has a chance to reject it.
    return {field: value.__dict__[field] for field in declared}


def parse_turn_submission(value: Any) -> SubmitAgentSessionTurn:
    """Copy and fully revalidate an exact turn DTO without trusting construction."""
    try:
        return SubmitAgentSessionTurn.model_validate(_model_input(value), strict=True)
    except Exception:
        pass
    raise AgentSessionTurnValidationError(_ERROR_MESSAGE) from None


__all__ = [
    "AgentSessionTurnValidationError",
    "SubmitAgentSessionTurn",
    "parse_turn_submission",
]
