"""장기실행(multi-session) 메모리 — progress artifact + 영속 SessionStore.

세션 간 작업상태 인계를 책임진다 (s06_context 의 *세션 내* 압축과 보완 관계).
엔진 코어는 인터페이스만 알고, 플랫폼은 entry_points 로 자기 백엔드를 끼운다.
"""

from .progress import ProgressStatus, ProgressItem, ProgressLog
from .recall import (
    Priority,
    RecallItem,
    RecallSet,
    DEFAULT_RECALL_CAP,
)
from .dedupe import content_fingerprint, dedupe
from .refine import (
    RefinedMemory,
    MemoryRefiner,
    ExtractiveRefiner,
    redact_sensitive,
    refine_message,
)
from .repro import (
    ReproBundle,
    build_repro_bundle,
    config_fingerprint,
)
from .activity import (
    ActivityEvent,
    ActivityFeed,
    activity_from_message,
)
from .lifecycle import (
    LifecyclePhase,
    LifecycleStep,
    ProjectLifecycle,
)
from .store import (
    SessionStore,
    InMemorySessionStore,
    FileSessionStore,
    register_session_store,
    get_session_store,
    available_session_stores,
    save_session,
    load_session,
    attach_progress,
    read_progress,
)
from .memory_store import (
    MemoryEntry,
    MemoryStore,
    InMemoryMemoryStore,
    register_memory_store,
    get_memory_store,
    has_memory_store,
    available_memory_stores,
    register_memory_extractor,
    get_memory_extractor,
)

__all__ = [
    "ProgressStatus",
    "ProgressItem",
    "ProgressLog",
    "Priority",
    "RecallItem",
    "RecallSet",
    "DEFAULT_RECALL_CAP",
    "content_fingerprint",
    "dedupe",
    "RefinedMemory",
    "MemoryRefiner",
    "ExtractiveRefiner",
    "redact_sensitive",
    "refine_message",
    "ReproBundle",
    "build_repro_bundle",
    "config_fingerprint",
    "ActivityEvent",
    "ActivityFeed",
    "activity_from_message",
    "LifecyclePhase",
    "LifecycleStep",
    "ProjectLifecycle",
    "SessionStore",
    "InMemorySessionStore",
    "FileSessionStore",
    "register_session_store",
    "get_session_store",
    "available_session_stores",
    "save_session",
    "load_session",
    "attach_progress",
    "read_progress",
    "MemoryEntry",
    "MemoryStore",
    "InMemoryMemoryStore",
    "register_memory_store",
    "get_memory_store",
    "has_memory_store",
    "available_memory_stores",
    "register_memory_extractor",
    "get_memory_extractor",
]
