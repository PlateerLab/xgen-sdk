"""s06_context — Cascade Compaction Mixin (Claude Code L3 / L4 / L5).

`ContextStage` 의 컴팩션 책임을 본 모듈로 분리. 단일 인스턴스 method 들 (try_cascade /
try_microcompact / try_context_collapse / try_autocompact / _autocompact_summarize) 이
한 god-class 안에 290+ LOC 동거하던 것을 mixin 으로 추출.

설계:
  - Mixin 은 Stage subclass 와 결합해 사용 (`class ContextStage(CascadeCompactionMixin, Stage):`).
  - 메서드 시그니처는 호환 유지 — `self.get_param`, `self.aux_call` 등 Stage 의 기능에 의존.
  - 임계값은 `_pct_threshold` 헬퍼를 통해 stage_param → runtime_default 폴백.

확장:
  - 새 compaction 단계 (예: L6 vector summary) 추가는 본 모듈에 새 method 만 더하면 됨.
  - cascade dispatch 순서 변경은 `try_cascade` 한 곳에서.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.state import PipelineState

logger = logging.getLogger("harness.stage.context.cascade")


class CascadeCompactionMixin:
    """L3 → L4 → L5 압축 cascade.

    `_pct_threshold` 헬퍼는 stage 모듈의 module-level 함수를 가져와 사용. (mixin 파일이
    stage 모듈에 import 되므로 순환 import 회피용으로 try_cascade 안에서 lazy import.)
    """

    async def try_cascade(
        self, state: "PipelineState", budget_used: float, results: dict,
    ) -> None:
        """Claude Code Cascade — 압력에 따라 L3 → L4 → L5 자동 선택. 한 턴에 하나만 발동.

        - < l3_th:  pass (L1 preview 는 s08 이 항상 수행)
        - >= l3_th: L3 microcompact (경량)
        - >= l4_th: L4 context_collapse_overlay (중량)
        - >= l5_th: L5 autocompact_llm (중량, 실패 시 회로 차단)

        v0.11.16 — L3 기본 70 → 80 (baseline token_budget 의 compaction_threshold 와 동기).
        Pilot #11 에서 조기 발동 품질 악화 관측 → baseline compact 시점 이후만 개입.
        v0.11.20 — helper 에 직접 threshold 전달 (state.metadata 임시 키 제거, leak 방지).
        """
        from .stage import _pct_threshold  # lazy — 순환 import 회피
        l3_th = _pct_threshold(self.get_param("cascade_l3_threshold", state, None), "cascade_l3_threshold_pct")
        l4_th = _pct_threshold(self.get_param("cascade_l4_threshold", state, None), "cascade_l4_threshold_pct")
        l5_th = _pct_threshold(self.get_param("cascade_l5_threshold", state, None), "cascade_l5_threshold_pct")
        cascade_applied: list[str] = []
        if budget_used >= l3_th:
            pre_mc = results.get("microcompacted", 0)
            self.try_microcompact(state, budget_used, results, threshold_override=l3_th)
            if results.get("microcompacted", 0) > pre_mc:
                cascade_applied.append("L3")
        if budget_used >= l5_th:
            pre_ac = results.get("autocompacted", 0)
            await self.try_autocompact(state, budget_used, results, threshold_override=l5_th)
            if results.get("autocompacted", 0) > pre_ac:
                cascade_applied.append("L5")
        elif budget_used >= l4_th:
            pre_cc = results.get("context_collapsed", 0)
            self.try_context_collapse(state, budget_used, results, threshold_override=l4_th)
            if results.get("context_collapsed", 0) > pre_cc:
                cascade_applied.append("L4")
        if cascade_applied:
            results["cascade_applied"] = cascade_applied
            logger.info("[Context] Cascade dispatched: %s (budget=%.0f%%)",
                        "+".join(cascade_applied), budget_used * 100)

    def try_microcompact(
        self, state: "PipelineState", budget_used: float, results: dict,
        threshold_override: float | None = None,
    ) -> None:
        """L3 — 오래된 tool_result 블록을 placeholder 로 교체. 원본은 pd_stores['tool_result'] 보존.

        threshold_override: cascade 에서 전달하는 임계(0~1). None 이면 stage_param 의 기본값 사용.
        """
        from .stage import _pct_threshold
        mc_keep = int(self.get_param("microcompact_keep_recent", state, None) or 0)
        if threshold_override is not None:
            mc_threshold = float(threshold_override)
        else:
            mc_threshold = _pct_threshold(
                self.get_param("microcompact_threshold", state, None),
                "microcompact_threshold_pct",
            )
        if budget_used <= mc_threshold:
            return
        tool_refs: list[tuple[int, int, str]] = []
        for mi, msg in enumerate(state.messages):
            if not isinstance(msg, dict):
                continue
            content = msg.get("content", "")
            if not isinstance(content, list):
                continue
            for bi, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tid = block.get("tool_use_id", "") or f"unknown_{mi}_{bi}"
                    tool_refs.append((mi, bi, tid))
        if len(tool_refs) <= mc_keep:
            return
        to_replace = tool_refs[:-mc_keep]
        replaced = 0
        for mi, bi, tid in to_replace:
            msg = state.messages[mi]
            content = msg.get("content") or []
            if isinstance(content, list) and 0 <= bi < len(content):
                original = content[bi]
                if isinstance(original, dict) and original.get("type") == "tool_result":
                    has_pd = state.pd_fetch("tool_result", tid) is not None
                    placeholder_text = (
                        f"[Microcompact — old tool_result. "
                        f"Retrieve via fetch_pd(kind='tool_result', id='{tid}')]"
                        if has_pd else
                        f"[Microcompact — tool_result omitted (id={tid})]"
                    )
                    content[bi] = {
                        "type": "tool_result",
                        "tool_use_id": tid,
                        "content": placeholder_text,
                    }
                    replaced += 1
        if replaced:
            results["compacted"] = True
            results["microcompacted"] = replaced
            logger.info(
                "[Context] L3 Microcompact: %d tool_results 교체 (최근 %d 유지)",
                replaced, mc_keep,
            )

    def try_context_collapse(
        self, state: "PipelineState", budget_used: float, results: dict,
        threshold_override: float | None = None,
    ) -> None:
        """L4 — 오래된 메시지를 overlay 로 교체하고 원본은 pd_stores['history'] 에 보존.

        threshold_override: cascade 에서 전달하는 임계(0~1). None 이면 stage_param 의 기본값 사용.
        """
        from .stage import _pct_threshold
        if threshold_override is not None:
            collapse_threshold = float(threshold_override)
        else:
            collapse_threshold = _pct_threshold(
                self.get_param("context_collapse_threshold", state, None),
                "context_collapse_threshold_pct",
            )
        keep_tail = int(self.get_param("context_collapse_keep_tail", state, None) or 0)
        if budget_used <= collapse_threshold or len(state.messages) <= keep_tail + 1:
            return
        head = state.messages[0]
        tail = state.messages[-keep_tail:]
        old = state.messages[1:-keep_tail]
        iter_no = state.loop_iteration
        preserved_ids: list[str] = []
        import json as _json
        for i, msg in enumerate(old):
            rid = f"msg_{iter_no}_{i}"
            role = msg.get("role", "?") if isinstance(msg, dict) else "?"
            preview_line = f"({role}) " + (
                str(msg.get("content", ""))[:120] if isinstance(msg, dict) else str(msg)[:120]
            )
            try:
                full_repr = _json.dumps(msg, ensure_ascii=False, default=str)
            except Exception as e:
                logger.debug("[Context] L4 collapse 메시지 %d JSON 직렬화 실패, str() fallback: %s", i, e)
                full_repr = str(msg)
            state.pd_store(
                kind="history",
                resource_id=rid,
                preview=preview_line,
                full=full_repr,
                meta={"role": role, "loop_iteration": iter_no, "original_index": i + 1},
            )
            preserved_ids.append(rid)
        overlay = {
            "role": "user",
            "content": (
                f"[Context Collapse Overlay — {len(old)} middle messages collapsed. "
                f"Originals preserved in pd_stores['history']. "
                f"Call fetch_pd(kind='history', id='<id below>') if needed. "
                f"First/last {keep_tail} messages kept. "
                f"Collapsed ids: {preserved_ids[:10]}" +
                (f"... (+{len(preserved_ids) - 10})" if len(preserved_ids) > 10 else "") +
                f"]"
            ),
        }
        state.messages = [head, overlay] + tail
        results["compacted"] = True
        results["context_collapsed"] = len(preserved_ids)
        logger.info(
            "[Context] L4 Collapse: %d messages preserved in pd_stores['history'], "
            "kept first + overlay + last %d",
            len(preserved_ids), keep_tail,
        )

    async def try_autocompact(
        self, state: "PipelineState", budget_used: float, results: dict,
        threshold_override: float | None = None,
    ) -> None:
        """L5 — child LLM 9-section summary 로 교체. 원본은 pd_stores['history'] 보존. 회로 차단기.

        threshold_override: cascade 에서 전달하는 임계(0~1). None 이면 stage_param 의 기본값 사용.
        """
        from .stage import _pct_threshold
        if threshold_override is not None:
            auto_threshold = float(threshold_override)
        else:
            auto_threshold = _pct_threshold(
                self.get_param("autocompact_threshold", state, None),
                "autocompact_threshold_pct",
            )
        keep_tail = int(self.get_param("autocompact_keep_tail", state, None) or 0)
        failures = int(state.metadata.get("autocompact_failures", 0))
        if failures >= 3:
            logger.warning("[Context] L5 Autocompact circuit-breaker tripped (failures=%d), skip", failures)
            return
        if budget_used <= auto_threshold or len(state.messages) <= keep_tail + 1:
            return
        head = state.messages[0]
        tail = state.messages[-keep_tail:]
        old = state.messages[1:-keep_tail]
        iter_no = state.loop_iteration
        preserved_ids: list[str] = []
        import json as _json
        for i, msg in enumerate(old):
            rid = f"auto_{iter_no}_{i}"
            role = msg.get("role", "?") if isinstance(msg, dict) else "?"
            try:
                full_repr = _json.dumps(msg, ensure_ascii=False, default=str)
            except Exception as e:
                logger.debug("[Context] L5 autocompact 메시지 %d JSON 직렬화 실패, str() fallback: %s", i, e)
                full_repr = str(msg)
            state.pd_store(
                kind="history", resource_id=rid,
                preview=f"({role}) ...", full=full_repr,
                meta={"role": role, "loop_iteration": iter_no,
                      "original_index": i + 1, "compaction": "autocompact_llm"},
            )
            preserved_ids.append(rid)
        summary_text = await self._autocompact_summarize(state, old)
        if summary_text:
            summary_msg = {
                "role": "user",
                "content": (
                    "[Autocompact Summary — child agent 9-section:]\n" + summary_text +
                    f"\n\n[{len(preserved_ids)} originals preserved in pd_stores['history']. "
                    f"If needed: fetch_pd(kind='history', id='auto_<iter>_<idx>')]"
                ),
            }
            state.messages = [head, summary_msg] + tail
            results["compacted"] = True
            results["autocompacted"] = len(preserved_ids)
            logger.info(
                "[Context] L5 Autocompact: %d messages → summary, kept first + last %d",
                len(preserved_ids), keep_tail,
            )
        else:
            state.metadata["autocompact_failures"] = failures + 1
            logger.warning("[Context] L5 Autocompact 실패 %d/3", failures + 1)

    async def _autocompact_summarize(self, state: "PipelineState", old_messages: list[dict]) -> str:
        """Claude Code L5 child agent 9-section summary.

        state.provider 가 있으면 그걸로 LLM 호출. 없으면 규칙 기반 fallback summary.
        9 sections: Primary Request, Key Decisions, Tools Used, Errors/Fixes,
        Files Touched, Data Mentioned, User Preferences, Open Issues, Next Steps.
        """
        # 메시지들을 LLM 에 던질 텍스트로 직렬화 (경량)
        lines = []
        for i, msg in enumerate(old_messages):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, list):
                # content blocks: text/tool_use/tool_result — 텍스트만 추출
                parts = []
                for b in content:
                    if isinstance(b, dict):
                        if b.get("type") == "text":
                            parts.append(str(b.get("text", "")))
                        elif b.get("type") == "tool_use":
                            parts.append(f"[tool_use: {b.get('name','?')}]")
                        elif b.get("type") == "tool_result":
                            parts.append(f"[tool_result: {str(b.get('content',''))[:200]}]")
                content_str = " ".join(parts)
            else:
                content_str = str(content)
            lines.append(f"[{i+1}] {role}: {content_str[:800]}")
        conversation = "\n".join(lines)

        prompt = (
            "Summarize the conversation history below into the 9-section structure. "
            "Answer in the same language as the conversation, 1-3 lines per section.\n\n"
            "## Primary Request\n## Key Decisions\n## Tools Used\n## Errors/Fixes\n"
            "## Files Touched\n## Data Mentioned\n## User Preferences\n## Open Issues\n## Next Steps\n\n"
            "--- conversation start ---\n"
            f"{conversation}\n"
            "--- conversation end ---"
        )

        provider = getattr(state, "provider", None)
        if provider is None:
            # Fallback: 규칙 기반 초간단 summary
            roles = [m.get("role", "?") for m in old_messages if isinstance(m, dict)]
            return (
                "## Primary Request\n(child LLM unavailable — rule-based fallback)\n"
                f"## Messages\n{len(old_messages)} total ({dict((r, roles.count(r)) for r in set(roles))})\n"
                "## Note\nL5 triggered before provider initialization. Re-run for an accurate summary."
            )

        # v0.26.11 — _aux_call 통합 (max_tokens 단일 진실 소스 = config.aux_max_tokens).
        # token tracking + cost 누적 + StageSubstepEvent emit 일관 보장.
        try:
            from ...core.llm_call import aux_call
            return await aux_call(
                state,
                stage_id="s06_context.l5_autocompact",
                prompt=prompt,
                system="You are a precise summarization agent.",
            )
        except Exception as e:
            logger.warning("[Context] L5 summarize LLM 호출 실패: %s", e)
            return ""
