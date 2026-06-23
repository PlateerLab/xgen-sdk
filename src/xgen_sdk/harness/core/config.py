"""
HarnessConfig — 파이프라인 설정

v1.0 통합 — 9 스테이지 (s00_harness 본문 LLM 호출 stage 별도) + s05_policy 4훅 동시 작동.
s00_harness 가 Provider / Strategy / 본문 LLM 호출 / Planner 디스패처 역할.
workflow_data.harness_config 에서 로드.
"""

import logging
from dataclasses import dataclass, field, fields
from typing import Any, Optional

from .stage_config import canonical_stage_id as _canonical, canonical_stage_id_map as _alias_map

_cfg_logger = logging.getLogger("harness.core.config")


# 전체 10 스테이지 (v1.0 통합 — s05_strategy 분해 / s08_judge·s10_save 격하 / s12_publish 삭제)
# s00_harness 는 본문 LLM 호출 stage (Transport / Provider 디스패처). s05_policy 는 일반 순번 진입 + 4훅 동시.
# 이 리스트는 "원래부터 있던 기본 Stage" 화이트리스트. 새 Stage 는 registry 에 등록만 —
# `get_active_stage_ids()` 가 registry 와 병합해서 런타임 목록 생성.
ALL_STAGES = [
    "s01_input",
    "s02_history",
    "s03_prompt",
    "s04_tool",
    "s05_policy",
    "s06_context",
    "s07_act",
    "s08_decide",
    "s09_finalize",
]

# 비활성화 불가 스테이지 — 엔진 기본값 3개. 외부 기여자는 `mark_stage_required()` 로 추가.
# v1.0 — 번호 시프트 반영: s09_decide → s08_decide, s11_finalize → s09_finalize.
REQUIRED_STAGES: set[str] = {"s01_input", "s08_decide", "s09_finalize"}


# ─── v1.4.0 — Deprecated strategy / stage_param 자동 정규화 ───────────────────
# v1.4.0 BREAKING 에서 list_strategies() 가 빈 리스트로 좁혀진 stage 들의 옛 strategy
# 이름이 DB workflow row 의 active_strategies 에 잔존할 수 있다. 사용자 픽 카드가
# 사라진 상태에서 옛 이름이 dispatcher 로 흘러들어가면 동일 동작은 하지만 새 default
# (cascade / progressive_3level / section_priority / default) 의 이점을 못 누린다.
# `__post_init__` 에서 자동 정규화 — 모든 인스턴스화 경로 (cls(**), from_dict,
# from_workflow, 이식측 직접 cls(**config_kwargs)) 가 통과한다.
#
# 빈 문자열 ("") 로 매핑하면 active_strategies 키 자체 삭제 → resolve_strategy 가
# stage 의 default_impl 인자로 자동 폴백.
DEPRECATED_STRATEGIES_BY_STAGE: dict[str, dict[str, str]] = {
    "s01_input": {
        "with_classification": "",   # v1.4.0 hide. LLM 자율 분류로.
    },
    "s03_prompt": {
        "cot_planner": "",           # thinking 패턴 전부 hide. LLM 자율.
        "react": "",
        "none": "",
    },
    "s04_tool": {
        "eager_load": "",            # progressive_3level + ToolSearch 가 default.
        "capability_auto": "",       # 자동 capability discovery 도 hide.
        "none": "",
    },
    "s06_context": {
        # cascade 가 압력별 L3→L4→L5 자동 에스컬레이션. 옛 strategy 들 모두 cascade 로.
        "token_budget": "cascade",
        "sliding_window": "cascade",
        "microcompact": "cascade",
        "context_collapse_overlay": "cascade",
        "autocompact_llm": "cascade",
    },
    "s07_act": {
        "parallel_read": "",         # default (sequential) 로.
        "strict_no_error": "",
    },
}

