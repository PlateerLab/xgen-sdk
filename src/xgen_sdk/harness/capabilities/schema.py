"""
CapabilitySpec — 기능 명세 (포터블)

라이브러리는 capability가 어디서 왔는지(xgen 노드인지, MCP 서버인지)는 알 필요 없음.
provider_kind + provider_ref로 추상화, tool_factory로 실제 Tool 인스턴스 생성.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class ProviderKind(str, Enum):
    """capability를 실제 제공하는 주체의 유형"""

    XGEN_NODE = "xgen_node"       # xgen-workflow 노드 (Adapter가 주입)
    MCP_TOOL = "mcp_tool"          # MCP 서버의 도구
    BUILTIN = "builtin"            # 라이브러리 내장
    GALLERY = "gallery"            # pip entry_points로 등록된 외부 패키지
    API = "api"                    # 직접 HTTP 호출
    DB = "db"                      # DB 쿼리 도구
    RAG = "rag"                    # RAG 검색
    CUSTOM = "custom"


@dataclass(frozen=True)
class ParamSpec:
    """capability 실행 파라미터 명세"""

    name: str
    type_hint: str                  # "str" | "int" | "float" | "bool" | "list[str]" | "dict" | "file"
    description: str = ""
    required: bool = True
    default: Any = None
    enum: Optional[list[Any]] = None      # 허용 값 제한
    source_hint: str = ""                  # "user_input" | "context.last_message" | "llm_infer" | "none"


@dataclass
class CapabilitySpec:
    """
    단일 기능(capability) 명세.

    예시:
      CapabilitySpec(
          name="retrieval.web_search",
          category="retrieval",
          description="웹에서 최신 정보 검색",
          tags=["web", "search", "news"],
          params=[
              ParamSpec("query", "str", "검색어", required=True, source_hint="user_input"),
              ParamSpec("top_k", "int", "결과 개수", required=False, default=5),
          ],
          provider_kind=ProviderKind.XGEN_NODE,
          provider_ref="web_crawler",
      )
    """

    # --- 식별 ---
    name: str                                   # 고유 ID, 네임스페이스 형식 권장: "category.action"
    category: str                                # "retrieval" | "generation" | "transform" | "io" | "decision" | ...
    description: str                             # LLM intent 매칭용 자연어 설명

    # --- 매칭 ---
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)   # 대체 이름 ("웹검색" 등)

    # --- 파라미터 ---
    params: list[ParamSpec] = field(default_factory=list)

    # --- 제공자 ---
    provider_kind: ProviderKind = ProviderKind.CUSTOM
    provider_ref: str = ""                       # 구체 참조 (노드 id, MCP tool name 등)

    # --- Tool 인스턴스화 ---
    tool_factory: Optional[Callable[[dict], Any]] = None   # (config) → Tool — Adapter가 주입
    tool_name: str = ""                          # 실제 생성될 Tool의 이름 (비어있으면 name 사용)

    # --- 제약/비용 ---
    estimated_cost_usd: float = 0.0              # 1회 실행 예상 비용
    is_read_only: bool = True                    # 읽기 전용(병렬 실행 가능)
    latency_hint_ms: int = 0                     # 예상 레이턴시

    # --- 메타 ---
    version: str = "1.0"
    extra: dict[str, Any] = field(default_factory=dict)

    def required_params(self) -> list[ParamSpec]:
        return [p for p in self.params if p.required]

    def optional_params(self) -> list[ParamSpec]:
        return [p for p in self.params if not p.required]

    def to_dict(self) -> dict:
        """직렬화 (Factory/Tool 제외)"""
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "tags": list(self.tags),
            "aliases": list(self.aliases),
            "params": [
                {
                    "name": p.name,
                    "type_hint": p.type_hint,
                    "description": p.description,
                    "required": p.required,
                    "default": p.default,
                    "enum": list(p.enum) if p.enum else None,
                    "source_hint": p.source_hint,
                }
                for p in self.params
            ],
            "provider_kind": self.provider_kind.value,
            "provider_ref": self.provider_ref,
            "tool_name": self.tool_name or self.name,
            "estimated_cost_usd": self.estimated_cost_usd,
            "is_read_only": self.is_read_only,
            "latency_hint_ms": self.latency_hint_ms,
            "version": self.version,
            "extra": dict(self.extra),
        }


@dataclass
class CapabilityMatch:
    """매칭 결과 — Matcher가 반환"""

    spec: CapabilitySpec
    score: float                                  # 0.0~1.0
    strategy: str                                 # "exact_tag" | "keyword" | "embedding" | "llm"
    reason: str = ""
