"""
PersistStrategy — s09_finalize 의 'persist' strategy (v1.0).

구 s10_save stage 격하 흡수. 실행 결과를 DB(harness_execution_log)에 저장.
ServiceProvider.database 가 없으면 graceful skip.

박제 풀기:
  - 테이블명·필드 길이 cap 모두 stage_param 으로 override 가능
  - PERSIST_DEFAULTS dict 모듈 노출 — register_persist_defaults() 로 외부 조정
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("harness.stage.finalize.persist")


# 모듈 상수 — 박제 0. stage_param 또는 register_persist_defaults() 로 override.
PERSIST_DEFAULTS: dict[str, Any] = {
    "table_name": "harness_execution_log",
    "input_text_cap": 5_000,
    "output_text_cap": 50_000,
    "feedback_text_cap": 2_000,
}


def register_persist_defaults(**kwargs: Any) -> None:
    """persist strategy 의 기본값 override. 외부 작업자가 자기 도메인에 맞춰 조정."""
    for k, v in kwargs.items():
        if k in PERSIST_DEFAULTS:
            PERSIST_DEFAULTS[k] = v


async def persist_execution_record(state, get_param) -> dict:
    """state 의 실행 결과를 record dict 로 만들고 services.database 에 insert.

    Args:
        state: PipelineState
        get_param: stage 의 get_param 메서드 (3-level fallback)
    """
    table_name = get_param("table_name", state, PERSIST_DEFAULTS["table_name"])
    input_cap = int(get_param("input_text_cap", state, PERSIST_DEFAULTS["input_text_cap"]))
    output_cap = int(get_param("output_text_cap", state, PERSIST_DEFAULTS["output_text_cap"]))

    record = {
        "execution_id": state.execution_id,
        "workflow_id": state.workflow_id,
        "workflow_name": state.workflow_name,
        "user_id": state.user_id,
        "interaction_id": state.interaction_id,
        "status": "completed",
        "input_text": (state.user_input or "")[:input_cap],
        "output_text": (state.final_output or state.last_assistant_text or "")[:output_cap],
        "metrics": {
            "duration_ms": state.elapsed_ms,
            "input_tokens": state.token_usage.input_tokens,
            "output_tokens": state.token_usage.output_tokens,
            "total_tokens": state.token_usage.total,
            "cost_usd": state.cost_usd,
            "llm_calls": state.llm_call_count,
            "tools_executed": state.tools_executed_count,
            "iterations": state.loop_iteration,
            "model": state.provider.model_name if state.provider else "",
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    if state.validation_score is not None:
        record["metrics"]["validation_score"] = state.validation_score

    _feedback = getattr(state, "validation_feedback", None)
    if _feedback:
        fb_cap = int(get_param("feedback_text_cap", state, PERSIST_DEFAULTS["feedback_text_cap"]))
        record["metrics"]["validation_feedback"] = str(_feedback)[:fb_cap]

    state.metadata["execution_record"] = record
    state.metadata["execution_table_name"] = table_name

    inserted_id: Optional[int] = None
    services = state.metadata.get("services")
    if services and getattr(services, "database", None):
        try:
            inserted_id = await services.database.insert_record(table_name, record)
        except Exception as e:
            logger.warning("[Finalize.persist] DB insert 실패 (graceful skip): %s", e)

    logger.info(
        "[Finalize.persist] record prepared: %s (table=%s, inserted_id=%s)",
        state.execution_id, table_name, inserted_id,
    )
    return {
        "saved": True,
        "execution_id": state.execution_id,
        "table_name": table_name,
        "inserted_id": inserted_id,
    }
