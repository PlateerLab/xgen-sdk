"""Typed policy loading and enforcement outcomes."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple, TYPE_CHECKING

from .errors import safe_request_id, validate_error_code

if TYPE_CHECKING:  # pragma: no cover
    from .rules import MaskingRule


class PolicyLoadState(str, Enum):
    DISABLED = "disabled"
    LOADED = "loaded"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ERROR = "error"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class PolicyLoadResult:
    """A policy read whose empty-success and failure states cannot be confused."""

    state: PolicyLoadState
    rules: Tuple["MaskingRule", ...] = ()
    revision: Optional[str] = None
    scope: Optional[str] = None
    error_code: Optional[str] = None
    request_id: Optional[str] = None
    retryable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", PolicyLoadState(self.state))
        object.__setattr__(self, "rules", tuple(self.rules))
        if self.state in (PolicyLoadState.UNAVAILABLE, PolicyLoadState.INVALID):
            if self.rules:
                raise ValueError("failed policy loads cannot carry rules")
            if not self.error_code:
                raise ValueError("failed policy loads require error_code")
            object.__setattr__(self, "error_code", validate_error_code(self.error_code))
            object.__setattr__(self, "request_id", safe_request_id(self.request_id))
        elif self.error_code is not None or self.request_id is not None or self.retryable:
            raise ValueError("successful policy loads cannot carry error metadata")
        elif self.state is PolicyLoadState.DISABLED and self.rules:
            raise ValueError("disabled policy loads cannot carry rules")

    @property
    def is_loaded(self) -> bool:
        return self.state is PolicyLoadState.LOADED

    @property
    def is_disabled(self) -> bool:
        return self.state is PolicyLoadState.DISABLED

    @property
    def is_failure(self) -> bool:
        return self.state in (PolicyLoadState.UNAVAILABLE, PolicyLoadState.INVALID)


@dataclass(frozen=True)
class PolicyOutcome:
    """A redacted decision suitable for crossing an HTTP/service boundary."""

    decision: PolicyDecision
    error_code: Optional[str] = None
    request_id: Optional[str] = None
    retryable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision", PolicyDecision(self.decision))
        if self.decision is PolicyDecision.ERROR:
            if not self.error_code:
                raise ValueError("error outcomes require error_code")
            object.__setattr__(self, "error_code", validate_error_code(self.error_code))
            object.__setattr__(self, "request_id", safe_request_id(self.request_id))
        elif self.error_code is not None or self.request_id is not None or self.retryable:
            raise ValueError("non-error outcomes cannot carry error metadata")


__all__ = ["PolicyLoadState", "PolicyLoadResult", "PolicyDecision", "PolicyOutcome"]
