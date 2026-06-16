"""
Session — 멀티턴 대화 + 설정 저장/로드

멀티턴 세션 관리.
하네스 설정과 대화 이력을 유지하여 멀티턴 실행.
"""

import copy
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import HarnessConfig
from .pipeline import Pipeline
from .state import PipelineState
from ..events.emitter import EventEmitter

logger = logging.getLogger("harness.session")


@dataclass
class SessionState:
    """세션 상태 — 대화 이력 + 설정"""
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    config: HarnessConfig = field(default_factory=HarnessConfig)
    messages: list[dict] = field(default_factory=list)  # 누적 대화 이력
    turn_count: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    created_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


class HarnessSession:
    """멀티턴 하네스 세션"""

    def __init__(self, config: Optional[HarnessConfig] = None, session_id: str = ""):
        self.state = SessionState(
            session_id=session_id or str(uuid.uuid4())[:8],
            config=config or HarnessConfig(),
        )

    @property
    def session_id(self) -> str:
        return self.state.session_id

    @property
    def config(self) -> HarnessConfig:
        return self.state.config

    async def run(self, user_input: str, emitter: Optional[EventEmitter] = None) -> PipelineState:
        """한 턴 실행 — 이전 대화 이력 유지"""
        emitter = emitter or EventEmitter()
        pipeline = Pipeline.from_config(self.config, emitter)

        # v0.11.27 — list.copy() 는 shallow 라 내부 dict 가 세션 이력과 공유됨.
        # 멀티턴 실행 중 메시지 dict 를 건드리면 이전 턴 기록까지 변질되므로 deepcopy 로 격리.
        state = PipelineState(
            user_input=user_input,
            conversation_history=copy.deepcopy(self.state.messages),
        )
        # 이전 대화를 messages 에 직접 주입 (s02_history 비활성이어도 동작). 마찬가지로 deepcopy.
        for msg in self.state.messages:
            state.messages.append(copy.deepcopy(msg))

        await pipeline.run(state)

        # 이번 턴 메시지를 세션 이력에 추가
        self.state.messages.append({"role": "user", "content": user_input})
        if state.final_output:
            self.state.messages.append({"role": "assistant", "content": state.final_output})

        self.state.turn_count += 1
        self.state.total_tokens += state.token_usage.total
        self.state.total_cost_usd += state.cost_usd

        return state

    def to_dict(self) -> dict:
        """세션 상태를 직렬화 (저장용)"""
        return {
            "session_id": self.state.session_id,
            "config": {
                "provider": self.config.provider,
                "model": self.config.model,
                "temperature": self.config.temperature,
                "max_tokens": self.config.max_tokens,
                "system_prompt": self.config.system_prompt,
                "disabled_stages": list(self.config.disabled_stages),
                "artifacts": self.config.artifacts,
                "max_iterations": self.config.max_iterations,
                "validation_threshold": self.config.validation_threshold,
            },
            "messages": self.state.messages,
            "turn_count": self.state.turn_count,
            "total_tokens": self.state.total_tokens,
            "total_cost_usd": self.state.total_cost_usd,
            "created_at": self.state.created_at,
            "metadata": self.state.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HarnessSession":
        """직렬화된 상태에서 세션 복원"""
        from ..providers import get_default_provider, get_default_model
        config_data = data.get("config", {})
        provider = config_data.get("provider") or get_default_provider()
        config = HarnessConfig(
            provider=provider,
            model=config_data.get("model") or get_default_model(provider),
            temperature=config_data.get("temperature", 0.7),
            max_tokens=config_data.get("max_tokens", 8192),
            system_prompt=config_data.get("system_prompt", ""),
            disabled_stages=set(config_data.get("disabled_stages", [])),
            artifacts=config_data.get("artifacts", {}),
            max_iterations=config_data.get("max_iterations", 5),
            validation_threshold=config_data.get("validation_threshold", 0.7),
        )

        session = cls(config=config, session_id=data.get("session_id", ""))
        session.state.messages = data.get("messages", [])
        session.state.turn_count = data.get("turn_count", 0)
        session.state.total_tokens = data.get("total_tokens", 0)
        session.state.total_cost_usd = data.get("total_cost_usd", 0.0)
        session.state.created_at = data.get("created_at", time.time())
        session.state.metadata = data.get("metadata", {})
        return session

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> "HarnessSession":
        return cls.from_dict(json.loads(json_str))


class SessionManager:
    """세션 매니저 — 인메모리 + DB 저장/로드"""

    def __init__(self):
        self._sessions: dict[str, HarnessSession] = {}

    def create(self, config: Optional[HarnessConfig] = None) -> HarnessSession:
        session = HarnessSession(config=config)
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Optional[HarnessSession]:
        return self._sessions.get(session_id)

    def delete(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def list_sessions(self) -> list[dict]:
        return [
            {
                "session_id": s.session_id,
                "turn_count": s.state.turn_count,
                "total_tokens": s.state.total_tokens,
                "provider": s.config.provider,
                "model": s.config.model,
            }
            for s in self._sessions.values()
        ]

    async def save_to_db(self, session_id: str, db_manager) -> bool:
        """DB에 세션 저장"""
        session = self._sessions.get(session_id)
        if not session or not db_manager:
            return False
        try:
            record = {
                "session_id": session_id,
                "session_data": session.to_json(),
                "updated_at": time.time(),
            }
            db_manager.upsert_record("harness_sessions", {"session_id": session_id}, record)
            return True
        except Exception as e:
            logger.error("Failed to save session %s: %s", session_id, e)
            return False

    async def load_from_db(self, session_id: str, db_manager) -> Optional[HarnessSession]:
        """DB에서 세션 로드"""
        if not db_manager:
            return None
        try:
            records = db_manager.find_records_by_condition(
                "harness_sessions", {"session_id": session_id}, limit=1
            )
            if records:
                session = HarnessSession.from_json(records[0]["session_data"])
                self._sessions[session_id] = session
                return session
        except Exception as e:
            logger.error("Failed to load session %s: %s", session_id, e)
        return None
