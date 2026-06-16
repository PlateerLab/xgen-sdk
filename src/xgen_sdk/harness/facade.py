from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from xgen_harness import (
    HarnessConfig,
    HarnessSession,
    Pipeline,
    load_session,
    register_stage,
    save_session,
)
from xgen_harness.core.config import mark_stage_required
from xgen_harness.core.execution_context import set_execution_context
from xgen_harness.providers import get_api_key_env

if TYPE_CHECKING:
    from xgen_harness.core.state import PipelineState
    from xgen_harness.events.emitter import EventEmitter
    from xgen_harness.memory import SessionStore


def _resolve_key(provider: str, explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    name = get_api_key_env(provider)
    try:
        from xgen_sdk.config import get_config_value

        value = get_config_value(name)
        if value:
            return str(value)
    except Exception:
        pass
    import os

    return os.environ.get(name, "")


class Harness:
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        store: "Optional[SessionStore]" = None,
        emitter: "Optional[EventEmitter]" = None,
        **config_kwargs: Any,
    ) -> None:
        self._provider = config_kwargs.get("provider", "")
        self._model = config_kwargs.get("model", "")
        self._api_key = _resolve_key(self._provider, api_key)
        self._store = store
        self._emitter = emitter
        self.config: HarnessConfig = HarnessConfig(**config_kwargs)

    def add_step(
        self,
        stage_id: str,
        stage_class: type,
        *,
        artifact: str = "default",
        required: bool = False,
    ) -> "Harness":
        register_stage(stage_id, artifact, stage_class)
        if required:
            mark_stage_required(stage_id)
        return self

    def delete_step(self, stage_id: str) -> "Harness":
        self.config.toggle_stage(stage_id, False)
        return self

    def enable_step(self, stage_id: str) -> "Harness":
        self.config.toggle_stage(stage_id, True)
        return self

    def steps(self) -> list[str]:
        return self.config.get_active_stage_ids()

    async def run(
        self,
        user_input: str,
        emitter: "Optional[EventEmitter]" = None,
        session_id: Optional[str] = None,
    ) -> "PipelineState":
        if self._api_key:
            set_execution_context(
                api_key=self._api_key, provider=self._provider, model=self._model
            )
        session = None
        if session_id and self._store is not None:
            session = load_session(self._store, session_id, self.config)
        if session is None:
            session = HarnessSession(config=self.config, session_id=session_id or "")
        state = await session.run(user_input, emitter or self._emitter)
        if self._store is not None:
            save_session(self._store, session)
        return state

    def build(self) -> Pipeline:
        return Pipeline.from_config(self.config)
