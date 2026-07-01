"""
S05 Policy — Policy Gate (v0.17.0)

## 책임
선언형 Guard 체인을 4 개 훅 포인트에 집행한다:
  PRE_MAIN       — 본문 LLM 호출 직전 (입력/Plan 정책)
  PRE_TOOL       — 도구 호출 직전 (pending_tool_calls 각 항목)
  POST_RESPONSE  — LLM 응답 직후 (출력 검증)
  LOOP_BOUNDARY  — 루프 경계 (예산/반복/집계 정책)

## 호출 규약
Pipeline 이 `stage.role == "policy_gate"` 인스턴스를 찾아 `invoke_hook(state, hook)`
를 직접 호출한다 — 일반 loop 순서에서는 bypass. (orchestrator_planner 와 동일 패턴.)

## stage_params 계약
```
stage_params["s05_policy"] = {
    "guards": [
        {"name": "iteration"},
        {"name": "cost_budget", "params": {"cost_budget_usd": 5.0}},
        {"name": "tool_precondition", "params": {"rules": [...]}},
    ]
}
```
`guards` 항목 각각은 `{"name": <guard name>, "params": <dict>}`. Guard name 은
`xgen_harness.guards` entry_points 그룹에 등록된 이름 (내장 5 종 + 외부 갤러리).
"""

from __future__ import annotations

import logging
from typing import Any

from ...core.stage import Stage, StrategyInfo
from ...core.state import PipelineState

logger = logging.getLogger("harness.stage.policy")


