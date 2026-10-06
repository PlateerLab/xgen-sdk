"""Strict local metadata contracts for canonical Agent Session attachments.

These models validate inert metadata only. Filenames are display values and
must never be joined to a filesystem path. The models do not upload content,
authenticate a receipt, or prove server ownership of an attachment.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


AGENT_ATTACHMENT_MAX_BYTES = 104_857_600
AGENT_ATTACHMENT_MAX_COUNT = 10
AGENT_ATTACHMENT_MAX_TOTAL_BYTES = 104_857_600

_ERROR_MESSAGE = "Invalid canonical Agent Session attachment metadata"
_MAX_USER_ID = "9223372036854775807"
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MIME = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
)
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_NUMERIC_HOST_LABEL = re.compile(r"^(?:[0-9]+|0x[0-9a-f]*)$")
_IPV6_TEXT = re.compile(r"^[0-9a-f:]+$")
_BIDI_CONTROLS = frozenset(
    (0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A))
)
_SCOPE_FIELDS = ("origin", "user_id", "session_id", "workflow_id")


class AgentAttachmentValidationError(ValueError):
    """The supplied metadata is not a safe canonical attachment contract."""


class _AttachmentModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    @classmethod
    def model_construct(
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> _AttachmentModel:
        declared = set(cls.model_fields)
        if set(values) - declared or (_fields_set is not None and set(_fields_set) - declared):
            raise AgentAttachmentValidationError(_ERROR_MESSAGE) from None
        return super().model_construct(_fields_set=_fields_set, **values)


def _valid_port(port: str | None) -> bool:
    if port is None:
        return True
    if (
        not port
        or len(port) > 5
        or not port.isascii()
        or not port.isdecimal()
        or port[0] == "0"
    ):
        return False
    number = int(port)
    return 1 <= number <= 65_535 and number != 443 and str(number) == port


def _valid_dns_or_ipv4(host: str) -> bool:
    if not host or len(host) > 253 or host.endswith("."):
        return False
    if all(character in "0123456789." for character in host):
        try:
            return str(ipaddress.IPv4Address(host)) == host
        except ipaddress.AddressValueError:
            return False
    labels = host.split(".")
    if any(not _DNS_LABEL.fullmatch(label) for label in labels):
        return False
    if any(label.startswith("xn--") for label in labels):
        return False
    # WHATWG treats a numeric final label as an IPv4 candidate rather than a DNS name.
    return _NUMERIC_HOST_LABEL.fullmatch(labels[-1]) is None


def _whatwg_ipv6(address: ipaddress.IPv6Address) -> str:
    """Serialize eight hextets without Python's version-dependent mapped form."""
    packed = address.packed
    hextets = [int.from_bytes(packed[index : index + 2], "big") for index in range(0, 16, 2)]

    best_start = -1
    best_length = 0
    index = 0
    while index < len(hextets):
        if hextets[index] != 0:
            index += 1
            continue
        end = index
        while end < len(hextets) and hextets[end] == 0:
            end += 1
        length = end - index
        if length >= 2 and length > best_length:
            best_start = index
            best_length = length
        index = end

    if best_start < 0:
        return ":".join(format(hextet, "x") for hextet in hextets)
    left = ":".join(format(hextet, "x") for hextet in hextets[:best_start])
    right = ":".join(format(hextet, "x") for hextet in hextets[best_start + best_length :])
    return f"{left}::{right}"


def _canonical_origin(value: str) -> bool:
    if not value.isascii() or not value.startswith("https://"):
        return False
    if "\\" in value or any(character.isspace() for character in value):
        return False
    authority = value[len("https://") :]
    if not authority or any(character in authority for character in "/?#@"):
        return False

    port: str | None = None
    if authority.startswith("["):
        close = authority.find("]")
        if close < 0:
            return False
        host = authority[1:close]
        suffix = authority[close + 1 :]
        if suffix:
            if not suffix.startswith(":"):
                return False
            port = suffix[1:]
        if not _valid_port(port) or not _IPV6_TEXT.fullmatch(host):
            return False
        try:
            canonical_host = _whatwg_ipv6(ipaddress.IPv6Address(host))
        except ipaddress.AddressValueError:
            return False
        if canonical_host != host:
            return False
        canonical = f"https://[{canonical_host}]" + (f":{port}" if port else "")
        return canonical == value

    if authority.count(":") > 1:
        return False
    if ":" in authority:
        host, port = authority.rsplit(":", 1)
    else:
        host = authority
    if not _valid_port(port) or not _valid_dns_or_ipv4(host):
        return False
    canonical = f"https://{host}" + (f":{port}" if port else "")
    return canonical == value