# stage_params 측 deprecated value — UI 표면 단순화 정합. v1.4.0 default 로 강제 정규화.
# 빈 dict 의 stage_id 는 정규화 없음.
DEPRECATED_STAGE_PARAM_VALUES: dict[str, dict[str, dict[str, str]]] = {
    # v1.12.2 — s04_tool.rag_tool_mode 폐기. 옛 'both'/'context' 박힌 데이터는 stage 가 안 읽어
    # 무해. 정규화 mapping 도 제거.
    "s06_context": {
        # rag_pd_mode 'eager' (구 default) → 'progressive' (v1.1.1+ default).
        "rag_pd_mode": {"eager": "progressive"},
    },
}


def _normalize_active_strategies(active: dict[str, str]) -> dict[str, str]:
    """deprecated strategy 이름을 새 default 로 정규화 (v1.4.0).

    빈 문자열로 매핑되면 키 자체 삭제 → stage 의 default_impl 폴백.
    list_strategies 가 살아있는 stage (s02 / s05 / s08 / s09) 는 영향 없음.
    """
    if not isinstance(active, dict) or not active:
        return active or {}
    result: dict[str, str] = {}
    for sid, name in active.items():
        if not isinstance(name, str) or not name:
            continue
        mapping = DEPRECATED_STRATEGIES_BY_STAGE.get(sid)
        if mapping is None:
            result[sid] = name
            continue
        normalized = mapping.get(name, name)
        if normalized:
            result[sid] = normalized
        # normalized == "" → 키 삭제 (default_impl 폴백)
    return result


def _normalize_stage_params(stage_params: dict) -> dict:
    """deprecated stage_param value 를 v1.4.0 default 로 정규화."""
    if not isinstance(stage_params, dict) or not stage_params:
        return stage_params or {}
    result: dict = {}
    for sid, params in stage_params.items():
        if not isinstance(params, dict):
            result[sid] = params
            continue
        rules = DEPRECATED_STAGE_PARAM_VALUES.get(sid)
        if rules is None:
            result[sid] = params
            continue
        new_params = dict(params)
        for field_id, value_map in rules.items():
            if field_id in new_params and new_params[field_id] in value_map:
                new_params[field_id] = value_map[new_params[field_id]]
        result[sid] = new_params
    return result


def mark_stage_required(stage_id: str) -> None:
    """새 Stage 를 "비활성화 불가" 로 등록.

    외부 패키지가 자기 Stage 를 반드시 돌게 하려면 import 시 호출.
    기본 3개는 엔진이 이미 등록돼 있다.
    """
    if not isinstance(stage_id, str) or not stage_id.strip():
        raise ValueError("stage_id must be non-empty string")
    REQUIRED_STAGES.add(stage_id)


def unmark_stage_required(stage_id: str) -> None:
    """테스트/운영에서 필수 지정 해제."""
    REQUIRED_STAGES.discard(stage_id)


def get_required_stages() -> set[str]:
    """현재 필수 Stage id set 의 읽기용 스냅샷 복사."""
    return set(REQUIRED_STAGES)


