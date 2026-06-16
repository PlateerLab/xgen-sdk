"""
PipelineBuilder — Fluent API로 파이프라인 구성

체이닝으로 파이프라인 구성:
    pipeline = (PipelineBuilder()
        # with_provider 생략 → providers.get_default_provider() 로 해석 (env/레지스트리 기반).
        # .with_provider("openai", "gpt-4o-mini", api_key)  # 명시 예시
        .with_system("You are a helpful assistant.")
        .with_tools([weather_tool, search_tool])
        .with_mcp_sessions(["session-abc"])
        .with_rag(collection="docs", top_k=5)
        .with_validate(threshold=0.8)
        .disable("s05_policy")
        .with_artifact("s00_harness", "streaming")
        .build())
"""

from typing import Any, Optional

from .config import HarnessConfig, ALL_STAGES, REQUIRED_STAGES
from .execution_context import set_execution_context
from .pipeline import Pipeline
from .state import PipelineState
from .registry import ArtifactRegistry
from ..events.emitter import EventEmitter
from ..tools.base import Tool


class PipelineBuilder:
    """Fluent builder로 파이프라인 구성"""

    def __init__(self):
        from ..providers import get_default_provider, get_default_model
        self._provider = get_default_provider()
        self._model = get_default_model(self._provider)
        self._api_key = ""
        self._temperature = 0.7
        self._max_tokens = 8192
        self._system_prompt = ""
        self._disabled: set[str] = set()
        self._artifacts: dict[str, str] = {}
        self._active_strategies: dict[str, str] = {}  # v1.0 — Strategy 카드 픽
        self._stage_params: dict[str, dict] = {}      # v1.0 — stage_params 누적
        self._tools: list[Tool] = []
        self._tool_definitions: list[dict] = []
        self._mcp_sessions: list[str] = []
        self._rag_collections: list[dict] = []  # [{"collection": "docs", "top_k": 5}]
        self._max_iterations = 10
        self._max_retries = 3
        self._validation_threshold = 0.7
        self._thinking_enabled = False
        self._thinking_budget = 10000
        self._event_emitter: Optional[EventEmitter] = None

    # --- Provider ---

    def with_provider(self, provider: str, model: str = "", api_key: str = "") -> "PipelineBuilder":
        self._provider = provider
        if model:
            self._model = model
        if api_key:
            self._api_key = api_key
        return self

    def with_model(self, model: str) -> "PipelineBuilder":
        self._model = model
        return self

    def with_api_key(self, key: str) -> "PipelineBuilder":
        self._api_key = key
        return self

    def with_temperature(self, temp: float) -> "PipelineBuilder":
        self._temperature = temp
        return self

    # --- System Prompt ---

    def with_system(self, prompt: str) -> "PipelineBuilder":
        self._system_prompt = prompt
        return self

    # --- Tools ---

    def with_tools(self, tools: list[Tool]) -> "PipelineBuilder":
        """Tool ABC 인스턴스 등록"""
        self._tools.extend(tools)
        return self

    def with_tool_definitions(self, defs: list[dict]) -> "PipelineBuilder":
        """Anthropic API 포맷 도구 정의 직접 등록"""
        self._tool_definitions.extend(defs)
        return self

    def with_mcp_sessions(self, session_ids: list[str]) -> "PipelineBuilder":
        """MCP 세션 ID 등록 (실행 시 자동 디스커버리)"""
        self._mcp_sessions.extend(session_ids)
        return self

    # --- RAG ---

    def with_rag(self, collection: str, top_k: int = 4, **_legacy_kwargs) -> "PipelineBuilder":
        """RAG 컬렉션 등록 (실행 시 자동 검색).

        v1.12.2 — enhance_prompt 인자 폐기. 옛 호출자 BC 위해 **_legacy_kwargs 로 흡수만.
        값은 어디서도 read 되지 않음.
        """
        self._rag_collections.append({
            "collection": collection,
            "top_k": top_k,
        })
        return self

    # --- Stage Control ---

    def disable(self, stage_id: str) -> "PipelineBuilder":
        """스테이지 비활성화"""
        if stage_id not in REQUIRED_STAGES:
            self._disabled.add(stage_id)
        return self

    def enable(self, stage_id: str) -> "PipelineBuilder":
        """스테이지 활성화"""
        self._disabled.discard(stage_id)
        return self

    def with_artifact(self, stage_id: str, artifact_name: str) -> "PipelineBuilder":
        """스테이지 아티팩트 선택"""
        self._artifacts[stage_id] = artifact_name
        return self

    # --- Validation ---

    def with_validate(self, threshold: float = 0.7) -> "PipelineBuilder":
        """검증 활성화 — v1.0 에선 s08_decide 의 judge_then_loop strategy 픽 + threshold 설정.

        구 s08_judge stage 가 격하됨 → s08_decide 에 strategy 로 흡수.
        """
        self._active_strategies["s08_decide"] = "judge_then_loop"
        self._stage_params.setdefault("s08_decide", {})["judge_threshold"] = threshold
        self._validation_threshold = threshold
        return self

    def without_validate(self) -> "PipelineBuilder":
        # active_strategies 에서 judge_then_loop 떼기 (다른 strategy 로 복귀)
        if self._active_strategies.get("s08_decide") == "judge_then_loop":
            self._active_strategies["s08_decide"] = "threshold"
        return self

    # --- Loop ---

    def with_loop(self, max_iterations: int = 10, max_retries: int = 3) -> "PipelineBuilder":
        self._max_iterations = max_iterations
        self._max_retries = max_retries
        return self

    # --- Thinking ---

    def with_thinking(self, budget_tokens: int = 10000) -> "PipelineBuilder":
        self._thinking_enabled = True
        self._thinking_budget = budget_tokens
        return self

    # --- Events ---

    def with_emitter(self, emitter: EventEmitter) -> "PipelineBuilder":
        self._event_emitter = emitter
        return self

    # === Build ===

    def build(self) -> Pipeline:
        """파이프라인 빌드"""
        # API 키를 실행 컨텍스트에 설정 (contextvars — 동시 실행 격리)
        if self._api_key:
            set_execution_context(
                api_key=self._api_key,
                provider=self._provider,
                model=self._model,
            )

        config = HarnessConfig(
            provider=self._provider,
            model=self._model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            system_prompt=self._system_prompt,
            disabled_stages=self._disabled,
            artifacts=self._artifacts,
            max_iterations=self._max_iterations,
            max_retries=self._max_retries,
            validation_threshold=self._validation_threshold,
            thinking_enabled=self._thinking_enabled,
            thinking_budget_tokens=self._thinking_budget,
            active_strategies=dict(self._active_strategies),
            stage_params=dict(self._stage_params),
        )

        emitter = self._event_emitter or EventEmitter()
        pipeline = Pipeline.from_config(config, emitter)
        return pipeline

    def build_state(self, user_input: str, **kwargs) -> PipelineState:
        """PipelineState도 함께 빌드"""
        state = PipelineState(user_input=user_input, **kwargs)

        # 도구 등록
        for tool in self._tools:
            state.tool_definitions.append(tool.to_api_format())
            if "tool_registry" not in state.metadata:
                state.metadata["tool_registry"] = {}
            state.metadata["tool_registry"][tool.name] = tool

        state.tool_definitions.extend(self._tool_definitions)

        # MCP 세션
        if self._mcp_sessions:
            state.metadata["mcp_sessions"] = self._mcp_sessions

        # RAG 컬렉션
        if self._rag_collections:
            state.metadata["rag_collections"] = self._rag_collections

        return state

    def describe(self) -> dict:
        """현재 빌더 설정 요약"""
        return {
            "provider": self._provider,
            "model": self._model,
            "disabled_stages": list(self._disabled),
            "artifacts": self._artifacts,
            "tools": len(self._tools) + len(self._tool_definitions),
            "mcp_sessions": self._mcp_sessions,
            "rag_collections": [r["collection"] for r in self._rag_collections],
            "validation": self._active_strategies.get("s08_decide") == "judge_then_loop",
            "thinking": self._thinking_enabled,
            "max_iterations": self._max_iterations,
        }

    # ───────────────────────────────────────────────
    # 직렬화 — 빌더 상태를 저장/로드
    # ───────────────────────────────────────────────
    # api_key / Tool 인스턴스 / EventEmitter 는 제외 (민감정보 / 실행 시 재주입).
    # tool_definitions 는 순수 데이터라 포함.

    def to_dict(self) -> dict[str, Any]:
        """빌더 상태를 JSON-직렬화 가능한 dict 로.

        제외: api_key (보안), Tool ABC 인스턴스 (재직렬화 불가), EventEmitter (런타임 객체).
        """
        return {
            "provider": self._provider,
            "model": self._model,
            "temperature": float(self._temperature),
            "max_tokens": int(self._max_tokens),
            "system_prompt": self._system_prompt,
            "disabled_stages": sorted(self._disabled),
            "artifacts": dict(self._artifacts),
            "tool_definitions": list(self._tool_definitions),
            "mcp_sessions": list(self._mcp_sessions),
            "rag_collections": list(self._rag_collections),
            "max_iterations": int(self._max_iterations),
            "max_retries": int(self._max_retries),
            "validation_threshold": float(self._validation_threshold),
            "thinking_enabled": bool(self._thinking_enabled),
            "thinking_budget": int(self._thinking_budget),
            "_schema_version": 1,
        }

    def to_json(self, indent: int = 2) -> str:
        import json
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PipelineBuilder":
        b = cls()
        b._provider = data.get("provider", b._provider)
        b._model = data.get("model", b._model)
        b._temperature = float(data.get("temperature", b._temperature))
        b._max_tokens = int(data.get("max_tokens", b._max_tokens))
        b._system_prompt = data.get("system_prompt", b._system_prompt)
        b._disabled = set(data.get("disabled_stages", []))
        b._artifacts = dict(data.get("artifacts", {}))
        b._tool_definitions = list(data.get("tool_definitions", []))
        b._mcp_sessions = list(data.get("mcp_sessions", []))
        b._rag_collections = list(data.get("rag_collections", []))
        b._max_iterations = int(data.get("max_iterations", b._max_iterations))
        b._max_retries = int(data.get("max_retries", b._max_retries))
        b._validation_threshold = float(data.get("validation_threshold", b._validation_threshold))
        b._thinking_enabled = bool(data.get("thinking_enabled", b._thinking_enabled))
        b._thinking_budget = int(data.get("thinking_budget", b._thinking_budget))
        return b

    @classmethod
    def from_json(cls, text: str) -> "PipelineBuilder":
        import json
        return cls.from_dict(json.loads(text))

    @classmethod
    def load(cls, path: str) -> "PipelineBuilder":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_json(f.read())
