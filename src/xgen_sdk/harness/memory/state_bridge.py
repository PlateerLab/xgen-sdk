"""StateProvider/StateRecorder 참조 구현 — memory 모듈을 stateful loop seam 에 연결.

Provider 는 호출마다 bounded(cap·char_budget) 뷰를 재계산(내부 무축적), Recorder 는
seq(int)만 보유하고 매 회차 1건을 persist 콜백으로 위임(프로세스 무축적). 둘 다 DB
무관 — 소스/persist 는 이식측 콜백. 서브에이전트별 새 인스턴스라 누수가 구조적으로 없다.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Sequence, Union

from .recall import RecallSet
from .refine import RefinedMemory, refine_message
from .activity import activity_from_message

logger = logging.getLogger("harness.memory.state_bridge")

_RecallSrc = Union[RecallSet, Callable[[Any], Optional[RecallSet]], None]
_RefinedSrc = Union[Sequence[RefinedMemory], Callable[[Any], Sequence[RefinedMemory]], None]


class MemoryStateProvider:
    """정제 장기기억 + 작업기억 → bounded state 뷰(markdown). 누적 없음."""

    def __init__(
        self,
        *,
        recall: _RecallSrc = None,
        refined: _RefinedSrc = None,
        prior_lessons: _RefinedSrc = None,
        max_recall: int = 8,
        max_refined: int = 5,
        max_lessons: int = 3,
        char_budget: int = 2000,
        item_char_cap: int = 400,
    ) -> None:
        self._recall = recall
        self._refined = refined
        # 런간 학습: 과거 실행에서 persist 된 실패 교훈(cross-run). 이번 런 loop_lessons 로 슬롯을
        # 못 채운 만큼만 보강 → 같은 실수를 실행을 넘어 반복하지 않는다. RefinedMemory 형상.
        self._prior_lessons = prior_lessons
        self.max_recall = max_recall
        self.max_refined = max_refined
        self.max_lessons = max_lessons
        self.char_budget = char_budget
        # 단일 항목이 전체 budget 을 삼키지 않도록 항목별 상한(회상 content 가 가장 큼).
        self.item_char_cap = item_char_cap

    def get_state_view(self, state) -> Optional[str]:
        parts: list[str] = []
        # Reflexion: 최근 실패 교훈 — 이번 런(in-run) 우선 + 과거 런(cross-run resume)으로 슬롯 보강.
        # in-run loop_lessons 는 dict, prior_lessons 는 RefinedMemory — 둘 다 intent/outcome/memory_id.
        inrun = (getattr(state, "metadata", None) or {}).get("loop_lessons") or []
        prior = self._resolve(self._prior_lessons, state) or []

        def _f(x, k):
            return (x.get(k) if isinstance(x, dict) else getattr(x, k, None)) or ""

        merged: list[tuple[str, str]] = []
        seen: set = set()
        for l in list(inrun)[-self.max_lessons:] + list(prior):
            if len(merged) >= self.max_lessons:
                break
            key = _f(l, "memory_id") or _f(l, "intent")
            if key and key in seen:
                continue
            seen.add(key)
            merged.append((_f(l, "intent"), _f(l, "outcome")))
        if merged:
            parts.append("### Recent attempt lessons (avoid repeating mistakes)")
            for _intent, _outcome in merged:
                line = f"- {_intent}".rstrip()
                if _outcome:
                    line += f" → {_outcome}"
                parts.append(line)
        refined = self._resolve(self._refined, state) or []
        if refined:
            parts.append("### Long-term memory (refined)")
            for m in list(refined)[: self.max_refined]:
                line = f"- {m.intent}".rstrip()
                if m.outcome:
                    line += f" → {m.outcome}"
                parts.append(line)
        recall = self._resolve(self._recall, state)
        if recall is not None:
            ranked = recall.ranked()[: self.max_recall]
            if ranked:
                parts.append("### Working memory")
                for it in ranked:
                    parts.append(f"- {self._cap_item(it.content)}".rstrip())
        view = "\n".join(parts).strip()
        if not view:
            return None
        if len(view) > self.char_budget:
            orig = len(view)
            view = view[: self.char_budget].rstrip() + f"\n…(truncated {orig - self.char_budget} chars)"
            logger.info("[state] state_view 주입 %d자→%d자 캡(char_budget=%d)",
                        orig, self.char_budget, self.char_budget)
        return view

    def _cap_item(self, content: str) -> str:
        """회상/작업기억 단일 항목 content 상한. 초과분은 잘라 표기(항목 1건이 budget 독점 방지)."""
        s = content or ""
        if self.item_char_cap > 0 and len(s) > self.item_char_cap:
            return s[: self.item_char_cap].rstrip() + f"…(truncated {len(s) - self.item_char_cap} chars)"
        return s

    @staticmethod
    def _resolve(src, state):
        if src is None:
            return None
        return src(state) if callable(src) else src


class MemoryStateRecorder:
    """iteration 경계 incremental 기록 — persist 콜백으로 외부 위임. seq(int)만 보유."""

    def __init__(
        self,
        persist: Callable[[str, dict], None],
        *,
        actor: str = "harness",
        refine_on_complete: bool = True,
        max_lessons: int = 3,
    ) -> None:
        self._persist = persist
        self.actor = actor
        self.refine_on_complete = refine_on_complete
        self.max_lessons = max_lessons
        self._seq = 0

    def record_iteration(self, state, decision: str) -> None:
        self._seq += 1
        ref = self._ref(state)
        done = decision in ("complete", "abort")
        ev = activity_from_message(
            seq=self._seq,
            actor=self.actor,
            raw_message=self._last_user(state),
            kind="harness",
            status="done" if done else "active",
            ref=ref,
        )
        self._persist("activity", ev.to_dict())
        if decision == "retry":  # Reflexion: 실패 회차를 교훈으로 정제(다음 회차 반영)
            lesson = refine_message(
                self._last_user(state), self._last_assistant(state),
                memory_id=f"lesson-{ref.get('run_id', 'run')}-{self._seq}",
                provenance={**ref, "kind": "lesson", "iteration": self._seq},
            )
            self._push_lesson(state, lesson.to_dict())
            self._persist("lesson", lesson.to_dict())
        if self.refine_on_complete and decision == "complete":
            mem = refine_message(
                self._last_user(state),
                self._last_assistant(state),
                memory_id=f"{ref.get('run_id', 'run')}-{self._seq}",
                provenance=ref,
            )
            self._persist("refined_memory", mem.to_dict())

    def _push_lesson(self, state, lesson: dict) -> None:
        """in-run 교훈 버퍼(state.metadata['loop_lessons']) — max_lessons FIFO 캡."""
        meta = getattr(state, "metadata", None)
        if meta is None:
            return
        buf = meta.get("loop_lessons")
        if not isinstance(buf, list):
            buf = []
            meta["loop_lessons"] = buf
        buf.append(lesson)
        if len(buf) > self.max_lessons:
            del buf[: len(buf) - self.max_lessons]

    @staticmethod
    def _ref(state) -> dict:
        meta = getattr(state, "metadata", None) or {}
        return {k: meta.get(k) for k in ("run_id", "thread_id", "interaction_id") if meta.get(k)}

    @staticmethod
    def _last_user(state) -> str:
        for m in reversed(getattr(state, "messages", []) or []):
            if m.get("role") == "user":
                c = m.get("content")
                return c if isinstance(c, str) else str(c)
        return ""

    @staticmethod
    def _last_assistant(state) -> str:
        return (getattr(state, "last_assistant_text", "") or
                getattr(state, "final_output", "") or "")
