"""
ParameterResolver — 런타임 도구 호출 시 파라미터 자동 채움

우선순위:
  1. provided (LLM/사용자가 직접 준 값)
  2. context 추출 (source_hint 해석: user_input, context.last_message, ...)
  3. LLM 추론 (llm_fn 주입 시)
  4. capability spec default

누락 시 missing_param 이벤트 발행 가능.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from ..events.types import MissingParamEvent
from .schema import CapabilitySpec, ParamSpec

logger = logging.getLogger("harness.capabilities.resolver")


@dataclass
class ResolveResult:
    """파라미터 해석 결과"""

    args: dict[str, Any] = field(default_factory=dict)            # 최종 args
    missing: list[ParamSpec] = field(default_factory=list)        # 필수인데 못 채움
    warnings: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)         # param → "provided|context|llm|default"

    @property
    def ok(self) -> bool:
        return not self.missing

    def summary(self) -> str:
        parts = [f"args={len(self.args)}"]
        if self.missing:
            parts.append(f"missing={[p.name for p in self.missing]}")
        if self.warnings:
            parts.append(f"warnings={len(self.warnings)}")
        return ", ".join(parts)


class ParameterResolver:
    """
    도구 호출 시점에 CapabilitySpec + provided args + state context를 종합해서
    최종 호출 args를 생성.

    사용 예:
        resolver = ParameterResolver(spec, state, llm_fn=some_fn)
        result = await resolver.resolve(provided={"query": "..."})
        if result.ok:
            tool.execute(result.args)
        else:
            # missing param 이벤트 발행됨, UI가 되물음
    """

    def __init__(
        self,
        spec: CapabilitySpec,
        state,                                                    # PipelineState (순환 import 피함)
        *,
        llm_fn: Optional[Callable[[ParamSpec, "PipelineState"], Awaitable[Optional[Any]]]] = None,
        emit_missing_event: bool = True,
    ) -> None:
        self.spec = spec
        self.state = state
        self.llm_fn = llm_fn
        self.emit_missing_event = emit_missing_event

    async def resolve(self, provided: Optional[dict[str, Any]] = None) -> ResolveResult:
        provided = provided or {}
        result = ResolveResult()

        for param in self.spec.params:
            value, source = await self._resolve_one(param, provided)
            if value is not None:
                value = self._coerce(param, value, result)
                result.args[param.name] = value
                result.sources[param.name] = source
            elif param.required:
                result.missing.append(param)
                if self.emit_missing_event:
                    self._emit_missing_event(param)

        # enum 검증
        for name, value in list(result.args.items()):
            spec_param = next((p for p in self.spec.params if p.name == name), None)
            if spec_param and spec_param.enum and value not in spec_param.enum:
                result.warnings.append(
                    f"{name}={value!r} not in enum {spec_param.enum} — dropped"
                )
                result.args.pop(name)
                if spec_param.required:
                    result.missing.append(spec_param)

        return result

    # ---------- 내부 ----------

    async def _resolve_one(
        self, param: ParamSpec, provided: dict
    ) -> tuple[Optional[Any], str]:
        """우선순위 체인"""
        # 1. provided
        if param.name in provided and provided[param.name] not in (None, ""):
            return provided[param.name], "provided"

        # 2. context extract (source_hint)
        ctx_value = self._extract_from_context(param)
        if ctx_value is not None:
            return ctx_value, "context"

        # 3. LLM inference (옵션)
        if self.llm_fn is not None:
            try:
                llm_value = await self.llm_fn(param, self.state)
                if llm_value is not None:
                    return llm_value, "llm"
            except Exception as e:
                logger.warning("[Resolver] llm_fn failed for %s: %s", param.name, e)

        # 4. default
        if param.default is not None:
            return param.default, "default"

        return None, "none"

    def _extract_from_context(self, param: ParamSpec) -> Optional[Any]:
        """source_hint를 해석해 state에서 값을 꺼냄."""
        hint = (param.source_hint or "").strip().lower()
        if not hint:
            return None

        state = self.state

        if hint == "user_input":
            return getattr(state, "user_input", None) or None

        if hint in ("context.last_message", "last_message"):
            messages = getattr(state, "messages", []) or []
            for msg in reversed(messages):
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    return content
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            return block.get("text")
            return None

        if hint == "context.user_id":
            return getattr(state, "user_id", None) or None

        if hint == "context.workflow_id":
            return getattr(state, "workflow_id", None) or None

        if hint.startswith("metadata."):
            key = hint.split(".", 1)[1]
            return (getattr(state, "metadata", {}) or {}).get(key)

        if hint.startswith("state."):
            attr = hint.split(".", 1)[1]
            return getattr(state, attr, None)

        # 그 외 hint는 metadata에서 동일 키 시도
        metadata = getattr(state, "metadata", {}) or {}
        return metadata.get(hint)

    def _coerce(self, param: ParamSpec, value: Any, result: ResolveResult) -> Any:
        """간단한 타입 강제 — 실패 시 원본 유지하고 warning 기록."""
        t = param.type_hint

        try:
            if t == "str":
                return value if isinstance(value, str) else str(value)
            if t == "int":
                if isinstance(value, bool):  # bool은 int 서브클래스 — 제외
                    return int(value)
                return int(value) if not isinstance(value, int) else value
            if t == "float":
                return float(value) if not isinstance(value, float) else value
            if t == "bool":
                if isinstance(value, bool):
                    return value
                if isinstance(value, str):
                    return value.lower() in ("true", "1", "yes", "y", "on")
                return bool(value)
            if t.startswith("list"):
                if isinstance(value, list):
                    return value
                if isinstance(value, str):
                    return [s.strip() for s in value.split(",") if s.strip()]
                return [value]
            if t == "dict":
                return value if isinstance(value, dict) else {"value": value}
        except (ValueError, TypeError) as e:
            result.warnings.append(f"{param.name}: coerce to {t} failed ({e}) — kept raw")

        return value

    def _emit_missing_event(self, param: ParamSpec) -> None:
        emitter = getattr(self.state, "event_emitter", None)
        if emitter is None:
            return
        try:
            event = MissingParamEvent(
                capability=self.spec.name,
                tool_name=self.spec.tool_name or self.spec.name,
                param_name=param.name,
                param_type=param.type_hint,
                description=param.description,
                source_hint=param.source_hint,
            )
            emit_method = getattr(emitter, "emit", None)
            if callable(emit_method):
                emit_method(event)
        except Exception as e:
            logger.debug("[Resolver] Failed to emit missing_param event: %s", e)
