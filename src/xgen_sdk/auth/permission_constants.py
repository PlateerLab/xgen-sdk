"""
Permission 상수 정의

section_config.py를 대체하는 전체 permission 정의.
모든 resource:action 조합이 여기서 정의된다.
"""

# ─────────────────────────────────────────────────────────
# 전체 Permission 정의
# 형식: (resource, action, description)
# ─────────────────────────────────────────────────────────

ALL_PERMISSIONS = [
    # ── 일반 기능 ──
    # Workflow
    ("workflow", "create", "워크플로우 생성"),
    ("workflow", "read", "워크플로우 조회"),
    ("workflow", "update", "워크플로우 수정"),
    ("workflow", "delete", "워크플로우 삭제"),
    ("workflow", "execute", "워크플로우 실행"),
    ("workflow", "deploy", "워크플로우 배포"),
    ("workflow", "approve", "워크플로우 승인"),

    # Document
    ("document", "create", "문서 생성"),
    ("document", "read", "문서 조회"),
    ("document", "update", "문서 수정"),
    ("document", "delete", "문서 삭제"),
    ("document", "share", "문서 공유"),

    # Canvas
    ("canvas", "edit", "캔버스 편집"),
    ("canvas", "read", "캔버스 조회"),

    # Model
    ("model", "upload", "모델 업로드"),
    ("model", "read", "모델 조회"),
    ("model", "train", "모델 훈련"),
    ("model", "deploy", "모델 배포"),

    # MCP
    ("mcp", "install", "MCP 설치"),
    ("mcp", "manage", "MCP 관리"),

    # Prompt
    ("prompt", "create", "프롬프트 생성"),
    ("prompt", "read", "프롬프트 조회"),
    ("prompt", "update", "프롬프트 수정"),
    ("prompt", "share", "프롬프트 공유"),

    # Tool
    ("tool", "create", "도구 생성"),
    ("tool", "read", "도구 조회"),
    ("tool", "update", "도구 수정"),

    # Data
    ("data", "read", "데이터 조회"),
    ("data", "manage", "데이터 관리"),

    # ── 관리 기능 (admin 영역) ──
    # User Management
    ("admin.user", "create", "사용자 생성"),
    ("admin.user", "read", "사용자 조회"),
    ("admin.user", "update", "사용자 수정"),
    ("admin.user", "delete", "사용자 삭제"),
    ("admin.user", "approve", "사용자 승인"),

    # Group Management
    ("admin.group", "read", "그룹 조회"),
    ("admin.group", "manage", "그룹 관리"),
    ("admin.group", "update", "그룹 수정"),

    # Role Management
    ("admin.role", "read", "역할 조회"),
    ("admin.role", "manage", "역할 관리"),

    # System
    ("admin.system", "config", "시스템 설정"),
    ("admin.system", "read", "시스템 설정 조회"),
    ("admin.system", "update", "시스템 설정 수정"),
    ("admin.system", "monitor", "시스템 모니터링"),
    ("admin.system", "health", "시스템 상태 조회"),
    ("admin.system", "backup", "시스템 백업"),

    # Workflow Management (Admin)
    ("admin.workflow", "read", "워크플로우 관리 조회"),
    ("admin.workflow", "manage", "워크플로우 관리"),
    ("admin.workflow", "monitor", "워크플로우 모니터링"),

    # Chat Monitoring
    ("admin.chat", "monitor", "채팅 모니터링"),

    # Logs
    ("admin.log", "read", "로그 조회"),
    ("admin.audit", "read", "감사 로그 조회"),

    # Database / Storage
    ("admin.database", "read", "데이터베이스 조회"),
    ("admin.database", "manage", "데이터베이스 관리"),
    ("admin.storage", "read", "스토리지 조회"),
    ("admin.storage", "manage", "스토리지 관리"),

    # Backup
    ("admin.backup", "manage", "백업 관리"),

    # Security
    ("admin.security", "read", "보안 설정 조회"),
    ("admin.security", "manage", "보안 설정 관리"),

    # MCP (Admin)
    ("admin.mcp", "read", "MCP 조회"),
    ("admin.mcp", "manage", "MCP 관리"),

    # ML (Admin)
    ("admin.ml", "read", "ML 모델 조회"),
    ("admin.ml", "manage", "ML 모델 관리"),

    # Node Management
    ("admin.node", "manage", "노드 관리"),

    # Prompt (Admin)
    ("admin.prompt", "read", "프롬프트 스토어 조회"),

    # Governance
    ("admin.governance", "manage", "거버넌스 관리"),
    ("admin.governance", "review", "거버넌스 리뷰"),
    ("admin.governance", "audit", "거버넌스 감사"),
]

# ─────────────────────────────────────────────────────────
# Resource 목록 (자동 생성)
# ─────────────────────────────────────────────────────────

PERMISSION_RESOURCES = sorted(set(p[0] for p in ALL_PERMISSIONS))

# ─────────────────────────────────────────────────────────
# permission 문자열 생성 헬퍼
# ─────────────────────────────────────────────────────────

def permission_string(resource: str, action: str) -> str:
    """resource:action 형태의 permission 문자열 생성"""
    return f"{resource}:{action}"


def all_permission_strings() -> list:
    """전체 permission 문자열 목록 반환"""
    return [permission_string(r, a) for r, a, _ in ALL_PERMISSIONS]


def permissions_by_resource(resource: str) -> list:
    """특정 resource의 permission 문자열 목록 반환"""
    return [permission_string(r, a) for r, a, _ in ALL_PERMISSIONS if r == resource]


def permissions_grouped() -> dict:
    """resource별로 그룹핑된 permission 사전 반환

    Returns:
        {
            "workflow": [("create", "워크플로우 생성"), ("read", "워크플로우 조회"), ...],
            "admin.user": [("create", "사용자 생성"), ...],
            ...
        }
    """
    grouped = {}
    for resource, action, description in ALL_PERMISSIONS:
        if resource not in grouped:
            grouped[resource] = []
        grouped[resource].append((action, description))
    return grouped
