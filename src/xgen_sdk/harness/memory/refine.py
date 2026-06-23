"""Message → 정제 장기기억 (v1.24).

비전(L0/L5): "출발은 언제나 메시지다" + "허접한 프롬프트도 결과물이 훌륭하면 결과물에
맞게 정제 → 프라이버시 공포 완화" + "메시지 + 정제 장기기억 = 재현성".

엔진은 정제의 **메커니즘·자료구조**만 제공한다(무하드코딩):
  - raw 메시지 원문을 **저장하지 않는다**(프라이버시 floor) — 정제된 intent + outcome 만.
  - 민감정보(이메일/전화/키/토큰)는 결정적 redaction 으로 마스킹 후에만 보관.
  - raw→정제 NL 변환은 pluggable `MemoryRefiner` 에 위임. 기본은 LLM 없이 결정적
    추출(`ExtractiveRefiner`) — 테스트·오프라인에서 동일 입력→동일 출력.
  - provenance(run_id/thread_id/spec_ref)를 실어 State Spine·spec.json 과 꿰맨다(재현성).

장기 기억 매체는 md 로 충분 — `RefinedMemory.to_markdown()` 이 그 표면.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional, Protocol, runtime_checkable

# 결정적 redaction 패턴 — 프라이버시 floor. 순서·치환토큰 고정(같은 입력→같은 출력).
_REDACTORS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "⟪email⟫"),
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"), "⟪api_key⟫"),
    (re.compile(r"\b(?:ghp|gho|glpat)[-_][A-Za-z0-9]{16,}\b"), "⟪token⟫"),
    (re.compile(r"\b(?:\+?\d{1,3}[ -]?)?(?:0\d{1,2}[ -]?)?\d{3,4}[ -]?\d{4}\b"), "⟪phone⟫"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "⟪hex⟫"),
]


def redact_sensitive(text: str) -> tuple[str, int]:
    """민감정보를 결정적으로 마스킹. 반환 (마스킹된 텍스트, 치환 횟수)."""
    if not text:
        return "", 0
    out = text
    count = 0
    for pat, repl in _REDACTORS:
        out, n = pat.subn(repl, out)
        count += n
    return out, count


@runtime_checkable
class MemoryRefiner(Protocol):
    """raw 메시지 + 결과 → 정제 필드. 구현은 LLM/휴리스틱 자유(엔진은 계약만)."""

    def refine(self, raw_message: str, result: str, context: dict[str, Any]) -> dict[str, Any]:
        """반환 dict: {intent: str, outcome: str, tags: list[str]}. 원문 누출 금지."""
        ...


class ExtractiveRefiner:
    """기본 refiner — LLM 없이 결정적 추출. 원문 저장 안 함(redaction 후 요약)."""

    def __init__(self, *, intent_chars: int = 240, outcome_chars: int = 600, max_tags: int = 6):
        self.intent_chars = intent_chars
        self.outcome_chars = outcome_chars
        self.max_tags = max_tags

    @staticmethod
    def _first_sentence(text: str, cap: int) -> str:
        t = " ".join((text or "").split())
        m = re.search(r"(.+?[.!?。…])(\s|$)", t)
        s = m.group(1) if m else t
        return s[:cap].rstrip()

    def _tags(self, text: str) -> list[str]:
        # 결정적 키워드 추출 — 4자 이상 토큰 빈도 상위, 등장 순서로 tie-break.
        words = re.findall(r"[A-Za-z가-힣][A-Za-z0-9가-힣_]{3,}", (text or "").lower())
        freq: dict[str, int] = {}
        order: dict[str, int] = {}
        for i, w in enumerate(words):
            freq[w] = freq.get(w, 0) + 1
            order.setdefault(w, i)
        ranked = sorted(freq, key=lambda w: (-freq[w], order[w]))
        return ranked[: self.max_tags]

    def refine(self, raw_message: str, result: str, context: dict[str, Any]) -> dict[str, Any]:
        red_msg, _ = redact_sensitive(raw_message)
        red_res, _ = redact_sensitive(result)
        return {
            "intent": self._first_sentence(red_msg, self.intent_chars),
            "outcome": red_res[: self.outcome_chars].rstrip(),
            "tags": self._tags(f"{red_msg} {red_res}"),
        }


@dataclass
class RefinedMemory:
    """정제된 장기기억 1건. raw 원문 미보유(프라이버시 floor)."""
    memory_id: str
    intent: str = ""                 # 정제된 의도(원문 아님)
    outcome: str = ""                # 결과 요약(redacted)
    tags: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)  # run_id/thread_id/spec_ref 등
    redaction_count: int = 0          # 마스킹된 민감정보 수(프라이버시 가시화)
    source: str = "message"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RefinedMemory":
        return cls(
            memory_id=str(data["memory_id"]),
            intent=str(data.get("intent", "")),
            outcome=str(data.get("outcome", "")),
            tags=list(data.get("tags", []) or []),
            provenance=dict(data.get("provenance", {}) or {}),
            redaction_count=int(data.get("redaction_count", 0) or 0),
            source=str(data.get("source", "message")),
        )

    def to_markdown(self) -> str:
        """장기기억 매체(md) 표면. 사람·다른 멤버가 보는 '무엇을·왜·어떻게'."""
        lines = [f"## {self.memory_id}", "", f"**의도:** {self.intent}", "",
                 f"**결과:** {self.outcome}"]
        if self.tags:
            lines += ["", f"**태그:** {', '.join(self.tags)}"]
        if self.provenance:
            prov = " · ".join(f"{k}={v}" for k, v in self.provenance.items())
            lines += ["", f"**출처:** {prov}"]
        if self.redaction_count:
            lines += ["", f"_민감정보 {self.redaction_count}건 마스킹됨(프라이버시)_"]
        return "\n".join(lines)


def refine_message(
    raw_message: str,
    result: str,
    *,
    memory_id: str,
    refiner: Optional[MemoryRefiner] = None,
    provenance: Optional[dict[str, Any]] = None,
    context: Optional[dict[str, Any]] = None,
) -> RefinedMemory:
    """raw 메시지 + 결과 → RefinedMemory. 원문은 저장하지 않는다.

    refiner 미지정 시 결정적 `ExtractiveRefiner`. redaction_count 는 raw+result 양쪽의
    마스킹 합(프라이버시 가시화). provenance 로 run/thread/spec 과 꿰매 재현성 확보.
    """
    r = refiner or ExtractiveRefiner()
    fields = r.refine(raw_message, result, dict(context or {}))
    _, rc_msg = redact_sensitive(raw_message)
    _, rc_res = redact_sensitive(result)
    return RefinedMemory(
        memory_id=memory_id,
        intent=str(fields.get("intent", "")),
        outcome=str(fields.get("outcome", "")),
        tags=list(fields.get("tags", []) or []),
        provenance=dict(provenance or {}),
        redaction_count=rc_msg + rc_res,
        source="message",
    )
