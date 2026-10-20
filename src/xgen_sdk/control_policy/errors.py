"""Typed, redacted failures for strict control-policy reads."""
from __future__ import annotations

import re
import uuid
from typing import Optional

_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def safe_request_id(value: Optional[str] = None) -> str:
    """Return an opaque request id without copying arbitrary input into errors."""
    if value is not None and _SAFE_REQUEST_ID.fullmatch(str(value)):
        return str(value)
    return uuid.uuid4().hex


def validate_error_code(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_CODE.fullmatch(value):
        raise ValueError("error_code must be a safe machine-readable code")
    return value


class PolicyError(RuntimeError):
    """Base error whose string form contains only a safe code and request id."""

    default_code = "policy_error"
    default_retryable = False

    def __init__(
        self,
        code: Optional[str] = None,
        *,
        request_id: Optional[str] = None,
        retryable: Optional[bool] = None,
    ) -> None:
        self.code = validate_error_code(code or self.default_code)
        self.request_id = safe_request_id(request_id)
        self.retryable = self.default_retryable if retryable is None else bool(retryable)
        super().__init__(f"{self.code} (request_id={self.request_id})")


class PolicyUnavailableError(PolicyError):
    """The authoritative policy/configuration store could not be read."""

    default_code = "policy_store_unavailable"
    default_retryable = True


class PolicyInvalidError(PolicyError):
    """The authoritative store returned an invalid policy or configuration."""

    default_code = "policy_data_invalid"
    default_retryable = False


__all__ = [
    "PolicyError",
    "PolicyUnavailableError",
    "PolicyInvalidError",
    "safe_request_id",
    "validate_error_code",
]
