"""Contracts for the canonical account Agent Session focus API.

These models describe verified API responses. They do not authenticate a caller
or replace the Gateway's Platform Session and DPoP checks.
"""

from .focus import (
    AccountEventPage,
    AccountFocusEvent,
    AgentSessionCreated,
    CreateAgentSession,
    CursorConflictResponse,
    FocusConflictResponse,
    FocusReplayGap,
    FocusState,
    SwitchFocus,
    apply_event_page,
)

__all__ = [
    "AccountEventPage",
    "AccountFocusEvent",
    "AgentSessionCreated",
    "CreateAgentSession",
    "CursorConflictResponse",
    "FocusConflictResponse",
    "FocusReplayGap",
    "FocusState",
    "SwitchFocus",
    "apply_event_page",
]
