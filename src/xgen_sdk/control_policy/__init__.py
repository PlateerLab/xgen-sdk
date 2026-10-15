"""xgen_sdk.control_policy: 통제 정책(개인정보 · 금칙어)을 쓰는 서비스의 공용 부분.

관리자가 [AI 거버넌스 > 통제 정책 관리] 에 등록한 정규식 정책을 어디서든 같은 뜻으로 적용한다.

    settings   종류 둘(개인정보 · 금칙어) × 적용 자리 넷의 스위치 이름 · 기본값 · 판정
    rules      정책 행 → 규칙(전체 치환 · 부분 마스킹), 글에 적용 · 찾기 · 가릴 구간
    loader     활성 정책 + 에이전트별 예외 읽기
    files      파일 바이트 가리기(텍스트 · HTML · Office · PDF), 업로드에서 지우기
    events     통제 정책 로그(policy_event_log) 적재

서비스는 이 모듈을 쓰고, 예전 자리(documents ``masking_policy`` · workflow ``piis_applier`` 등)는 여기를
다시 내보내는 얇은 층만 남긴다: 같은 규칙의 사본이 서비스마다 갈라지지 않게.
"""
from .events import (
    SAMPLE_TEXT_CAP,
    SAMPLES_PER_POLICY_CAP,
    SOURCE_CHAT_INPUT,
    SOURCE_CHAT_UPLOAD,
    SOURCE_DOCUMENT_OCR_SUPPLEMENT,
    SOURCE_DOCUMENT_UPLOAD,
    SOURCE_FILESTORE_UPLOAD,
    SOURCE_WORKFLOW_AGENT,
    SOURCE_WORKFLOW_EXECUTION_META,
    SOURCE_WORKFLOW_GUARDER,
    SOURCE_WORKFLOW_RAG,
    SOURCE_WORKFLOW_TOOL,
    build_detections,
    log_policy_event,
    log_result,
)
from .files import (
    FORMAT_MASKERS,
    MASKING_MAX_BYTES,
    MASKING_MAX_UNZIPPED_BYTES,
    STATUS_CLEAN,
    STATUS_MASKED,
    STATUS_OFF,
    STATUS_UNMASKABLE,
    FileMasking,
    FormatMasker,
    MaskingUnsupportedError,
    detection_text,
    extension_of,
    mask_bytes,
    mask_html_document,
    mask_text,
    masker_for,
    masker_for_extension,
    strip_bytes,
    strip_file,
)
from .loader import EXCEPTIONS_TABLE, excepted_policy_ids, load_rules, load_scope_rules
from .rules import (
    DEFAULT_MASK,
    MASK_MODE_FULL,
    MASK_MODE_PARTIAL,
    SAMPLE_LIMIT,
    MaskingResult,
    MaskingRule,
    apply_rules,
    compile_rules,
    find_masked_spans,
    partial_mask,
)
from .settings import (
    FORBIDDEN_WORD,
    KINDS,
    PII,
    SCOPE_CHAT_INPUT,
    SCOPE_CHAT_UPLOAD,
    SCOPE_DEFAULTS,
    SCOPE_FILESTORE_UPLOAD,
    SCOPE_MODEL_INPUT,
    SCOPES,
    PolicyKind,
    as_bool,
    config_flag,
    kind_of,
    kinds_on,
    scope_key,
    scope_on,
    scope_settings,
)

__all__ = [
    # settings
    "PII", "FORBIDDEN_WORD", "KINDS", "PolicyKind", "kind_of",
    "SCOPE_CHAT_INPUT", "SCOPE_MODEL_INPUT", "SCOPE_CHAT_UPLOAD", "SCOPE_FILESTORE_UPLOAD",
    "SCOPES", "SCOPE_DEFAULTS", "scope_key", "scope_settings", "scope_on", "kinds_on", "config_flag", "as_bool",
    # rules
    "DEFAULT_MASK", "MASK_MODE_FULL", "MASK_MODE_PARTIAL", "SAMPLE_LIMIT",
    "MaskingRule", "MaskingResult", "partial_mask", "compile_rules", "apply_rules", "find_masked_spans",
    # loader
    "EXCEPTIONS_TABLE", "excepted_policy_ids", "load_rules", "load_scope_rules",
    # files
    "MASKING_MAX_BYTES", "MASKING_MAX_UNZIPPED_BYTES", "MaskingUnsupportedError", "FormatMasker", "FORMAT_MASKERS", "FileMasking",
    "STATUS_OFF", "STATUS_CLEAN", "STATUS_MASKED", "STATUS_UNMASKABLE",
    "extension_of", "masker_for", "masker_for_extension", "mask_text", "mask_html_document", "mask_bytes", "detection_text",
    "strip_bytes", "strip_file",
    # events
    "SOURCE_WORKFLOW_AGENT", "SOURCE_WORKFLOW_EXECUTION_META", "SOURCE_WORKFLOW_GUARDER", "SOURCE_WORKFLOW_RAG",
    "SOURCE_WORKFLOW_TOOL", "SOURCE_DOCUMENT_UPLOAD", "SOURCE_DOCUMENT_OCR_SUPPLEMENT",
    "SOURCE_CHAT_INPUT", "SOURCE_CHAT_UPLOAD", "SOURCE_FILESTORE_UPLOAD",
    "SAMPLE_TEXT_CAP", "SAMPLES_PER_POLICY_CAP", "build_detections", "log_policy_event", "log_result",
]
