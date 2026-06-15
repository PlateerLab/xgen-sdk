"""xgen_sdk.harness.facade

XGEN 하네스 엔진(`xgen-harness`)을 플랫폼에서 한 줄로 쓰게 감싸는 facade.

엔진은 도메인 agnostic 한 별도 패키지다. 이 SDK 는 엔진을 **정규 의존성으로 끌어와
래핑만** 한다 — 엔진을 수정하지 않고, 엔진은 SDK 를 모른다(의존 방향 SDK → engine).

add_step / delete_step:
    엔진은 스테이지를 add(`register_stage` + entry_points) / delete(`disabled_stages`
    토글)로 확장하도록 설계돼 있다. 이 facade 가 그 둘을 대칭 메서드로 노출한다.
    REQUIRED 스테이지(s01_input·s08_decide·s09_finalize)는 엔진이 비활성을 거부한다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from xgen_harness import HarnessConfig, HarnessSession, Pipeline, register_stage

if TYPE_CHECKING:
    from xgen_harness.core.state import PipelineState
    from xgen_harness.events.emitter import EventEmitter


class Harness:
    """하네스 1회/멀티턴 실행의 SDK facade.

    Example:
        from xgen_sdk.harness import Harness

        h = Harness(provider="anthropic", model="claude-sonnet-4-6",
                    api_key=key, system_prompt="...", max_iterations=5)
        h.delete_step("s06_context")           # 비필수 스텝 제거
        h.add_step("s_audit", MyAuditStage)     # 커스텀 스텝 추가
        state = await h.run("사용자 입력")

    스텝 제어는 엔진 확장점을 그대로 위임한다:
        add_step    → xgen_harness.register_stage (+ optional required 마킹)
        delete_step → HarnessConfig.toggle_stage(id, False)  (REQUIRED 는 거부됨)
    """

    def __init__(self, *, api_key: Optional[str] = None, **config_kwargs: Any) -> None:
        self._api_key = api_key
        self._provider = config_kwargs.get("provider", "")
        self._model = config_kwargs.get("model", "")
        self.config: HarnessConfig = HarnessConfig(**config_kwargs)

    # ── 스텝 확장 (add / delete) ──────────────────────────────────
    def add_step(
        self,
        stage_id: str,
        stage_class: type,
        *,
        artifact: str = "default",
        required: bool = False,
    ) -> "Harness":
        """커스텀 스텝 추가. stage_class 는 `xgen_harness.Stage` 서브클래스여야 하며
        `order`(실행 순서)·`phase`(ingress/loop/egress)를 선언한다. 코어 수정 0."""
        register_stage(stage_id, artifact, stage_class)
        if required:
            from xgen_harness.core.config import mark_stage_required
            mark_stage_required(stage_id)
        return self

    def delete_step(self, stage_id: str) -> "Harness":
        """스텝 비활성(제거). REQUIRED 스테이지는 엔진이 거부하므로 무시된다."""
        self.config.toggle_stage(stage_id, False)
        return self

    def enable_step(self, stage_id: str) -> "Harness":
        """delete_step 으로 끈 스텝을 다시 켠다."""
        self.config.toggle_stage(stage_id, True)
        return self

    def steps(self) -> list[str]:
        """현재 활성 스텝 ID 목록 (순서대로)."""
        return self.config.get_active_stage_ids()

    # ── 실행 ──────────────────────────────────────────────────────
    async def run(
        self, user_input: str, emitter: "Optional[EventEmitter]" = None
    ) -> "PipelineState":
        """10-스테이지 파이프라인 실행. provider/tool_source 는 HarnessConfig +
        엔진 레지스트리(플랫폼이 entry_points 로 주입)가 해석한다."""
        if self._api_key:
            from xgen_harness.core.execution_context import set_execution_context
            set_execution_context(
                api_key=self._api_key, provider=self._provider, model=self._model
            )
        return await HarnessSession(config=self.config).run(user_input, emitter)

    def build(self) -> Pipeline:
        """고급 사용 — 엔진 Pipeline 인스턴스를 직접 얻는다."""
        return Pipeline.create(self.config)
