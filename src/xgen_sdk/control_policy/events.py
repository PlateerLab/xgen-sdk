"""Synchronous compatibility API for Core-owned governance evidence."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional
from uuid import UUID, uuid4

from .evidence import (
    MAX_DETECTIONS, MAX_SAMPLE_TEXT, MAX_SAMPLES, EvidenceActor, EvidenceDetection,
    EvidenceDetections, EvidenceEvent, EvidenceIngestRequest, EvidenceIngestResult,
    EvidenceSample, EvidenceSink, EvidenceWriteError, HttpEvidenceSink,
)

SOURCE_WORKFLOW_AGENT = "workflow_agent"
SOURCE_WORKFLOW_EXECUTION_META = "workflow_execution_meta"
SOURCE_WORKFLOW_GUARDER = "workflow_guarder"
SOURCE_WORKFLOW_RAG = "workflow_rag"
SOURCE_WORKFLOW_TOOL = "workflow_tool"
SOURCE_DOCUMENT_UPLOAD = "document_upload"
SOURCE_DOCUMENT_OCR_SUPPLEMENT = "document_ocr_supplement"
SOURCE_CHAT_INPUT = "chat_input"
SOURCE_CHAT_UPLOAD = "chat_upload"
SOURCE_FILESTORE_UPLOAD = "filestore_upload"

SAMPLE_TEXT_CAP = MAX_SAMPLE_TEXT
SAMPLES_PER_POLICY_CAP = MAX_SAMPLES
_SOURCES = {
    SOURCE_WORKFLOW_AGENT, SOURCE_WORKFLOW_EXECUTION_META, SOURCE_WORKFLOW_GUARDER,
    SOURCE_WORKFLOW_RAG, SOURCE_WORKFLOW_TOOL, SOURCE_DOCUMENT_UPLOAD,
    SOURCE_DOCUMENT_OCR_SUPPLEMENT, SOURCE_CHAT_INPUT, SOURCE_CHAT_UPLOAD,
    SOURCE_FILESTORE_UPLOAD,
}
_POLICY_TYPES = {"pii", "forbidden_word", "guarder"}
_ENTRY_KEYS = {"policy_id", "policy_name", "policy_type", "count", "samples", "enforced"}
_SAMPLE_KEYS = {"original", "masked"}


def _invalid() -> EvidenceWriteError:
    return EvidenceWriteError("EVIDENCE_REQUEST_INVALID")


def build_detections(entries: Iterable[Dict[str, Any]]) -> EvidenceDetections:
    """Build a typed payload while exposing only a safe legacy JSON projection."""
    if isinstance(entries, (str, bytes, dict)):
        raise _invalid()
    normalized: list[EvidenceDetection] = []
    try:
        for entry in entries:
            if len(normalized) >= MAX_DETECTIONS:
                raise _invalid()
            if not isinstance(entry, dict) or not set(entry).issubset(_ENTRY_KEYS):
                raise _invalid()
            raw_samples = entry.get("samples", [])
            if not isinstance(raw_samples, (list, tuple)) or len(raw_samples) > SAMPLES_PER_POLICY_CAP:
                raise _invalid()
            samples: list[EvidenceSample] = []
            for sample in raw_samples:
                if not isinstance(sample, dict) or not set(sample).issubset(_SAMPLE_KEYS):
                    raise _invalid()
                samples.append(EvidenceSample(sample.get("original"), sample.get("masked", "")))
            normalized.append(EvidenceDetection(
                policy_id=entry.get("policy_id"),
                policy_name=entry.get("policy_name", ""),
                count=entry.get("count", len(raw_samples)),
                samples=tuple(samples),
                enforced=entry.get("enforced", True),
            ))
    except EvidenceWriteError:
        raise
    except (TypeError, ValueError):
        raise _invalid() from None
    return EvidenceDetections(normalized)


def _db(app_db: Any) -> Any:
    return getattr(app_db, "config_db_manager", None) or app_db


def _subject_rows(app_db: Any, table: str, column: str, value: str) -> list[Any]:
    finder = getattr(app_db, "find_records_by_condition", None)
    if callable(finder):
        try:
            return list(finder(table, {column: value}, limit=2, select_columns=["id"]))
        except Exception:
            raise EvidenceWriteError("EVIDENCE_SDK_SUBJECT_LOOKUP", retryable=True) from None
    query = getattr(_db(app_db), "execute_query", None)
    if not callable(query):
        raise EvidenceWriteError("EVIDENCE_SDK_SUBJECT_LOOKUP", retryable=True)
    try:
        rows = query(f"SELECT id FROM {table} WHERE {column} = %s ORDER BY id LIMIT 2", (value,))
    except Exception:
        raise EvidenceWriteError("EVIDENCE_SDK_SUBJECT_LOOKUP", retryable=True) from None
    if rows is None:
        raise EvidenceWriteError("EVIDENCE_SDK_SUBJECT_LOOKUP", retryable=True)
    return list(rows)


def _row_id(row: Any) -> int:
    try:
        value = row.get("id") if isinstance(row, dict) else row[0]
    except (AttributeError, KeyError, IndexError, TypeError):
        raise EvidenceWriteError("EVIDENCE_SDK_SUBJECT_INVALID") from None
    if type(value) is not int or value <= 0:
        raise EvidenceWriteError("EVIDENCE_SDK_SUBJECT_INVALID")
    return value


def _resolve_subject(app_db: Any, *, subject_type: str | None, subject_id: int | None,
                     workflow_id: str | None, collection_name: str | None,
                     user_id: int | None) -> tuple[str, int]:
    if subject_type is not None or subject_id is not None:
        if subject_type not in {"workflow", "collection", "actor"} or type(subject_id) is not int or subject_id <= 0:
            raise EvidenceWriteError("EVIDENCE_SDK_SUBJECT_INVALID")
        return subject_type, subject_id
    if workflow_id:
        kind, rows = "workflow", _subject_rows(app_db, "workflow_meta", "workflow_id", workflow_id)
    elif collection_name:
        kind, rows = "collection", _subject_rows(app_db, "vector_db", "collection_name", collection_name)
    elif user_id is not None:
        return "actor", user_id
    else:
        raise EvidenceWriteError("EVIDENCE_SDK_SUBJECT_REQUIRED")
    if len(rows) != 1:
        raise EvidenceWriteError("EVIDENCE_SDK_SUBJECT_NOT_UNIQUE")
    return kind, _row_id(rows[0])


def _optional(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise _invalid()
    return value


def _event(*, source: str, policy_type: str, policy_ids: Optional[List[int]],
           policy_names: Optional[List[str]], total_detected: int, workflow_id: Optional[str],
           execution_id: Optional[str], interaction_id: Optional[str], node_id: Optional[str],
           collection_name: Optional[str], collection_display_name: Optional[str],
           document_name: Optional[str], user_id: Optional[int], username: Optional[str],
           session_id: Optional[str]) -> EvidenceEvent:
    if source not in _SOURCES or policy_type not in _POLICY_TYPES:
        raise _invalid()
    ids = [] if policy_ids is None else policy_ids
    names = [] if policy_names is None else policy_names
    if (not isinstance(ids, list) or len(ids) > 100 or any(type(v) is not int for v in ids)
            or not isinstance(names, list) or len(names) > 100
            or any(not isinstance(v, str) or len(v) > 100 for v in names)
            or type(total_detected) is not int or total_detected < 0):
        raise _invalid()
    if user_id is None or (type(user_id) is int and user_id == 0):
        actor_id = None
    elif type(user_id) is int and user_id > 0:
        actor_id = user_id
    else:
        raise _invalid()
    return EvidenceEvent(
        source, policy_type, tuple(ids), tuple(names), total_detected,
        _optional(workflow_id, 255), _optional(execution_id, 255), _optional(interaction_id, 255),
        _optional(node_id, 128), _optional(collection_name, 255), _optional(collection_display_name, 255),
        _optional(document_name, 1024), actor_id, _optional(username, 255), _optional(session_id, 128),
    )


def log_policy_event(
    app_db: Any, *, source: str, policy_type: str,
    policy_ids: Optional[List[int]] = None, policy_names: Optional[List[str]] = None,
    total_detected: int = 0, detections: Optional[EvidenceDetections] = None,
    workflow_id: Optional[str] = None, execution_id: Optional[str] = None,
    interaction_id: Optional[str] = None, node_id: Optional[str] = None,
    collection_name: Optional[str] = None, collection_display_name: Optional[str] = None,
    document_name: Optional[str] = None, user_id: Optional[int] = None,
    username: Optional[str] = None, session_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None, subject_type: str | None = None,
    subject_id: int | None = None, idempotency_key: str | None = None,
    evidence_sink: EvidenceSink | None = None,
) -> EvidenceIngestResult:
    """Synchronously retain one event in Core; failures always raise a typed error."""
    if metadata not in (None, {}):
        raise _invalid()
    if detections is None:
        typed = EvidenceDetections(())
    elif type(detections) is EvidenceDetections:
        typed = detections
    else:
        raise _invalid()
    event = _event(
        source=source, policy_type=policy_type, policy_ids=policy_ids, policy_names=policy_names,
        total_detected=total_detected, workflow_id=workflow_id, execution_id=execution_id,
        interaction_id=interaction_id, node_id=node_id, collection_name=collection_name,
        collection_display_name=collection_display_name, document_name=document_name,
        user_id=user_id, username=username, session_id=session_id,
    )
    resolved_type, resolved_id = _resolve_subject(
        app_db, subject_type=subject_type, subject_id=subject_id, workflow_id=workflow_id,
        collection_name=collection_name, user_id=event.user_id,
    )
    key = str(uuid4()) if idempotency_key is None else idempotency_key
    try:
        if not isinstance(key, str):
            raise ValueError()
        key = str(UUID(key))
    except (TypeError, ValueError):
        raise _invalid() from None
    request = EvidenceIngestRequest(key, resolved_type, resolved_id, event, typed)
    return (evidence_sink or HttpEvidenceSink()).ingest(request, actor=EvidenceActor(event.user_id, event.username))


def log_result(app_db: Any, found: Any, *, source: str, enforced: bool = True, **context: Any) -> int:
    """Retain one event per policy type; any failed event raises immediately."""
    if found is None or not getattr(found, "detections", None):
        return 0
    written = 0
    for policy_type, entries in found.by_type().items():
        if not policy_type:
            continue
        log_policy_event(
            app_db, source=source, policy_type=policy_type,
            policy_ids=[e["policy_id"] for e in entries if e.get("policy_id") is not None],
            policy_names=[e.get("policy_name") or "" for e in entries],
            total_detected=sum(int(e.get("count") or 0) for e in entries),
            detections=build_detections([{**e, "enforced": enforced} for e in entries]), **context,
        )
        written += 1
    return written
