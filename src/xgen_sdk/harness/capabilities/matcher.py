"""
CapabilityMatcher — intent → CapabilitySpec 매칭

3단계 fallback:
  1. exact_tag  — 태그/이름/alias 정확 일치 (가장 빠름)
  2. keyword    — 설명/태그 문자열 포함 (토큰 기반)
  3. llm        — LLM judge (Matcher는 llm_fn만 받고 구현은 외부 주입)

라이브러리는 LLM 직접 호출하지 않음. 호출자(s04_tool 등)가 llm_fn을 넘김.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional

from .registry import CapabilityRegistry, get_default_registry
from .schema import CapabilityMatch, CapabilitySpec

logger = logging.getLogger("harness.capabilities.matcher")


class MatchStrategy(str, Enum):
    EXACT_TAG = "exact_tag"
    KEYWORD = "keyword"
    LLM = "llm"
    AUTO = "auto"          # 순차 fallback


_TOKEN_RE = re.compile(r"[A-Za-z가-힣0-9]+")


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "")}


@dataclass
class _Candidate:
    spec: CapabilitySpec
    score: float
    strategy: str
    reason: str


class CapabilityMatcher:
    """
    intent(자연어 또는 태그) → CapabilitySpec 후보 리스트.

    llm_fn 시그니처:
        async def llm_fn(intent: str, candidates: list[CapabilitySpec]) -> list[tuple[str, float]]
        → [(capability_name, score), ...]
    """

    def __init__(
        self,
        registry: Optional[CapabilityRegistry] = None,
        *,
        llm_fn: Optional[Callable[[str, list[CapabilitySpec]], Awaitable[list[tuple[str, float]]]]] = None,
        min_score: float = 0.3,
    ) -> None:
        self.registry = registry or get_default_registry()
        self.llm_fn = llm_fn
        self.min_score = min_score

    # ---------- 공용 진입점 ----------

    def match(
        self,
        intent: str,
        *,
        limit: int = 5,
        strategy: MatchStrategy = MatchStrategy.AUTO,
    ) -> list[CapabilityMatch]:
        """동기 매칭 — exact_tag + keyword까지. LLM 단계는 amatch()."""
        if not intent:
            return []

        candidates: list[_Candidate] = []

        if strategy in (MatchStrategy.AUTO, MatchStrategy.EXACT_TAG):
            candidates.extend(self._match_exact(intent))

        # 정확 매칭으로 충분히 찾았으면 그대로 반환
        if candidates and strategy == MatchStrategy.AUTO and len(candidates) >= limit:
            return self._finalize(candidates, limit)

        if strategy in (MatchStrategy.AUTO, MatchStrategy.KEYWORD):
            existing = {c.spec.name for c in candidates}
            candidates.extend(c for c in self._match_keyword(intent) if c.spec.name not in existing)

        return self._finalize(candidates, limit)

    async def amatch(
        self,
        intent: str,
        *,
        limit: int = 5,
        strategy: MatchStrategy = MatchStrategy.AUTO,
    ) -> list[CapabilityMatch]:
        """비동기 매칭 — LLM fallback 포함."""
        # 1~2단계는 동기
        results = self.match(intent, limit=limit * 2, strategy=strategy)

        # 충분하면 반환
        if strategy in (MatchStrategy.EXACT_TAG, MatchStrategy.KEYWORD):
            return results[:limit]

        # LLM 단계
        if self.llm_fn is None:
            return results[:limit]

        if len(results) >= limit and results[0].score >= 0.7:
            return results[:limit]

        # LLM으로 재랭킹
        pool = results if results else [CapabilityMatch(spec=s, score=0.0, strategy="seed") for s in self.registry.list_all()[:30]]
        specs = [m.spec for m in pool]
        try:
            llm_scores = await self.llm_fn(intent, specs)
        except Exception as e:
            logger.debug("llm_fn matcher fallback: %s", e)
            return results[:limit]

        score_map = dict(llm_scores)
        merged: list[_Candidate] = []
        for m in pool:
            llm_score = score_map.get(m.spec.name, 0.0)
            combined = max(m.score, llm_score)
            if combined < self.min_score:
                continue
            strategy_label = "llm" if llm_score > m.score else m.strategy
            reason = f"llm={llm_score:.2f}" if llm_score > m.score else m.reason
            merged.append(_Candidate(m.spec, combined, strategy_label, reason))

        return self._finalize(merged, limit)

    # ---------- 단계별 구현 ----------

    def _match_exact(self, intent: str) -> list[_Candidate]:
        """이름/태그/alias 정확 일치"""
        query = intent.strip().lower()
        out: list[_Candidate] = []

        # 이름 정확 일치
        spec = self.registry.get(query)
        if spec:
            out.append(_Candidate(spec, 1.0, "exact_tag", f"name match: {query}"))

        # 태그 일치
        tag_hits = self.registry.find_by_tag(query)
        for s in tag_hits:
            if any(c.spec.name == s.name for c in out):
                continue
            out.append(_Candidate(s, 0.9, "exact_tag", f"tag match: {query}"))

        # 카테고리 일치
        cat_hits = self.registry.find_by_category(query)
        for s in cat_hits:
            if any(c.spec.name == s.name for c in out):
                continue
            out.append(_Candidate(s, 0.7, "exact_tag", f"category match: {query}"))

        return out

    def _match_keyword(self, intent: str) -> list[_Candidate]:
        """
        토큰 기반 유사도 — 한국어 조사 대응 위해 부분일치도 허용.

        매칭 조건 (쿼리 토큰 기준):
        - 정확 일치 (1.0)
        - 부분 일치: 짧은 쪽이 긴 쪽에 포함되고, 최소 2자 이상 (0.7)
        """
        query_tokens = _tokenize(intent)
        if not query_tokens:
            return []

        out: list[_Candidate] = []
        for spec in self.registry.list_all():
            haystack_tokens = _tokenize(
                " ".join(
                    [
                        spec.name,
                        spec.description,
                        spec.category,
                        " ".join(spec.tags),
                        " ".join(spec.aliases),
                    ]
                )
            )
            if not haystack_tokens:
                continue

            matched: list[tuple[str, str, float]] = []
            for qt in query_tokens:
                if len(qt) < 2:
                    continue
                best: Optional[tuple[str, float]] = None
                for ht in haystack_tokens:
                    if qt == ht:
                        best = (ht, 1.0)
                        break
                    if len(ht) < 2:
                        continue
                    # 부분 일치: 짧은 쪽이 긴 쪽에 포함
                    short, long_ = (qt, ht) if len(qt) <= len(ht) else (ht, qt)
                    if len(short) >= 2 and short in long_:
                        ratio = len(short) / len(long_)
                        if ratio < 0.5:
                            continue
                        score = 0.6 + 0.4 * ratio
                        if best is None or score > best[1]:
                            best = (ht, score)
                if best is not None:
                    matched.append((qt, best[0], best[1]))

            if not matched:
                continue

            # 쿼리 토큰 중 매칭된 비율 × 평균 매칭 강도
            coverage = len(matched) / max(len(query_tokens), 1)
            strength = sum(m[2] for m in matched) / len(matched)
            score = coverage * strength

            if score < self.min_score:
                continue

            reason = "keyword: " + ", ".join(f"{q}↔{h}({s:.2f})" for q, h, s in matched[:3])
            out.append(_Candidate(spec, score, "keyword", reason))

        return out

    # ---------- 마무리 ----------

    def _finalize(self, candidates: list[_Candidate], limit: int) -> list[CapabilityMatch]:
        """중복 제거 + 정렬 + 변환"""
        best: dict[str, _Candidate] = {}
        for c in candidates:
            prev = best.get(c.spec.name)
            if prev is None or c.score > prev.score:
                best[c.spec.name] = c

        ordered = sorted(best.values(), key=lambda x: x.score, reverse=True)
        return [
            CapabilityMatch(spec=c.spec, score=c.score, strategy=c.strategy, reason=c.reason)
            for c in ordered[:limit]
        ]

    # ---------- 배치 ----------

    def match_many(
        self,
        intents: list[str],
        *,
        limit_per_intent: int = 3,
    ) -> dict[str, list[CapabilityMatch]]:
        """여러 intent를 한번에 매칭 — 각각 독립 결과"""
        return {intent: self.match(intent, limit=limit_per_intent) for intent in intents}
