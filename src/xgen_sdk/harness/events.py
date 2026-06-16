"""하네스 실행 이벤트를 xgen_sdk.logging.BackendLogger 로 흘리는 EventEmitter 브리지."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from xgen_harness import (
    DoneEvent,
    ErrorEvent,
    EventEmitter,
    RetryEvent,
    StageEnterEvent,
    StageExitEvent,
)

if TYPE_CHECKING:
    from xgen_harness.events.types import HarnessEvent
    from xgen_sdk.logging import BackendLogger


def logging_emitter(
    logger: "BackendLogger", emitter: Optional[EventEmitter] = None
) -> EventEmitter:
    emitter = emitter or EventEmitter()

    async def _forward(event: "HarnessEvent") -> None:
        stage = getattr(event, "stage_id", "") or getattr(event, "stage", "")
        meta = {"stage": stage} if stage else None
        if isinstance(event, ErrorEvent):
            logger.error(getattr(event, "message", "") or "harness error", meta, "harness")
        elif isinstance(event, RetryEvent):
            logger.warn(getattr(event, "reason", "") or "harness retry", meta, "harness")
        elif isinstance(event, DoneEvent):
            logger.info(
                "harness done",
                {"success": getattr(event, "success", None)},
                "harness",
            )
        elif isinstance(event, (StageEnterEvent, StageExitEvent)):
            logger.debug(type(event).__name__, meta, "harness")

    emitter.subscribe(_forward)
    return emitter
