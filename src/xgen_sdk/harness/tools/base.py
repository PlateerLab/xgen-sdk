"""
Tool ABC — 도구 인터페이스

모든 도구(MCP, 빌트인, 노드 브릿지)가 구현하는 기반 인터페이스.
Anthropic tool 정의 포맷과 호환.

v0.23.0 — MCP tool annotations (readOnlyHint / destructiveHint / idempotentHint /
openWorldHint) 1급 필드화. s07_act 이름 휴리스틱 폐기의 전제.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """도구 실행 결과"""
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_api_format(self, tool_use_id: str) -> dict:
        """Anthropic tool_result 포맷으로 변환"""
        result = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": self.content,
        }
        if self.is_error:
            result["is_error"] = True
        return result

    @classmethod
    def success(cls, content: str, **metadata) -> "ToolResult":
        return cls(content=content, is_error=False, metadata=metadata)

    @classmethod
    def error(cls, message: str) -> "ToolResult":
        return cls(content=message, is_error=True)


class Tool(ABC):
    """도구 기반 인터페이스.

    힌트 속성 4개 (MCP 표준 `annotations` 와 1:1 대응):

    - `read_only_hint`  : 외부 상태를 변경하지 않음 → s07_act 가 asyncio.gather 병렬 실행
    - `destructive_hint`: 되돌릴 수 없음 (파일 삭제·DB drop 등) → HITL / Policy Gate 트리거
    - `idempotent_hint` : 같은 입력에 여러 번 불러도 같은 결과 → 재시도 안전
    - `open_world_hint` : 외부 시스템 영향 (네트워크·파일시스템) → 샌드박스 / 감사

    서브클래스는 정확한 값을 선언해야 한다. 기본값은 *가장 안전한 쪽* — 모든 힌트를 False 로
    두어 엔진이 "안전한 줄 모름" 을 가정하고 순차 실행 · 감사 강화로 빠진다.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        ...

    @property
    def input_schema(self) -> dict:
        """JSON Schema for tool input"""
        return {"type": "object", "properties": {}}

    @property
    def category(self) -> str:
        return "general"

    # ─── v0.23.0 힌트 4종 ─────────────────────────────────────────────

    @property
    def read_only_hint(self) -> bool:
        """외부 상태를 변경하지 않는다. 기본 False (안전 쪽)."""
        return False

    @property
    def destructive_hint(self) -> bool:
        """되돌릴 수 없는 변경을 만든다. 기본 False."""
        return False

    @property
    def idempotent_hint(self) -> bool:
        """같은 입력에 같은 결과. 기본 False."""
        return False

    @property
    def open_world_hint(self) -> bool:
        """외부 세계(네트워크·파일) 에 영향. 기본 True (안전 쪽)."""
        return True

    # ─── 외부 wheel legacy 호환 — 보존 ────────────────────────────────
    # v0.23 deprecated 표기됐으나 capabilities/schema.py:83 + s07_act/stage.py:316 fallback
    # chain 이 외부 wheel 의 옛 Tool 인스턴스를 detect 할 때 이 별칭을 그대로 사용. 진짜 정신
    # 정합 = 외부 wheel 호환 보장 → 보존 (read_only_hint 가 신규 표준).

    @property
    def is_read_only(self) -> bool:
        """외부 ext legacy 별칭. 신규 코드는 read_only_hint 사용."""
        return self.read_only_hint

    # ─── 실행 ──────────────────────────────────────────────────────────

    @abstractmethod
    async def execute(self, input_data: dict) -> ToolResult:
        ...

    # ─── 직렬화 ───────────────────────────────────────────────────────

    def annotations(self) -> dict[str, bool]:
        """MCP 표준 `annotations` 블록. 힌트 4종 캡슐화."""
        return {
            "readOnlyHint": self.read_only_hint,
            "destructiveHint": self.destructive_hint,
            "idempotentHint": self.idempotent_hint,
            "openWorldHint": self.open_world_hint,
        }

    def to_api_format(self) -> dict:
        """LLM provider 표준 tool 정의 포맷 (Anthropic/OpenAI/Bedrock 공통).

        v0.24.4 — annotations 필드 제거. Anthropic API 가 unknown field 로 400
        거부하는 문제(v0.24.3 화이트리스트 안전망 외에) 의 근본 해결:
        payload 포맷 자체에서 annotations 를 빼고, s04_tool 이 별도 맵
        (`state.tool.annotations`) 으로 분리 저장한다. 힌트 조회는 s07_act /
        HITLGuard 가 그 맵 또는 Tool.annotations() 메서드를 본다.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def to_index_entry(self) -> dict:
        """Progressive Disclosure Level 1: 메타데이터만 (annotations 포함)."""
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "annotations": self.annotations(),
        }
