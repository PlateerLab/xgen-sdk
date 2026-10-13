"""통제 정책 로그(``policy_event_log``) 적재: core · workflow · documents 가 같은 함수를 쓴다.

세 서비스가 같은 PostgreSQL 을 쓰므로 HTTP 없이 바로 넣는다. 적재는 fire-and-forget: 실패해도 가리기 ·
막기 · 응답을 막지 않는다. 관리자 화면 [통제 정책 관리 > 통제 정책 로그] 의 단일 출처다.

    from xgen_sdk.control_policy import build_detections, log_policy_event

    log_policy_event(
        app_db, source=SOURCE_CHAT_INPUT, policy_type="pii",
        policy_ids=[12], policy_names=["주민등록번호"], total_detected=1,
        detections=build_detections([{"policy_id": 12, "policy_name": "주민등록번호", "count": 1,
                                      "samples": [{"original": "123456-1234567", "masked": "****"}]}]),
        workflow_id="...", interaction_id="...", user_id=42,
    )
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("xgen-sdk.policy-event-log")

# 발생지: 관리자 화면의 [전체 발생지] 필터와 1:1 (core adminGovernancePolicyEventController._ALLOWED_SOURCES).
SOURCE_WORKFLOW_AGENT = "workflow_agent"
SOURCE_WORKFLOW_EXECUTION_META = "workflow_execution_meta"
SOURCE_WORKFLOW_GUARDER = "workflow_guarder"
SOURCE_WORKFLOW_RAG = "workflow_rag"
SOURCE_WORKFLOW_TOOL = "workflow_tool"
SOURCE_DOCUMENT_UPLOAD = "document_upload"
SOURCE_DOCUMENT_OCR_SUPPLEMENT = "document_ocr_supplement"
#: 통제 정책 [세부설정] 의 적용 자리.
SOURCE_CHAT_INPUT = "chat_input"
SOURCE_CHAT_UPLOAD = "chat_upload"
SOURCE_FILESTORE_UPLOAD = "filestore_upload"

# 감사 목적이라 원문 전체는 남기지 않는다(사용자 합의): 샘플 글은 100자, 정책당 샘플 10개까지.
SAMPLE_TEXT_CAP = 100
SAMPLES_PER_POLICY_CAP = 10

_INSERT_SQL = """
INSERT INTO policy_event_log (
    source, policy_type,
    policy_ids, policy_names, total_detected, detections,
    workflow_id, execution_id, interaction_id, node_id,
    collection_name, collection_display_name, document_name,
    user_id, username, session_id, metadata
) VALUES (
    %s, %s,
    %s, %s, %s, %s::jsonb,
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s::jsonb
)
"""


def _cap_text(value: Any) -> str:
    if value is None:
        return ""
    s = str(value)
    return s if len(s) <= SAMPLE_TEXT_CAP else s[:SAMPLE_TEXT_CAP] + "…"


def build_detections(entries: Iterable[Dict[str, Any]]) -> str:
    """탐지 상세 → 표준 JSON 문자열.

    entry: ``{"policy_id", "policy_name", "count"(없으면 샘플 수), "samples": [{"original", "masked"}],
    "enforced"(없으면 참)}``. ``enforced`` 는 그 건이 **실제로 가려졌거나 막혔는지**: 탐지만 한 건을 적용된
    것처럼 보이면 관리자가 "껐는데 계속 가려진다" 고 오독한다.
    """
    normalized: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        samples = []
        for sample in (entry.get("samples") or [])[:SAMPLES_PER_POLICY_CAP]:
            if not isinstance(sample, dict):
                continue
            samples.append({"original": _cap_text(sample.get("original")), "masked": _cap_text(sample.get("masked"))})
        count = entry.get("count")
        if not isinstance(count, int):
            count = len(entry.get("samples") or [])
        normalized.append({
            "policy_id": entry.get("policy_id"),
            "policy_name": entry.get("policy_name") or "",
            "count": count,
            "samples": samples,
            "enforced": bool(entry.get("enforced", True)),
        })
    return json.dumps(normalized, ensure_ascii=False)


def log_policy_event(
    app_db: Any,
    *,
    source: str,
    policy_type: str,
    policy_ids: Optional[List[int]] = None,
    policy_names: Optional[List[str]] = None,
    total_detected: int = 0,
    detections: Optional[str] = None,
    workflow_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    interaction_id: Optional[str] = None,
    node_id: Optional[str] = None,
    collection_name: Optional[str] = None,
    collection_display_name: Optional[str] = None,
    document_name: Optional[str] = None,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    session_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """한 줄 넣는다. ``app_db`` 는 ``AppDatabaseManager``(``config_db_manager`` 를 가진 것) 또는 그 안의 DB 관리자.

    성공 여부를 돌려주지만 부르는 쪽이 다룰 필요는 없다(fire-and-forget).
    """
    try:
        db = getattr(app_db, "config_db_manager", None) or app_db
        if db is None:
            return False
        db.execute_update_delete(
            _INSERT_SQL,
            (
                source,
                policy_type,
                policy_ids or [],
                policy_names or [],
                int(total_detected or 0),
                detections if detections is not None else "[]",
                workflow_id,
                execution_id,
                interaction_id,
                node_id,
                collection_name,
                collection_display_name,
                document_name,
                user_id,
                username,
                session_id,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        return True
    except Exception as exc:  # noqa: BLE001, 기록 실패가 본 흐름을 막지 않는다
        logger.warning("policy_event_log INSERT failed (source=%s type=%s): %s", source, policy_type, exc)
        return False


def log_result(app_db: Any, found: Any, *, source: str, enforced: bool = True, **context: Any) -> int:
    """:class:`~xgen_sdk.control_policy.rules.MaskingResult` 의 탐지를 종류마다 한 줄씩. 넣은 줄 수.

    ``context`` 는 :func:`log_policy_event` 의 나머지 칸(workflow_id · user_id · document_name · metadata …).
    """
    if found is None or not getattr(found, "detections", None):
        return 0
    written = 0
    for policy_type, entries in found.by_type().items():
        if not policy_type:
            continue
        ok = log_policy_event(
            app_db,
            source=source,
            policy_type=policy_type,
            policy_ids=[e["policy_id"] for e in entries if e.get("policy_id") is not None],
            policy_names=[e.get("policy_name") or "" for e in entries],
            total_detected=sum(int(e.get("count") or 0) for e in entries),
            detections=build_detections([{**e, "enforced": enforced} for e in entries]),
            **context,
        )
        written += 1 if ok else 0
    return written
