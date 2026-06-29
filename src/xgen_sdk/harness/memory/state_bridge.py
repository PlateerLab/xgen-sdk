"""StateProvider/StateRecorder 참조 구현 — memory 모듈을 stateful loop seam 에 연결.

Provider 는 호출마다 bounded(cap·char_budget) 뷰를 재계산(내부 무축적), Recorder 는
seq(int)만 보유하고 매 회차 1건을 persist 콜백으로 위임(프로세스 무축적). 둘 다 DB
무관 — 소스/persist 는 이식측 콜백. 서브에이전트별 새 인스턴스라 누수가 구조적으로 없다.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Sequence, Union

from .recall import RecallSet
from .refine import RefinedMemory, refine_message
from .activity import activity_from_message

_RecallSrc = Union[RecallSet, Callable[[Any], Optional[RecallSet]], None]
_RefinedSrc = Union[Sequence[RefinedMemory], Callable[[Any], Sequence[RefinedMemory]], None]


class MemoryStateProvider:
    """정제 장기기억 + 작업기억 → bounded state 뷰(markdown). 누적 없음."""

    def __init__(
        self,
        *,
        recall: _RecallSrc = None,
        refined: _RefinedSrc = None,
        max_recall: int = 8,
        max_refined: int = 5,
        max_lessons: int = 3,
        char_budget: int = 2000,
    ) -> None:
        self._recall = recall
        self._refined = refined
        self.max_recall = max_recall
        self.max_refined = max_refined
        self.max_lessons = max_lessons
        self.char_budget = char_budget

    def get_state_view(self, state) -> Optional[str]:
        parts: list[str] = []
        # Reflexion: 이번 런의 최근 실패 교훈(in-run, bounded) — 같은 실수 반복 회피.
        lessons = (getattr(state, "metadata", None) or {}).get("loop_lessons") or []
        if lessons:
            parts.append("### Recent attempt lessons (avoid repeating mistakes)")
            for l in list(lessons)[-self.max_lessons:]:
                line = f"- {l.get('intent', '')}".rstrip()
                if l.get("outcome"):
                    line += f" → {l['outcome']}"
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
                    parts.append(f"- {it.content}".rstrip())
        view = "\n".join(parts).strip()
        if not view:
            return None
        if len(view) > self.char_budget:
            view = view[: self.char_budget].rstrip() + "\n…(truncated)"
        return view

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