def _valid_unicode_metadata(value: str, maximum_bytes: int, *, filename: bool = False) -> bool:
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    if not 1 <= len(encoded) <= maximum_bytes:
        return False
    if "/" in value or "\\" in value or (filename and value in {".", ".."}):
        return False
    return not any(
        codepoint <= 0x1F
        or 0x7F <= codepoint <= 0x9F
        or codepoint in _BIDI_CONTROLS
        for codepoint in map(ord, value)
    )


class AgentAttachmentScope(_AttachmentModel):
    # Character caps reject oversized metadata before UTF-8 encoding or URL parsing.
    origin: str = Field(min_length=1, max_length=320)
    user_id: str = Field(min_length=1, max_length=19)
    session_id: str = Field(min_length=36, max_length=36)
    workflow_id: str = Field(min_length=1, max_length=128)

    @field_validator("origin")
    @classmethod
    def canonical_https_origin(cls, value: str) -> str:
        if not _canonical_origin(value):
            raise ValueError("origin must be canonical HTTPS")
        return value

    @field_validator("user_id")
    @classmethod
    def canonical_user_id(cls, value: str) -> str:
        if (
            not value.isascii()
            or not re.fullmatch(r"[1-9][0-9]*", value)
            or len(value) > len(_MAX_USER_ID)
            or (len(value) == len(_MAX_USER_ID) and value > _MAX_USER_ID)
        ):
            raise ValueError("user_id must be canonical decimal")
        return value

    @field_validator("session_id")
    @classmethod
    def canonical_session_id(cls, value: str) -> str:
        if not _UUID.fullmatch(value):
            raise ValueError("session_id must be a lowercase UUID")
        return value

    @field_validator("workflow_id")
    @classmethod
    def canonical_workflow_id(cls, value: str) -> str:
        if not _valid_unicode_metadata(value, 128):
            raise ValueError("workflow_id must be valid Unicode metadata")
        return value


class AgentAttachmentReceipt(AgentAttachmentScope):
    attachment_id: str = Field(min_length=36, max_length=36)
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0, le=AGENT_ATTACHMENT_MAX_BYTES)
    media_type: str = Field(min_length=3, max_length=255)
    sha256: str = Field(min_length=64, max_length=64)

    @field_validator("attachment_id")
    @classmethod
    def canonical_attachment_id(cls, value: str) -> str:
        if not _UUID.fullmatch(value):
            raise ValueError("attachment_id must be a lowercase UUID")
        return value

    @field_validator("filename")
    @classmethod
    def canonical_filename(cls, value: str) -> str:
        if not _valid_unicode_metadata(value, 255, filename=True):
            raise ValueError("filename must be valid Unicode metadata")
        return value

    @field_validator("media_type")
    @classmethod
    def canonical_media_type(cls, value: str) -> str:
        if not value.isascii() or not _MIME.fullmatch(value) or len(value.encode("ascii")) > 255:
            raise ValueError("media_type must be canonical MIME metadata")
        return value

    @field_validator("sha256")
    @classmethod
    def canonical_sha256(cls, value: str) -> str:
        if not value.isascii() or not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be lowercase hexadecimal")
        return value


class AgentAttachmentReference(_AttachmentModel):
    attachment_id: str = Field(min_length=36, max_length=36)
    sha256: str = Field(min_length=64, max_length=64)

    @field_validator("attachment_id")
    @classmethod
    def canonical_attachment_id(cls, value: str) -> str:
        if not _UUID.fullmatch(value):
            raise ValueError("attachment_id must be a lowercase UUID")
        return value

    @field_validator("sha256")
    @classmethod
    def canonical_sha256(cls, value: str) -> str:
        if not value.isascii() or not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be lowercase hexadecimal")
        return value


