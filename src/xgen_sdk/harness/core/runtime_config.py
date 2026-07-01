"""RuntimeConfigMutator — 실행 중 자기 config 를 되쓰는 중립 메커니즘 (v1.24).

"환경설정을 스스로 가져오고 꽂는 노드" 의 엔진측 코어. 스테이지/전략/플러그인이
`state.get_config_mutator()` 로 이 객체를 얻어 자기 downstream config(stage_params /
active_strategies / scalar / system_prompt / node_overrides)를 라이브로 되쓴다.

설계 원칙 (PHILOSOPHY: 엔진=메커니즘, 이식=정책):
  - **중립**: 변이 어휘는 `forge.EngineAlgebra` 에서 재사용 — legality 검증과 inverse
    (롤백)가 빌드타임 forge 와 *동일한 config dict 경로*를 공유한다. 새 어휘 하드코딩 0.
  - **기본 OFF**: mode 는 `config.runtime_self_govern` 로 게이트. "off"(기본)면 모든
    변이가 no-op → 기존 동작 변화 0. 이식 노드 파라미터로만 opt-in.
  - **롤백 가능**: "act" 모드의 모든 변이는 (move, inverse) 저널에 쌓여 `rollback()`
    한 방으로 역적용 (Inertia-Brake 와 동형).

mode:
  - "off"     : 모든 변이 no-op, False 반환.
  - "observe" : 변이를 적용하지 않고 proposals 에 기록 후 True — diff 가시화/HITL.
  - "act"     : algebra legality 통과분만 라이브 적용 + inverse 저널.
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Optional

from ..forge.algebra import EngineAlgebra, Move

logger = logging.getLogger("harness.runtime_config")

# algebra 가 다루는 config dict view 에 실어야 하는 scalar 필드 (HarnessConfig 실 필드).
_SCALAR_FIELDS = ("validation_threshold", "max_retries", "max_iterations", "temperature")
_VALID_MODES = ("off", "observe", "act")


class RuntimeConfigMutator:
    """런타임 config 되쓰기 — gated·journaled·reversible."""

    def __init__(
        self,
        config: Any,
        *,
        services: Any = None,
        algebra: Optional[EngineAlgebra] = None,
        mode: str = "off",
    ) -> None:
        self._cfg = config
        self._services = services
        self._algebra = algebra or EngineAlgebra()
        self._mode = mode if mode in _VALID_MODES else "off"
        # 적용 저널: (applied_move, inverse_move) — rollback 역적용용 + provenance.
        self._journal: list[tuple[Move, Move]] = []
        # observe 모드에 쌓이는 미적용 제안.
        self._proposals: list[Move] = []

    # ── 가시 상태 ────────────────────────────────────────────────
    @property
    def mode(self) -> str:
        return self._mode

    @property
    def journal(self) -> list[tuple[Move, Move]]:
        return list(self._journal)

    @property
    def proposals(self) -> list[Move]:
        return list(self._proposals)

    def diff(self) -> list[str]:
        """사람이 읽는 적용/제안 요약 (config_diff 이벤트/ForgeDiffPanel 용)."""
        applied = [str(m) for m, _ in self._journal]
        proposed = [str(m) for m in self._proposals]
        return applied + [f"(proposed) {p}" for p in proposed]

    # ── 내부: config dict view ↔ dataclass write-back ────────────
    def _view(self) -> dict[str, Any]:
        """algebra 가 읽고 쓰는 최소 config dict view (deepcopy)."""
        v: dict[str, Any] = {
            "active_strategies": copy.deepcopy(getattr(self._cfg, "active_strategies", {}) or {}),
            "stage_params": copy.deepcopy(getattr(self._cfg, "stage_params", {}) or {}),
            "system_prompt": getattr(self._cfg, "system_prompt", "") or "",
        }
        for sk in _SCALAR_FIELDS:
            v[sk] = getattr(self._cfg, sk, None)
        return v

    def _apply_move(self, move: Move) -> None:
        """move 를 dataclass config 에 직접 적용 (ungated — 게이트는 호출자 책임)."""
        if move.op == "set_node_override":
            node_id, key = move.target.split(":", 1)
            no = getattr(self._cfg, "node_overrides", None)
            if no is None:
                self._cfg.node_overrides = no = {}
            no.setdefault(node_id, {})[key] = move.value
            return
        # 나머지 어휘는 algebra.apply 에 위임 후 변경면만 write-back.
        new = self._algebra.apply(self._view(), move)
        self._cfg.active_strategies = new.get("active_strategies", self._cfg.active_strategies)
        self._cfg.stage_params = new.get("stage_params", self._cfg.stage_params)
        if "system_prompt" in new:
            self._cfg.system_prompt = new["system_prompt"]
        for sk in _SCALAR_FIELDS:
            if sk in new:
                setattr(self._cfg, sk, new[sk])

    def _inverse(self, move: Move) -> Move:
        if move.op == "set_node_override":
            node_id, key = move.target.split(":", 1)
            before = (getattr(self._cfg, "node_overrides", {}) or {}).get(node_id, {}).get(key)
            return Move("set_node_override", move.target, before)
        return self._algebra.inverse(self._view(), move)

    def _mutate(self, move: Move) -> bool:
        """게이트 → legality → (act)적용+저널 / (observe)제안기록 / (off)no-op."""
        # node_override 는 algebra 어휘 밖이라 legality 면제, 나머지는 algebra 검증.
        if move.op != "set_node_override" and not self._algebra.is_legal(move):
            return False
        if self._mode == "off":
            return False
        if self._mode == "observe":
            self._proposals.append(move)
            return True
        # act
        inv = self._inverse(move)
        try:
            self._apply_move(move)
        except Exception as e:  # algebra.apply 가 illegal 등으로 raise
            logger.warning("[mutator] apply failed: %s (%s)", move, e)
            return False
        self._journal.append((move, inv))
        logger.info("[mutator] applied %s", move)
        return True

    # ── 공개 변이 API ────────────────────────────────────────────
    def set_strategy(self, stage: str, impl: str) -> bool:
        """active_strategies[stage] = impl."""
        return self._mutate(Move("set_strategy", stage, impl))

    def set_stage_param(self, stage: str, key: str, value: Any) -> bool:
        """stage_params[stage][key] = value."""
        return self._mutate(Move("set_stage_param", f"{stage}:{key}", value))

    def set_scalar(self, key: str, value: Any) -> bool:
        """top-level scalar (validation_threshold/max_retries/max_iterations/temperature)."""
        return self._mutate(Move("tune_scalar", key, value))

    def set_node_override(self, node_id: str, key: str, value: Any) -> bool:
        """node_overrides[node_id][key] = value (노드별 환경 오버라이드)."""
        return self._mutate(Move("set_node_override", f"{node_id}:{key}", value))

    def append_guidance(self, text: str) -> bool:
        """GEPA-진화 가이드 블록을 system_prompt 에 append (사용자 프롬프트와 분리)."""
        return self._mutate(Move("append_guidance", "system_prompt", text))

    async def persist_env(self, key: str, value: Any, category: str = "") -> bool:
        """환경 KV 영속 되쓰기 (MutableConfigService.set_value). 미구현 시 graceful False.

        in-run dataclass 변이와 달리 이건 **프로세스 밖**(persistent_configs)을 바꾸므로
        이식측 권한 게이트(ABAC) 안에서만 의미. 서비스 없으면 False.
        """
        if self._mode == "off":
            return False
        if self._mode == "observe":
            self._proposals.append(Move("persist_env", key, value))
            return True
        cfg_svc = getattr(self._services, "config", None) if self._services else None
        setter = getattr(cfg_svc, "set_value", None) if cfg_svc else None
        if setter is None:
            logger.info("[mutator] persist_env skipped — no MutableConfigService.set_value")
            return False
        try:
            return bool(await setter(key, str(value), category))
        except Exception as e:
            logger.warning("[mutator] persist_env failed: %s", e)
            return False

    # ── Plan 호환 (s00 _merge_plan_into_config 의 gated 대체) ────
    def apply_plan(self, plan: Any) -> int:
        """HarnessPlan(params/strategies/max_iterations)을 gated 경로로 적용.

        과거 `s00_harness._merge_plan_into_config` 가 ungated 로 하던 일을, 같은
        config 경로 위에서 legality 검증·inverse 저널·mode 게이트를 거쳐 수행한다.
        반환 = 실제 적용(또는 observe 기록)된 move 수.
        """
        applied = 0
        for sid, overrides in (getattr(plan, "params", {}) or {}).items():
            if not isinstance(overrides, dict):
                continue
            for k, val in overrides.items():
                if self.set_stage_param(sid, k, val):
                    applied += 1
        for sid, impl in (getattr(plan, "strategies", {}) or {}).items():
            if isinstance(impl, str) and impl and self.set_strategy(sid, impl):
                applied += 1
        mi = getattr(plan, "max_iterations", None)
        if isinstance(mi, int) and mi > 0 and self.set_scalar("max_iterations", mi):
            applied += 1
        return applied

    # ── 신호 기반 자가조정 (retry 경계 자동 제안) ────────────────
    def propose_from_retry_signals(
        self,
        *,
        score: Optional[float],
        threshold: Optional[float],
        feedback: str = "",
        retry_count: int = 0,
    ) -> int:
        """judge 미달로 retry 직전, **신호로부터** 보수적 config 변경을 제안·적용.

        하드코딩된 config 값을 박지 않는다. "어떤 knob 을 어느 방향으로" 는 신호가
        정하고, "실제 값" 은 algebra 의 적응형 이웃 생성기(_gen_candidates)가 현재값
        기준으로 뽑는다(타입-안전 경계 안에서). 게이트/적용/저널은 기존 _mutate 경유:
          - mode="off"     → 아무것도 안 함(모든 하위 호출 no-op).
          - mode="observe" → 제안만 proposals 에 기록.
          - mode="act"     → legality 통과분만 라이브 적용 + inverse 저널.

        신호→방향 매핑(보수적, 신호 없으면 아무 제안 안 함):
          1. score 가 threshold 미달 → temperature 를 **현재값보다 낮은** 이웃으로
             (더 결정적인 재시도). 후보 중 현재값 미만이 있을 때만.
          2. 매 재시도 정체(score 정보 유무 무관, retry_count>=1) → validation_threshold
             를 **현재값보다 낮은** 이웃으로 한 칸(무한 retry 완화 — 통과 문턱 낮춤).
        반환 = 제안/적용된 move 수.
        """
        # off 모드면 후보 계산 자체를 건너뛴다(관측 가능한 부작용 0).
        if self._mode == "off":
            return 0

        view = self._view()
        proposed = 0

        def _lower_neighbor(key: str) -> Any:
            """현재값보다 낮은 가장 가까운 적응형 후보(없으면 None)."""
            cur = view.get(key)
            if not isinstance(cur, (int, float)):
                return None
            from ..forge.algebra import _gen_candidates
            lowers = [c for c in _gen_candidates(key, cur) if isinstance(c, (int, float)) and c < cur]
            return max(lowers) if lowers else None

        below = (
            isinstance(score, (int, float))
            and isinstance(threshold, (int, float))
            and score < threshold
        )
        if below:
            t = _lower_neighbor("temperature")
            if t is not None and self.set_scalar("temperature", t):
                proposed += 1
        if retry_count >= 1:
            vt = _lower_neighbor("validation_threshold")
            if vt is not None and self.set_scalar("validation_threshold", vt):
                proposed += 1
        return proposed

    # ── 롤백 (Inertia-Brake 동형) ────────────────────────────────
    def rollback(self) -> int:
        """저널의 inverse 를 역순 적용 → config 를 변이 이전으로 복원. 반환 = 되돌린 수."""
        n = 0
        for _move, inv in reversed(self._journal):
            try:
                self._apply_move(inv)
                n += 1
            except Exception as e:
                logger.warning("[mutator] rollback step failed: %s (%s)", inv, e)
        self._journal.clear()
        return n
