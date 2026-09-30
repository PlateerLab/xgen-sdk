"""Contracts for account focus, Agent Session events and linked messages.

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
from .events import (
    AgentSessionEvent,
    AgentSessionEventFrame,
    AgentSessionEventPage,
    AgentSessionReplayCursor,
    AgentSessionReplayGap,
    AgentSessionSnapshot,
    AgentSessionTurnSummary,
    SessionCursorConflictResponse,
    apply_session_event_page,
)
from .messages import (
    AgentSessionMessage,
    AgentSessionMessageConflictResponse,
    AgentSessionMessageCursor,
    AgentSessionMessageGap,
    AgentSessionMessagePage,
    apply_message_page,
)

__all__ = [
    "AccountEventPage",
    "AccountFocusEvent",
    "AgentSessionEvent",
    "AgentSessionEventFrame",
    "AgentSessionEventPage",
    "AgentSessionMessage",
    "AgentSessionMessageConflictResponse",
    "AgentSessionMessageCursor",
    "AgentSessionMessageGap",
    "AgentSessionMessagePage",
    "AgentSessionReplayCursor",
    "AgentSessionReplayGap",
    "AgentSessionSnapshot",
    "AgentSessionTurnSummary",
    "AgentSessionCreated",
    "CreateAgentSession",
    "CursorConflictResponse",
    "FocusConflictResponse",
    "FocusReplayGap",
    "FocusState",
    "SwitchFocus",
    "SessionCursorConflictResponse",
    "apply_event_page",
    "apply_message_page",
    "apply_session_event_page",
]