def _model_input(value: Any, expected_model: type[_AttachmentModel]) -> Any:
    if isinstance(value, BaseModel):
        if type(value) is not expected_model:
            raise ValueError("unexpected attachment model type")
        declared = set(expected_model.model_fields)
        if set(value.__dict__) != declared:
            raise ValueError("attachment model fields are not exact")
        extra = getattr(value, "__pydantic_extra__", None)
        if extra not in (None, {}):
            raise ValueError("attachment model has extra fields")
        fields_set = getattr(value, "__pydantic_fields_set__", set())
        if not isinstance(fields_set, set) or not fields_set <= declared:
            raise ValueError("attachment model field set is invalid")
        return expected_model.model_dump(
            value, mode="python", round_trip=True, warnings=False
        )
    return value


def _validate_scope(value: Any) -> AgentAttachmentScope:
    return AgentAttachmentScope.model_validate(
        _model_input(value, AgentAttachmentScope), strict=True
    )


def _validate_receipt(value: Any) -> AgentAttachmentReceipt:
    return AgentAttachmentReceipt.model_validate(
        _model_input(value, AgentAttachmentReceipt), strict=True
    )


def _same_scope(receipt: AgentAttachmentReceipt, scope: AgentAttachmentScope) -> bool:
    return all(getattr(receipt, field) == getattr(scope, field) for field in _SCOPE_FIELDS)


def _invalid() -> AgentAttachmentValidationError:
    return AgentAttachmentValidationError(_ERROR_MESSAGE)


def parse_attachment_scope(value: Any) -> AgentAttachmentScope:
    """Return a new, fully validated scope without trusting model construction."""
    try:
        return _validate_scope(value)
    except Exception:
        raise _invalid() from None


def parse_attachment_receipt(
    value: Any, expected_scope: Any
) -> AgentAttachmentReceipt:
    """Return a copied receipt only when all four authenticated scope fields match."""
    try:
        scope = _validate_scope(expected_scope)
        receipt = _validate_receipt(value)
        if not _same_scope(receipt, scope):
            raise ValueError("attachment receipt scope mismatch")
        return receipt
    except Exception:
        raise _invalid() from None


def prepare_attachment_references(
    receipts: Any, expected_scope: Any
) -> tuple[AgentAttachmentReference, ...]:
    """Validate local receipts and project ordered immutable turn references.

    Shape validation does not authenticate a receipt. The caller must provide
    an authenticated scope and independently enforce server ownership and the
    stored checksum before dispatch.
    """
    try:
        scope = _validate_scope(expected_scope)
        if not isinstance(receipts, (list, tuple)) or len(receipts) > AGENT_ATTACHMENT_MAX_COUNT:
            raise ValueError("attachment count is invalid")
        seen: set[str] = set()
        total_bytes = 0
        references: list[AgentAttachmentReference] = []
        for value in receipts:
            receipt = _validate_receipt(value)
            if not _same_scope(receipt, scope) or receipt.attachment_id in seen:
                raise ValueError("attachment receipt is not unique in the expected scope")
            seen.add(receipt.attachment_id)
            total_bytes += receipt.size_bytes
            if total_bytes > AGENT_ATTACHMENT_MAX_TOTAL_BYTES:
                raise ValueError("attachment aggregate size is invalid")
            references.append(
                AgentAttachmentReference(
                    attachment_id=receipt.attachment_id,
                    sha256=receipt.sha256,
                )
            )
        return tuple(references)
    except Exception:
        raise _invalid() from None


__all__ = [
    "AGENT_ATTACHMENT_MAX_BYTES",
    "AGENT_ATTACHMENT_MAX_COUNT",
    "AGENT_ATTACHMENT_MAX_TOTAL_BYTES",
    "AgentAttachmentReceipt",
    "AgentAttachmentReference",
    "AgentAttachmentScope",
    "AgentAttachmentValidationError",
    "parse_attachment_receipt",
    "parse_attachment_scope",
    "prepare_attachment_references",
]
