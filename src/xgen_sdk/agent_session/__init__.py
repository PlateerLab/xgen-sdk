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
from .attachments import (
    AGENT_ATTACHMENT_MAX_BYTES,
    AGENT_ATTACHMENT_MAX_COUNT,
    AGENT_ATTACHMENT_MAX_TOTAL_BYTES,
    AgentAttachmentReceipt,
    AgentAttachmentReference,
    AgentAttachmentScope,
    AgentAttachmentValidationError,
    parse_attachment_receipt,
    parse_attachment_references,
    parse_attachment_scope,
    prepare_attachment_references,
)
from .turns import (
    AgentSessionTurnValidationError,
    SubmitAgentSessionTurn,
    parse_turn_submission,
)

__all__ = [
    "AGENT_ATTACHMENT_MAX_BYTES",
    "AGENT_ATTACHMENT_MAX_COUNT",
    "AGENT_ATTACHMENT_MAX_TOTAL_BYTES",
    "AccountEventPage",
    "AccountFocusEvent",
    "AgentAttachmentReceipt",
    "AgentAttachmentReference",
    "AgentAttachmentScope",
    "AgentAttachmentValidationError",
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
    "AgentSessionTurnValidationError",
    "AgentSessionCreated",
    "CreateAgentSession",
    "CursorConflictResponse",
    "FocusConflictResponse",
    "FocusReplayGap",
    "FocusState",
    "SwitchFocus",
    "SubmitAgentSessionTurn",
    "SessionCursorConflictResponse",
    "apply_event_page",
    "apply_message_page",
    "apply_session_event_page",
    "parse_attachment_receipt",
    "parse_attachment_references",
    "parse_attachment_scope",
    "parse_turn_submission",
    "prepare_attachment_references",
]
