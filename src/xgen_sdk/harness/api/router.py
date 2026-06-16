"""
하네스 API 라우터 — xgen-workflow에 include

xgen-workflow main.py:
    from xgen_harness.api.router import harness_router
    app.include_router(harness_router, prefix="/api/harness")

엔드포인트:
    GET  /api/harness/stages      — 등록된 모든 스테이지 설명 + required 정보 (v1.0 = 10)
    POST /api/harness/execute     — SSE 스트리밍 실행
    POST /api/harness/orchestrate — 멀티에이전트 DAG SSE 실행
    WS   /api/harness/ws/{sid}    — WebSocket 실행
"""

import asyncio
import json
import logging
import os
import uuid
from typing import Optional

from ..core.config import HarnessConfig, ALL_STAGES, REQUIRED_STAGES
from ..core.registry import ArtifactRegistry

logger = logging.getLogger("harness.api")

try:
    from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel

    harness_router = APIRouter(tags=["harness"])

    # === Models ===

    class ExecuteRequest(BaseModel):
        text: str
        # provider 미지정 시 providers.get_default_provider() 가 런타임 해석
        # (env XGEN_HARNESS_DEFAULT_PROVIDER → openai → anthropic → registry[0]).
        provider: str = ""
        # model 미지정 시 provider 의 PROVIDER_DEFAULT_MODEL 조회 (빈 문자열이면 adapter/stage 에서 해석).
        model: str = ""
        temperature: float = 0.7
        system_prompt: str = ""
        disabled_stages: list[str] = []
        artifacts: dict[str, str] = {}

    # === 스테이지 ===

    @harness_router.get("/stages")
    async def list_stages():
        """등록된 스테이지 전체 + required + 설정 스키마 (registry 기반 — 추가 stage 자동 합류)"""
        from ..core.stage_config import get_all_stage_configs
        registry = ArtifactRegistry.default()
        stages = registry.describe_all()
        configs = get_all_stage_configs()
        # v1.7.1 — registry.describe_all() 의 stage 응답에 stage_config 의 모든 키 자동 합류.
        # 옛 cherry-pick (icon/description_ko/description_en/fields/behavior 5 key) 는 RESERVED 외
        # 키 spread 로 자동 박힘 (backward 호환). 추가로 _inject_visibility_meta 가 박은
        # expose_strategy_picker, _inject_stage_meta 가 박은 progressive_threshold, 외부
        # Stage 가 박은 bypass_ko/en 등 임의 키 자동 합류 — 확장성 정합.
        RESERVED_STAGE_KEYS = {
            "stage_id", "display_name", "display_name_ko", "phase", "order",
            "role", "active", "required", "artifacts", "current_artifact",
            "source_file", "strategies",
        }
        for s in stages:
            s["active"] = True
            s["required"] = s["stage_id"] in REQUIRED_STAGES
            cfg = configs.get(s["stage_id"], {})
            for k, v in cfg.items():
                if k not in RESERVED_STAGE_KEYS:
                    s[k] = v
        return {"stages": stages, "required": list(REQUIRED_STAGES), "all": ALL_STAGES}

    # === Tool Sources (v0.25.0) ===
    #
    # 단일 도구 공급 채널. 등록된 모든 ToolSource 의 메타 + list_tools 결과.
    # 프론트 s04 UI 가 이 응답 하나로 Box 전부 동적 렌더.
    #
    # 쿼리 파라미터 (옵션):
    #   include_tools=true  — 각 소스의 list_tools() 결과 포함 (기본 true)
    #   filters=<json>      — {source_id: {...}} 필터 맵 (UI 에서 sub-UI 상태 전파용)

    from fastapi import Request

    @harness_router.get("/tool-sources")
    async def list_tool_sources(
        request: Request,
        include_tools: bool = True,
        filters: str = "",
    ):
        """등록된 ToolSource 메타 + (옵션) 각 소스의 도구 목록.

        요청 헤더 (Authorization / x-user-*) 는 ``use_request_headers`` 컨텍스트로
        전파되어 각 ToolSource 의 self-loopback 호출에 재사용된다.

        응답 shape::

            {
              "sources": [
                {
                  "source_id": "mcp-sessions",
                  "display_name": "MCP Sessions",
                  "display_name_ko": "MCP 세션",
                  "description": "...",
                  "icon": "🔌",
                  "category": "mcp",
                  "filter_schema": {...},
                  "tools": [
                    {"name": "...", "description": "...", "input_schema": {...}, "tags": [...]}
                  ]
                },
                ...
              ]
            }
        """
        from ..tools import describe_all_sources, list_all_tools, use_request_headers

        sources = describe_all_sources()
        if include_tools:
            parsed_filters: dict = {}
            if filters:
                try:
                    parsed_filters = json.loads(filters) or {}
                except Exception as e:
                    logger.debug("[tool-sources] filters json parse failed: %s", e)
            with use_request_headers(dict(request.headers)):
                tools_by_sid = await list_all_tools(parsed_filters)
            for s in sources:
                s["tools"] = tools_by_sid.get(s["source_id"], [])
        else:
            for s in sources:
                s["tools"] = []
        return {"sources": sources}

    # === 동적 옵션 (v0.11.25 제거) ===
    #
    # 엔진이 MCP 세션 / RAG 컬렉션 조회용 엔드포인트를 제공하던 경로는 삭제됨.
    # 이유:
    # 1. 엔진(라이브러리) 은 xgen 인프라 (xgen-documents / xgen-mcp-station) API 스키마
    #    (`/api/retrieval/documents/collections`, `/api/mcp/sessions`) 를 알 필요 없음.
    #    라이브러리 ≠ 인프라 원칙 (0.1 기조) 위반이었음.
    # 2. 이식측 `xgen-workflow` 가 `OptionSource` 레지스트리 기반 `/harness/options/<name>`
    #    단일 진입점을 이미 제공 (v0.11.23 단일 진실 소스). 두 곳에서 같은 데이터를
    #    내리면 응답 스키마 drift 가 발생.
    # 3. 이식/프론트 어느 쪽도 엔진의 이 엔드포인트를 호출하지 않아 제거가 안전.
    #
    # 외부 조직이 엔진만 단독 실행 (MCP stdio / CLI) 할 때 옵션 조회가 필요하면
    # `register_option_source()` 패턴으로 자기 ServiceProvider 또는 레지스트리에
    # 직접 붙이는 것을 권장.

    # === SSE 실행 ===

    @harness_router.post("/execute")
    async def execute_pipeline(req: ExecuteRequest):
        from ..core.pipeline import Pipeline
        from ..core.state import PipelineState
        from ..events.emitter import EventEmitter
        from ..events.types import DoneEvent, ErrorEvent, event_to_dict

        config = HarnessConfig(
            provider=req.provider, model=req.model,
            temperature=req.temperature, system_prompt=req.system_prompt,
            disabled_stages=set(req.disabled_stages),
            artifacts=req.artifacts,
        )
        emitter = EventEmitter()
        pipeline = Pipeline.from_config(config, emitter)
        state = PipelineState(user_input=req.text)

        async def gen():
            task = asyncio.create_task(pipeline.run(state))
            async for event in emitter.stream():
                yield f"data: {json.dumps(event_to_dict(event), ensure_ascii=False)}\n\n"
            await task

        return StreamingResponse(gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    # === WebSocket 실행 ===

    @harness_router.websocket("/ws/{session_id}")
    async def ws_execute(websocket: WebSocket, session_id: str):
        from ..core.pipeline import Pipeline
        from ..core.state import PipelineState
        from ..events.emitter import EventEmitter
        from ..events.types import (
            StageEnterEvent, StageExitEvent, MessageEvent,
            ToolCallEvent, ToolResultEvent, EvaluationEvent,
            MetricsEvent, ErrorEvent, DoneEvent, event_to_dict,
        )

        await websocket.accept()

        try:
            while True:
                data = await websocket.receive_json()
                if data.get("type") != "execute":
                    continue

                from ..providers import get_default_provider, get_default_model
                user_input = data.get("input", "")
                provider = data.get("provider") or get_default_provider()
                model = data.get("model") or get_default_model(provider)
                disabled = set(data.get("disabled_stages", []))

                config = HarnessConfig(provider=provider, model=model, disabled_stages=disabled)
                emitter = EventEmitter()
                pipeline = Pipeline.from_config(config, emitter)
                state = PipelineState(user_input=user_input)

                async def run():
                    try:
                        await pipeline.run(state)
                    except Exception as e:
                        # 원본 예외 트레이스를 그대로 호스트로 흘리지 않는다 — 내부 로그는 남기고
                        # 클라이언트에게는 ErrorCategory 기반 메시지와 타입만 전달.
                        from ..errors import HarnessError, ErrorCategory
                        logger.exception("[WS] pipeline run failed")
                        if isinstance(e, HarnessError):
                            msg = e.message or str(e.__class__.__name__)
                            category = getattr(e, "category", ErrorCategory.UNKNOWN).value
                        else:
                            msg = "Pipeline execution failed"
                            category = ErrorCategory.UNKNOWN.value
                        await emitter.emit(ErrorEvent(
                            message=msg,
                            error_type=e.__class__.__name__,
                            category=category,
                        ))
                        await emitter.emit(DoneEvent(final_output="", success=False))

                task = asyncio.create_task(run())

                async for event in emitter.stream():
                    try:
                        await websocket.send_json(_ws_event(event))
                    except Exception as e:
                        logger.debug("[WS] send_json 실패 (client disconnected?): %s", e)
                        break

                await task

        except WebSocketDisconnect:
            pass

    # WS event converter
    def _ws_event(event) -> dict:
        from ..events.types import (
            StageEnterEvent, StageExitEvent, MessageEvent,
            ToolCallEvent, ToolResultEvent, EvaluationEvent,
            MetricsEvent, ErrorEvent, DoneEvent, event_to_dict,
        )
        if isinstance(event, StageEnterEvent):
            return {"type": "stage.enter", "stage": event.stage_name, "stage_id": event.stage_id, "timestamp": event.timestamp,
                    "data": {"phase": event.phase, "step": event.step, "total": event.total, "bypassed": event.description == "bypassed"}}
        if isinstance(event, StageExitEvent):
            return {"type": "stage.exit", "stage": event.stage_name, "stage_id": event.stage_id, "timestamp": event.timestamp, "data": event.output or {}}
        if isinstance(event, MessageEvent):
            return {"type": "text.delta", "stage": "LLM", "timestamp": event.timestamp, "data": {"text": event.text}}
        if isinstance(event, ToolCallEvent):
            return {"type": "tool.call", "stage": "Execute", "timestamp": event.timestamp, "data": {"tool_name": event.tool_name, "tool_input": event.tool_input}}
        if isinstance(event, ToolResultEvent):
            return {"type": "tool.result", "stage": "Execute", "timestamp": event.timestamp, "data": {"tool_name": event.tool_name, "result": event.result[:500]}}
        if isinstance(event, EvaluationEvent):
            return {"type": "evaluation", "stage": "Validate", "timestamp": event.timestamp, "data": {"score": event.score, "verdict": event.verdict}}
        if isinstance(event, MetricsEvent):
            return {"type": "pipeline.metrics", "timestamp": event.timestamp, "data": {"duration_ms": event.duration_ms, "total_tokens": event.total_tokens, "cost_usd": event.cost_usd, "model": event.model}}
        if isinstance(event, DoneEvent):
            return {"type": "pipeline.complete", "timestamp": event.timestamp, "data": {"success": event.success, "text": event.final_output}}
        if isinstance(event, ErrorEvent):
            return {"type": "pipeline.error", "timestamp": event.timestamp, "data": {"message": event.message}}
        return event_to_dict(event)

except ImportError:
    harness_router = None