class PolicyGateStage(Stage):
    """정책 게이트 — 도구 호출·LLM 호출·응답·루프 경계 4 훅 시점에 Guard 체인을 집행하는 특수 Stage. 일반 loop 순번에는 끼지 않고 (자동 bypass), guards 파라미터가 선언됐을 때만 Pipeline 이 훅별로 개별 호출. 감사·예산·콘텐츠 정책이 필요할 때 활성화.

    UI 실행 요약의 "Skipped (condition unmet)" 은 "guards 가 비어있어서 훅이 아무 검사도 하지 않았다" 는 뜻 — 오류 아님. guards 를 선언하려면 s05_policy.guards stage_param 에 Guard 이름 리스트를 넘긴다.
    """

    # Machine meta — LLM(planner) 이 Auto 모드에서 읽는 선택 근거.
    when_to_use = "도구 호출 순서 정책·콘텐츠 정책·예산/반복 제한 등 규제·감사 요구가 있을 때"
    when_to_skip = "단순 Q&A 또는 내부 프로토타이핑"
    cost_hint = "low"

    @property
    def stage_id(self) -> str:
        return "s05_policy"

    @property
    def role(self) -> str:
        # Pipeline 이 이 role 로 찾아 `invoke_hook(state, hook)` 를 여러 시점에 직접 호출.
        return "policy_gate"

    @property
    def order(self) -> int:
        # v1.0: 일반 순번 진입 (PRE_TOOL 시점, s04_tool 다음 / s06_context 전).
        # Guard 가 block 하면 PipelineAbortError 즉시 발생 → "규제 위반 → 실행 멈춤" 보장.
        # 동시에 Pipeline 이 PRE_MAIN / POST_RESPONSE / LOOP_BOUNDARY 훅에서도 별도 호출.
        return 5

    @classmethod
    def param_schema(cls) -> list:
        """LLM 이 파라미터 조립 시 참조하는 스키마 + UI 동적 폼 렌더 기반.

        자연어 리터럴(label/description/placeholder) 없음 — UI 는 id 자동 렌더.
        `options_source` 만 남겨 외부 Guard 자동 합류 보장.
        """
        from ..strategies.guard import FieldSchema
        return [
            FieldSchema(
                id="guards",
                type="guard_list",
                options_source="guards_available",
                default=[],
            ),
        ]

    @classmethod
    def describe_config(cls) -> dict:
        # _compose_from_class_attrs 폴백은 description_en 에 한국어 docstring 을 그대로
        # 박는다 (stage_config.py:898-899 의 "i18n 없음. 추후 gettext" TODO). 다른 11
        # Stage 는 STAGE_CONFIGS dict 에 영문 별도 보유 — 이 Stage 만 self-describing
        # 으로 위임됐는데 override 가 비어있어서 영문 locale 에서도 한국어가 노출됐다.
        # 명시적 영문/한글 분리로 i18n 일관성 회복.
        return {
            "description_ko": (
                "정책 게이트 — 4 훅 시점(PRE_MAIN / PRE_TOOL / POST_RESPONSE / "
                "LOOP_BOUNDARY)에 Guard 체인을 집행하는 특수 Stage. "
                "guards 가 비어있으면 자동 bypass."
            ),
            "description_en": (
                "Policy Gate — runs a guard chain at 4 hook points "
                "(PRE_MAIN / PRE_TOOL / POST_RESPONSE / LOOP_BOUNDARY). "
                "Bypassed automatically when guards is empty."
            ),
            "when_to_use": cls.when_to_use,
            "when_to_skip": cls.when_to_skip,
            "cost_hint": cls.cost_hint,
            "fields": [f.to_dict() if hasattr(f, "to_dict") else f for f in cls.param_schema()],
            "behavior": [],
        }

    def should_bypass(self, state: PipelineState) -> bool:
        # v1.0 — 자동 bypass: guards 가 비어있으면 stage 흐름에서 skip (default 가 bypass).
        # guards 가 선언됐을 때만 일반 순번 진입 (PRE_TOOL 시점 검사). 4훅(PRE_MAIN/POST_RESPONSE/
        # LOOP_BOUNDARY) 호출은 Pipeline 이 role 로 별도 트리거하므로 영향 없음.
        guard_configs = self.get_param("guards", state, []) or []
        return not bool(guard_configs)

    async def execute(self, state: PipelineState) -> dict:
        # v1.0 — 일반 순번 진입 시점. machine meta 만 emit (UI 안내 텍스트 박제 X).
        # 실제 Guard 차단은 Pipeline 이 4 훅 (PRE_MAIN/PRE_TOOL/POST_RESPONSE/LOOP_BOUNDARY) 에서 별도 호출.
        guards = self.get_param("guards", state, []) or []
        guard_names = [g.get("name") for g in guards if isinstance(g, dict) and g.get("name")]
        # v1.5.3 — 사용자 디버깅 needs 정합. 활성 가드 list 를 verbose 이벤트로 EventLog 에 노출.
        # "정책 안 먹힘" 호소 — 어떤 가드가 활성됐는지 사용자가 보이게.
        from ...events.types import StageSubstepEvent as _Sub
        await state.emit_verbose(_Sub(
            stage_id=self.stage_id,
            substep="guards_active",
            meta={"guards": guard_names, "count": len(guards)},
        ))
        return {
            "active": True,
            "guards_declared": len(guards),
            "guard_names": guard_names,
        }

    async def invoke_hook(self, state: PipelineState, hook_name: str) -> dict[str, Any]:
        """Pipeline 이 훅 시점에 호출하는 진입점.

        반환값은 진단용. 실제 효과는 state 변이:
          - PRE_TOOL 차단: state.pending_tool_calls 에서 해당 tc 제거 +
                           state.add_tool_result(is_error=True) 로 가짜 결과 주입
          - PRE_MAIN / POST_RESPONSE / LOOP_BOUNDARY 차단:
            state.policy_block_reason / state.policy_block_guard 설정,
            LOOP_BOUNDARY 는 state.loop_decision = "complete" 도 설정.
        """
        from ..strategies.guard import HookPoint, build_guard_chain

        try:
            hook = HookPoint(hook_name)
        except ValueError:
            logger.warning("[PolicyGate] 알 수 없는 훅: %s", hook_name)
            return {"error": f"unknown hook: {hook_name}"}

        guard_configs = self.get_param("guards", state, []) or []
        if not guard_configs:
            return {"hook": hook.value, "guards": 0}

        chain = build_guard_chain(guard_configs)
        if not chain.guards:
            return {"hook": hook.value, "guards": 0}

        # PRE_TOOL 은 도구 호출별로 개별 검사 (v0.24.0 — HITL 대기 지원 async 경로)
        if hook == HookPoint.PRE_TOOL:
            return await self._check_pre_tool(state, chain)

        # 그 외 단일 검사
        results = chain.invoke(hook, state)
        blocked = next((r for r in results if not r.passed and r.severity == "block"), None)
        if blocked:
            state.policy_block_reason = blocked.reason
            state.policy_block_guard = blocked.guard_name
            # severity 는 Guard config 에서 왔다(하드코딩 아님). s09_finalize 가 소비.
            state.policy_block_severity = getattr(blocked, "severity", "block") or "block"
            logger.warning(
                "[PolicyGate] %s 차단 @ %s — %s",
                blocked.guard_name, hook.value, blocked.reason,
            )
            if hook == HookPoint.LOOP_BOUNDARY:
                state.loop_decision = "complete"
            await self._emit_policy_blocked(state, blocked, hook)
        else:
            # warn severity 위반은 통과시키되 감사 로그 + 이벤트로 남긴다(조용히 삼키지 않음).
            for r in results:
                if not r.passed and r.severity == "warn":
                    logger.warning(
                        "[PolicyGate] %s 경고 @ %s — %s (통과)",
                        r.guard_name, hook.value, r.reason,
                    )
                    await self._emit_policy_blocked(state, r, hook)
        return {
            "hook": hook.value,
            "guards_checked": len(chain.guards),
            "blocked": bool(blocked),
            "blocked_guard": blocked.guard_name if blocked else "",
        }

    async def _check_pre_tool(self, state: PipelineState, chain) -> dict[str, Any]:
        """pending_tool_calls 각 항목에 대해 Guard 체인 실행.

        v0.24.0 — invoke_async 사용. HITL 같은 await 필요 Guard 지원.
        기존 sync Guard 는 Guard.check_async 기본 구현이 check() 래핑이라 무영향.

        차단된 tc 는 pending 에서 제거하고 가짜 tool_result (is_error=True) 주입.
        LLM 은 다음 턴에 에러 결과를 보고 스스로 교정 시도.
        """
        from ..strategies.guard import HookPoint

        pending = list(state.pending_tool_calls or [])
        if not pending:
            return {"hook": HookPoint.PRE_TOOL.value, "checked": 0}

        blocked_ids: set[str] = set()
        checked = 0
        for tc in pending:
            checked += 1
            results = await chain.invoke_async(HookPoint.PRE_TOOL, state, pending_tool_call=tc)
            blocked = next((r for r in results if not r.passed and r.severity == "block"), None)
            if not blocked:
                continue

            tool_use_id = tc.get("tool_use_id", "")
            tool_name = tc.get("tool_name", "?")
            msg = blocked.tool_error_message or blocked.reason or "Blocked by policy"
            content = f"[BLOCKED by {blocked.guard_name}] {msg}"

            state.add_tool_result(tool_use_id, content, is_error=True)
            blocked_ids.add(tool_use_id)
            logger.warning(
                "[PolicyGate] pre_tool 차단: %s (tool=%s) — %s",
                blocked.guard_name, tool_name, blocked.reason,
            )
            await self._emit_policy_blocked(state, blocked, HookPoint.PRE_TOOL, tool_name=tool_name)

        if blocked_ids:
            # 차단된 항목 제거
            remaining = [tc for tc in pending if tc.get("tool_use_id") not in blocked_ids]
            state.pending_tool_calls = remaining
            # 전부 차단됐으면 tool_results 를 messages 에 flush
            # (s07_act 는 pending 비어있으면 bypass 되므로 수동 flush 필요)
            if not remaining:
                state.flush_tool_results()

        return {
            "hook": HookPoint.PRE_TOOL.value,
            "guards_checked": len(chain.guards),
            "checked": checked,
            "blocked": len(blocked_ids),
        }

    def list_strategies(self) -> list[StrategyInfo]:
        # Strategy Variants 대신 Guard 조합으로 구성 — Strategy 슬롯 없음.
        return []

    async def _emit_policy_blocked(
        self,
        state: PipelineState,
        blocked: Any,
        hook: Any,
        tool_name: str = "",
    ) -> None:
        """Guard block 사실을 PolicyBlockedEvent 로 발행. UI 가 "정책 차단" 배너 표시.

        block 은 사용자에게 즉시 가시화돼야 하는 정상 흐름(verbose 게이트 X)이라
        emit_event 사용. emitter 미연결이어도 throw 안 함 (state.emit_event 가 흡수).
        """
        from ...events.types import PolicyBlockedEvent
        hook_value = getattr(hook, "value", str(hook))
        await state.emit_event(PolicyBlockedEvent(
            guard_name=getattr(blocked, "guard_name", "") or "",
            hook=hook_value,
            reason=getattr(blocked, "reason", "") or "",
            severity=getattr(blocked, "severity", "block") or "block",
            tool_name=tool_name,
        ))