@dataclass
class HarnessConfig:
    """하네스 파이프라인 설정 — 정책 default 는 외부(이식측) 가 owns.

    엔진은 모든 정책 field 를 sentinel(0/None/"") 로 둔다. 이식측이 사용자 입력을 받아
    `resolve_policy_defaults()` 같은 함수로 자기 비즈니스 정책 default 를 박는다.
    엔진은 어떤 도메인(웹/사내/규제) 인지 모르므로 default 를 박을 수 없다.

    정책 field 와 머신 상수 구분:
      - 정책: max_iterations / temperature / cost_budget_usd / max_tokens 등 — 사용자 결정
      - 머신: queue_size / preview_size 등 — 엔진 동작에 필요 (PHILOSOPHY §5)
    """

    # --- LLM (정책 — sentinel) ---
    provider: str = ""                    # "" → providers.get_default_provider() 런타임 해석
    model: str = ""                       # "" → PROVIDER_DEFAULT_MODEL 레지스트리 lookup
    temperature: Optional[float] = None   # 이식측이 박음 (도메인별 다름 — 창의/엄격)
    max_tokens: Optional[int] = None      # 이식측 (응답 max — 비용/지연 정책)
    aux_max_tokens: Optional[int] = None  # 보조 LLM 호출 max (s06 compact / s08 judge)

    # --- 폴백 모델 (provider 별) — PROVIDER_DEFAULT_MODEL 레지스트리가 단일 진실 소스 ---
    openai_model: str = ""
    anthropic_model: str = ""

    # --- 루프 제어 (정책 — sentinel) ---
    max_iterations: Optional[int] = None       # 도구 호출 횟수 cap
    max_tool_rounds: Optional[int] = None      # 도구 라운드 cap
    max_retries: Optional[int] = None          # 검증 재시도 한도
    validation_threshold: Optional[float] = None  # judge 통과 임계

    # --- 시스템 프롬프트 ---
    system_prompt: str = ""

    # --- 스테이지 토글 (False = 비활성) ---
    disabled_stages: set = field(default_factory=set)

    # --- 스테이지별 아티팩트 선택 ---
    artifacts: dict = field(default_factory=dict)  # stage_id → artifact_name

    # --- 스테이지별 파라미터 (UI에서 설정, 런타임에 반영) ---
    stage_params: dict = field(default_factory=dict)  # stage_id → {field_id: value}

    # --- Strategy 선택 (UI에서 클릭, stage_id → strategy impl_name) ---
    active_strategies: dict = field(default_factory=dict)  # stage_id → impl_name

    # --- Strategy 커스텀 변형 (디폴트 건드리지 않고 파라미터만 덮어쓴 복사본) ---
    # 외부 작업자가 "progressive_3level" 을 건드리지 않고 "progressive_v2" 라는
    # 복사본을 만들어 쓸 수 있게. active_strategies[stage_id] 가 variant 이름이면
    # base impl 의 클래스로 인스턴스 생성 후 params 를 configure 에 병합.
    #
    # 형식: {stage_id: [{"name": "progressive_v2",       # active_strategies 에서 참조할 이름
    #                    "base": "progressive_3level",    # 복제 원본 impl_name
    #                    "params": {"threshold": 20},     # configure 에 주입할 오버라이드
    #                    "label": "내 커스텀"              # UI 표시용 (옵션)
    #                   }]}
    strategy_variants: dict = field(default_factory=dict)

    # --- Capability 선언 (capability name 리스트, s04_tool가 자동 바인딩) ---
    capabilities: list = field(default_factory=list)       # ["retrieval.web_search", ...]
    capability_params: dict = field(default_factory=dict)  # capability_name → {param_id: value}

    # --- 외부 입력 선언 (컴파일 타겟) ---
    # 컴파일된 wheel 이 런타임에 요구하는 외부 값 계약. UI 가 이 선언을 보고
    # 배포 전 입력 폼을 자동 렌더. 컴파일러(xgen_harness.compile) 가 auto-scan 으로
    # ${VAR} 플레이스홀더를 발견하면 이 필드에 후보로 등록.
    #
    # 형식: {name: {"type": "secret"|"url"|"string"|"int"|"bool",
    #              "required": bool, "default": Any, "description": str}}
    external_inputs: dict = field(default_factory=dict)

    # --- 리소스 선언 (v1.10.6+, spec freeze 단일 진실 소스) ---
    # 워크플로우가 사용하는 외부 리소스 — 산출물 spec.config 에 freeze 되어
    # 외부 실행자가 자기 인프라에 같은 이름으로 wire 할 수 있게 함. 이식측이
    # `_merge_freeze_resources` wrapper 로 보강하던 7 키 (5/16 worklog) 를
    # 엔진측 dataclass 로 승격 — to_dict / from_dict / from_workflow 자동 처리.
    #
    # 값은 raw 보존 (list[str] / list[dict] / dict 등 워크플로우 박은 형식 그대로).
    # 컬렉션 이름의 UI 표시명 ↔ UUID 변환은 이식측 책임 (cluster DB 조회 필요).
    mcp_sessions: list = field(default_factory=list)         # MCP 세션 이름/메타 리스트
    rag_collections: list = field(default_factory=list)      # Qdrant 컬렉션 이름 리스트
    db_connections: list = field(default_factory=list)       # DB 연결 이름 리스트
    ontology_collections: list = field(default_factory=list) # GraphRAG / 온톨로지 컬렉션
    files: list = field(default_factory=list)                # 단일 파일 경로/메타
    folders: list = field(default_factory=list)              # 폴더 경로/메타
    node_overrides: dict = field(default_factory=dict)       # node_id → override dict

    # --- 기타 (정책 — sentinel) ---
    cost_budget_usd: Optional[float] = None     # 실행당 USD 예산 cap
    context_window: Optional[int] = None        # 컨텍스트 윈도우 (provider context limit)
    thinking_enabled: bool = False              # bool 은 명시 — false 가 기본 의미
    thinking_budget_tokens: Optional[int] = None  # extended thinking 토큰 예산

    # --- 관찰/디버깅 ---
    # True 면 Pipeline/Stage 내부 세밀한 이벤트(ServiceLookup / CapabilityBind /
    # StageSubstep / Retry) 가 추가 발행. 기본 False 라 기존 SSE 출력량 변화 없음.
    verbose_events: bool = False

    # --- 런타임 자기조정 게이트 (v1.24 — 자가설정 노드) ---
    # 엔진은 "실행 중 자기 config 를 되쓰는" 중립 메커니즘(RuntimeConfigMutator)만 제공하고,
    # 그 활성화 정책은 이식측이 이 한 값으로 opt-in 한다 (PHILOSOPHY: 엔진=메커니즘, 이식=정책).
    #   - "off"     : 기본. Mutator 의 모든 변이가 no-op (default-inert). 동작 변화 0.
    #   - "observe" : 변이를 적용하지 않고 제안(proposals)만 기록 — diff 가시화/HITL 용.
    #   - "act"     : algebra 로 legality 검증 + inverse 저널 후 라이브 적용 (롤백 가능).
    runtime_self_govern: str = "off"

    # --- Judge LLM (v1.1.0+) ---
    # s08_decide 의 judge_then_loop 가 사용하는 별도 평가 모델. 미지정(빈 문자열) 시
    # 본문 provider/model 재사용 — backward compat. 사용자 의도: "Judge 가 자기 답을
    # 자기가 평가하는 약점" 을 더 강한/저렴한 모델로 분리 평가 가능하게.
    judge_provider: str = ""
    judge_model: str = ""
    # v1.7.1 — 사용자 명시 "본문 재사용" 의도 (UI chip). True 면 judge_model 박혀
    # 있어도 무시하고 본문 LLM 재사용. False 일 때만 judge_model 값 사용 (빈 값은
    # backward "미설정 = 본문 재사용" 시맨틱). frontend self-describing 정합용.
    judge_use_main: bool = False

    # 레거시 호환
    preset: str = ""

    def __post_init__(self) -> None:
        """빈 provider 는 레지스트리 기반 기본값.

        v1.4.0 — deprecated strategy / stage_param 자동 정규화. 모든 인스턴스화
        경로 (cls(**kwargs), from_dict, from_workflow, 이식측 직접 cls(**config_kwargs))
        가 통과한다. DB 의 옛 워크플로우 row 가 token_budget / eager / both 같은 옛
        값을 박고 있어도 실행 시점에 자동 cascade / progressive / tool 로 정규화.
        """
        if not self.provider:
            from ..providers import get_default_provider
            self.provider = get_default_provider()
        # v1.4.0 — deprecated 정규화. 옛 이름 → 새 default.
        if self.active_strategies:
            self.active_strategies = _normalize_active_strategies(self.active_strategies)
        if self.stage_params:
            self.stage_params = _normalize_stage_params(self.stage_params)

    def get_active_stage_ids(self) -> list[str]:
        """활성 스테이지 ID 목록.

        v0.17.0 — `ALL_STAGES` 하드 리스트 + registry 병합. registry 에 등록된
        Stage 클래스가 `default_active` 프로퍼티로 True 를 반환하면 기본 활성
        취급. `disabled_stages` 는 양쪽 모두에 적용.
        """
        ids = [s for s in ALL_STAGES if s not in self.disabled_stages]

        # registry 에 있지만 ALL_STAGES 에 없는 Stage 중 default_active=True 만 합류.
        try:
            from .registry import _get_default_registry
            reg = _get_default_registry()
            for sid in reg.list_stages():
                if sid in ids or sid in self.disabled_stages:
                    continue
                try:
                    cls = reg.get(sid, "default")
                    if getattr(cls, "default_active", True):
                        ids.append(sid)
                except Exception:
                    continue
        except Exception:
            pass

        return ids

    def is_stage_active(self, stage_id: str) -> bool:
        return stage_id not in self.disabled_stages

    def toggle_stage(self, stage_id: str, active: bool) -> None:
        """스테이지 활성/비활성 토글. 필수 스테이지는 비활성화 불가."""
        if stage_id in REQUIRED_STAGES and not active:
            return
        if active:
            self.disabled_stages.discard(stage_id)
        else:
            self.disabled_stages.add(stage_id)

    def get_artifact_for_stage(self, stage_id: str) -> str:
        return self.artifacts.get(stage_id, "default")

    # ───────────────────────────────────────────────
    # 직렬화 — Builder 산출물을 파일로 저장/로드
    # ───────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """HarnessConfig 를 JSON-직렬화 가능한 dict 로 변환.

        하드코딩된 필드 나열 없이 `dataclasses.fields(self)` 로 자동 발견.
        새 필드 추가해도 자동 포함됨 (허브 정신 유지).

        - set 은 정렬된 list 로 변환
        - tuple 은 list
        - 중첩 dict/list 는 그대로 (JSON-safe 가정)
        """
        data: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, set):
                value = sorted(value)
            elif isinstance(value, tuple):
                value = list(value)
            data[f.name] = value
        data["_schema_version"] = 1
        return data

    def to_json(self, indent: int = 2) -> str:
        """JSON 문자열로 직렬화."""
        import json
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def to_workflow_data(
        self,
        *,
        workflow_id: Optional[str] = None,
        workflow_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """cluster ``workflow_meta.workflow_data`` 형태 dict 반환.

        역방향 SDK 양방향용 (v1.13) — Python 으로 작성한 HarnessConfig 인스턴스를
        cluster `POST /api/agentflow/harness/workflows` body 로 그대로 보낼 수 있다.

        ``from_workflow()`` 라운드트립 호환을 위해 ``None`` 인 top-level 필드는
        결과 dict 에서 제거 (from_workflow 가 일부 필드에 ``float(value)`` 강제
        변환을 하는데 None 이 박혀있으면 TypeError). dataclass default 가 적용되
        도록 키 자체를 빼는 게 안전.

        반환 형태:
            {
                "workflow_type": "harness",
                "workflow_id": ...,            # 옵션
                "workflow_name": ...,          # 옵션
                "nodes": [],                   # 하네스는 노드 그래프 없음
                "edges": [],
                "harness_config": {...},       # HarnessConfig.to_dict() 의 None 제거
            }
        """
        hc = self.to_dict()
        hc_clean = {k: v for k, v in hc.items() if v is not None}
        wd: dict[str, Any] = {
            "workflow_type": "harness",
            "nodes": [],
            "edges": [],
            "harness_config": hc_clean,
        }
        if workflow_id:
            wd["workflow_id"] = workflow_id
        if workflow_name:
            wd["workflow_name"] = workflow_name
        return wd

    def save(self, path: str) -> None:
        """JSON 파일로 저장."""
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HarnessConfig":
        """dict → HarnessConfig 역직렬화 (자동 필드 매핑).

        `dataclasses.fields(cls)` 를 순회해 선언된 필드만 통과. 알 수 없는
        키는 무시(forward-compat). set 타입 필드는 list→set 변환.
        """
        declared = {f.name: f for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for name, f in declared.items():
            if name not in data:
                continue
            value = data[name]
            # set 필드는 list → set 복원 (JSON 에선 list 로 저장됨)
            if f.default_factory is set:
                value = set(value) if value is not None else set()
                if name == "disabled_stages":
                    value = value - REQUIRED_STAGES
            kwargs[name] = value
        return cls(**kwargs)

    @classmethod
    def from_json(cls, text: str) -> "HarnessConfig":
        """JSON 문자열 → HarnessConfig."""
        import json
        return cls.from_dict(json.loads(text))

    @classmethod
    def load(cls, path: str) -> "HarnessConfig":
        """JSON 파일 → HarnessConfig."""
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_json(f.read())

    @classmethod
    def resolve(
        cls,
        sources: "list",
        *,
        register_runtime_defaults: bool = True,
    ) -> "HarnessConfig":
        """다중 source 에서 HarnessConfig 생성 (v1.10.0).

        5 단계 resolution chain 의 핵심. sources 는 우선순위 순서 (앞이 강함).
        sources[0] 의 값이 sources[1] / sources[2] / ... 의 같은 키를 덮음.
        None 항목은 자동 skip (FileConfigSource missing_ok 패턴).

        합쳐진 dict 의 `runtime_defaults` 키는 자동 추출 → `register_runtime_default`
        전역 호출 (서브에이전트 / fallback 환경값이 SDK 안 builtin 에 박힘).

        Args:
            sources: ConfigSource 구현체 리스트 (앞이 우선). None 항목 skip.
            register_runtime_defaults: True 면 `runtime_defaults` 키를 전역
                runtime_default 레지스트리에 등록 (default True).

        예:
            from xgen_harness.config import DictConfigSource, EnvConfigSource, FileConfigSource

            config = HarnessConfig.resolve(sources=[
                EnvConfigSource(prefix="XGEN_HARNESS_"),
                FileConfigSource("./xgen-harness.toml"),
                DictConfigSource(CLUSTER_DEFAULTS),
            ])
        """
        from ..config.sources import deep_merge

        # None 항목 skip (FileConfigSource missing_ok 시 None 박힐 수 있음)
        sources = [s for s in sources if s is not None]
        if not sources:
            return cls()

        # 우선순위 정책: sources[0] 이 가장 강함 → reversed 로 base 부터 차곡차곡 overlay
        merged: dict = {}
        for source in reversed(sources):
            loaded = source.load() if hasattr(source, "load") else {}
            if not isinstance(loaded, dict):
                continue
            merged = deep_merge(merged, loaded)

        # runtime_defaults 키 추출 → 전역 register (HarnessConfig 필드 아님)
        runtime_defaults = merged.pop("runtime_defaults", None) if register_runtime_defaults else None
        if runtime_defaults and isinstance(runtime_defaults, dict):
            from .runtime_defaults import register_runtime_default
            for rd_key, rd_value in runtime_defaults.items():
                register_runtime_default(rd_key, rd_value)

        return cls.from_dict(merged)

    @classmethod
    def from_workflow(cls, harness_config: dict[str, Any], workflow_data: dict[str, Any]) -> "HarnessConfig":
        """workflow_data에서 설정 생성"""
        agent_config = _extract_agent_config_from_nodes(workflow_data)

        # 비활성 스테이지 — 구 stage_id 도 canonical 로 정규화 (v0.11 alias)
        disabled = set()
        disabled_list = harness_config.get("disabled_stages", [])
        if isinstance(disabled_list, list):
            disabled = {_canonical(s) for s in disabled_list} - REQUIRED_STAGES

        # preset 키 풀어주기 — `harness_config.preset = "minimal" | "chat" | ...`
        # 만 박혀있으면 PRESETS 에서 disabled_stages / active_strategies /
        # max_iterations 를 펼쳐서 채운다. 사용자가 disabled_stages 를 명시했으면
        # 그게 우선 (사용자 명시 > preset).
        preset_name = (harness_config.get("preset") or "").strip()
        preset_strategies: dict[str, str] = {}
        preset_max_iter: int | None = None
        preset_temp: float | None = None
        if preset_name:
            try:
                from .presets import PRESETS as _PRESETS
                _p = _PRESETS.get(preset_name)
                if _p is not None:
                    if not disabled_list:
                        disabled = set(_p.disabled_stages) - REQUIRED_STAGES
                    preset_strategies = dict(_p.active_strategies)
                    preset_max_iter = _p.max_iterations
                    preset_temp = _p.temperature
                else:
                    # 등록되지 않은 preset 이름 — 외부 작업자가 PRESETS.register() 를
                    # 빠뜨렸거나 사용자 오타. silent fail 은 디버깅을 어렵게 한다.
                    import logging as _logging
                    _logger = _logging.getLogger("xgen_harness.config")
                    _logger.warning(
                        "[HarnessConfig] unknown preset=%r — 등록된 preset: %s. "
                        "사용자 명시 disabled_stages/active_strategies 그대로 적용.",
                        preset_name, sorted(_PRESETS.keys()),
                    )
            except Exception as _e:
                # presets 모듈 import 실패 시 silent — 기존 동작 유지. 단 debug 로그.
                import logging as _logging
                _logger = _logging.getLogger("xgen_harness.config")
                _logger.debug("[HarnessConfig] presets 모듈 import 실패: %s", _e)

        from ..providers import get_default_provider
        return cls(
            provider=(
                harness_config.get("provider")
                or agent_config.get("provider")
                or get_default_provider()
            ),
            # model 은 sentinel "" 로 받음 — 어댑터/s01_input 이 런타임에 PROVIDER_DEFAULT_MODEL 참조.
            model=harness_config.get("model") or agent_config.get("model", ""),
            temperature=float(
                harness_config.get(
                    "temperature",
                    agent_config.get(
                        "temperature",
                        preset_temp if preset_temp is not None else 0.7,
                    ),
                )
            ),
            max_tokens=int(harness_config.get("max_tokens", 8192)),
            aux_max_tokens=int(harness_config.get("aux_max_tokens", 500)),
            # provider 별 폴백은 런타임 PROVIDER_DEFAULT_MODEL 에서 해석. 여기선 명시값만 전달.
            openai_model=harness_config.get("openai_model") or agent_config.get("openai_model", ""),
            anthropic_model=harness_config.get("anthropic_model") or agent_config.get("anthropic_model", ""),
            system_prompt=harness_config.get("system_prompt") or agent_config.get("system_prompt", ""),
            max_iterations=int(
                harness_config.get(
                    "max_iterations",
                    preset_max_iter if preset_max_iter is not None else 10,
                )
            ),
            # max_retries 가 명시되지 않으면 max_iterations 와 동기화. 사유: UI 가
            # 통상 "최대 반복 N회" 한 컨트롤만 노출 → 사용자가 N=5 로 늘려도 retry
            # cap=3 이 별도로 잘랐던 회귀 (BUG-C). 두 변수 의미는 다르지만 (iteration
            # = 도구→LLM 루프 / retries = validation 재시도) UI 단일 출처를 보장.
            max_retries=int(
                harness_config.get(
                    "max_retries",
                    int(harness_config.get(
                        "max_iterations",
                        preset_max_iter if preset_max_iter is not None else 10,
                    )),
                )
            ),
            validation_threshold=float(harness_config.get("validation_threshold", 0.7)),
            # cost_budget_usd — 명시값만 전달(미설정이면 None 유지). max_retries/validation_threshold
            # 와 대칭. 누락 시 pypi 컴파일(from_workflow 경유)에서 예산이 떨어져 강제 안 됐다.
            cost_budget_usd=(
                float(harness_config["cost_budget_usd"])
                if harness_config.get("cost_budget_usd") is not None
                else None
            ),
            disabled_stages=disabled,
            artifacts=_alias_map(harness_config.get("artifacts", {})),
            stage_params=_alias_map(harness_config.get("stage_params", {})),
            # 사용자 명시 active_strategies > preset active_strategies. preset 만
            # 있으면 그걸로 채움. 둘 다 있으면 사용자가 박은 키만 override.
            active_strategies=_alias_map({
                **preset_strategies,
                **(harness_config.get("active_strategies") or {}),
            }),
            strategy_variants=_alias_map(dict(harness_config.get("strategy_variants", {}) or {})),
            thinking_enabled=bool(harness_config.get("thinking_enabled", False)),
            thinking_budget_tokens=int(harness_config.get("thinking_budget_tokens", 10000)),
            # v1.1.0 — harness_mode/use_planner 제거 (Planner OFF 직선 흐름 고정).
            # 기존 DB row 의 두 키는 from_dict 가 fields() 화이트리스트로 자동 무시.
            judge_provider=str(harness_config.get("judge_provider", "") or ""),
            judge_model=str(harness_config.get("judge_model", "") or ""),
            judge_use_main=bool(harness_config.get("judge_use_main", False)),
            # v0.11.21 — top-level context_window 전파 (파싱 실패 시 dataclass 기본값 200_000 유지)
            context_window=_safe_int(
                harness_config.get("context_window"), default=200_000, minimum=1024,
            ),
            capabilities=list(harness_config.get("capabilities", []) or []),
            capability_params=dict(harness_config.get("capability_params", {}) or {}),
            external_inputs=dict(harness_config.get("external_inputs", {}) or {}),
            # v1.10.6 — 7 리소스 필드 raw 보존 (spec freeze 단일 진실 소스).
            # 이식측 wrapper 가 보강하던 영역을 엔진측 root 로 승격.
            mcp_sessions=list(harness_config.get("mcp_sessions", []) or []),
            rag_collections=list(harness_config.get("rag_collections", []) or []),
            db_connections=list(harness_config.get("db_connections", []) or []),
            ontology_collections=list(harness_config.get("ontology_collections", []) or []),
            files=list(harness_config.get("files", []) or []),
            folders=list(harness_config.get("folders", []) or []),
            node_overrides=dict(harness_config.get("node_overrides", {}) or {}),
            preset=preset_name,
        )


def _safe_int(value: Any, *, default: int, minimum: int | None = None) -> int:
    """빈 문자열/None/파싱 실패 시 default. minimum 미달 시 default."""
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


def _extract_agent_config_from_nodes(workflow_data: dict) -> dict[str, Any]:
    """에이전트 노드에서 provider/model/system_prompt 추출"""
    for node in workflow_data.get("nodes", []):
        data = node.get("data", {})
        if not data.get("id", "").startswith("agents/"):
            continue
        parameters = data.get("parameters", [])
        if not isinstance(parameters, list):
            continue

        def _get(pid: str) -> str:
            for p in parameters:
                if p.get("id") == pid:
                    v = p.get("value")
                    return str(v) if v is not None else ""
            return ""

        provider = _get("provider")
        model = _get("model")
        if not model:
            # 프로바이더별 default 모델 조회 — 레지스트리(providers/__init__.py)에서 해석.
            # 새 프로바이더 추가 시 config.py 수정 불필요.
            try:
                from ..providers import get_default_model
                per_provider_key = f"{provider}_model"
                model = _get(per_provider_key) or get_default_model(provider)
            except Exception as e:
                _cfg_logger.debug("default model resolution failed (provider=%s): %s", provider, e)
                model = ""

        # 프로바이더별 폴백 모델 (하드코딩 제거) — 레지스트리 기반
        try:
            from ..providers import get_default_model, list_providers
            per_provider_defaults = {
                f"{p}_model": _get(f"{p}_model") or get_default_model(p)
                for p in list_providers()
            }
        except Exception as e:
            _cfg_logger.debug("providers registry unavailable, per_provider_defaults 비움: %s", e)
            per_provider_defaults = {}

        return {
            "provider": provider,
            "model": model,
            **per_provider_defaults,
            "system_prompt": _get("system_prompt"),
            "temperature": _get("temperature") or "0.7",
        }
    return {}
