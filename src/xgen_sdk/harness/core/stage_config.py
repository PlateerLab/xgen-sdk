"""
스테이지별 설정 스키마 — UI에서 각 스테이지 클릭 시 보여줄 설정 항목

각 스테이지마다:
- 설명 (한국어/영어)
- 설정 가능한 필드 (type, label, options, default)
- 기술적 동작 설명
"""

import logging
from typing import Optional

_sc_logger = logging.getLogger("harness.core.stage_config")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stage ID Alias — v0.11.0 리네이밍 하위호환 레이어
# 구 저장 워크플로우 / 외부 갤러리 wheel 이 구 id 를 보내와도 내부에서 새 id 로 변환.
# Phase 1 (v0.11.x): 양쪽 다 수용. Phase 2 (v0.12+): 구 id 경고 → 제거.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STAGE_ID_ALIASES: dict[str, str] = {
    # 기존 0.x 시리즈 별칭
    "s02_memory":        "s02_history",
    "s03_system_prompt": "s03_prompt",
    "s04_tool_index":    "s04_tool",
    "s07_llm":           "s00_harness",   # 본문 LLM 호출은 s00 이 소유
    "s08_execute":       "s07_act",
    "s08_act":           "s07_act",
    # v1.0 — 11→10 통합 (s05_strategy 분해 / judge·save 격하 / publish 삭제)
    "s05_plan":          "s03_prompt",    # CoT 는 prompt 로 흡수
    "s05_strategy":      "s03_prompt",    # 동일
    "s09_validate":      "s08_decide",
    "s09_judge":         "s08_decide",    # judge → decide 의 judge_then_loop strategy
    "s08_judge":         "s08_decide",
    "s09_decide":        "s08_decide",    # 번호 −1 시프트
    "s10_decide":        "s08_decide",
    "s11_save":          "s09_finalize",  # save → finalize 의 persist strategy
    "s10_save":          "s09_finalize",
    "s12_finalize":      "s09_finalize",  # 번호 −2 시프트
    "s12_complete":      "s09_finalize",
    "s11_finalize":      "s09_finalize",
}


def canonical_stage_id(sid: str) -> str:
    """구 stage_id 가 들어와도 새 id 로 정규화. 이미 새 id 면 그대로."""
    if not isinstance(sid, str):
        return sid
    return STAGE_ID_ALIASES.get(sid, sid)


def canonical_stage_id_map(d: dict) -> dict:
    """dict 의 key 를 stage_id 로 간주하고 canonical 로 정규화한 새 dict 반환.

    구 id / 새 id 가 충돌하면 새 id 값이 유지 (나중 병합 승리).
    """
    if not isinstance(d, dict):
        return d
    out: dict = {}
    for k, v in d.items():
        canonical = canonical_stage_id(k) if isinstance(k, str) else k
        out[canonical] = v
    return out


STAGE_CONFIGS: dict[str, dict] = {
    "s00_harness": {
        "description_ko": "에이전트가 답변할 때 LLM 모델을 호출합니다. 응답을 실시간으로 받아오고, 일시 오류 시 자동 재시도합니다.",
        "description_en": "Calls the LLM to generate the agent's reply. Streams the response and retries on transient errors.",
        "when_to_use": "항상 활성. 비활성하면 답변 자체가 불가.",
        "when_to_skip": "비활성 불가.",
        "cost_hint": "medium",
        "icon": "🎯",
        "fields": [
            {
                # v0.15.1 자동 연동 — options 리터럴 제거. options_source 로
                # HarnessStage.list_strategies() 결과를 동적 주입 (StrategyResolver
                # _REGISTRY 에서 실측). 외부 플러그인이 새 Transport 등록하면 즉시 UI
                # 드롭다운에 합류.
                "id": "strategy",
                "label": "Transport Strategy",
                "type": "select",
                "options_source": "s00_harness_transport_strategies",
                "default": None,
                "description": "본문 LLM 호출 방식. 등록된 Transport Strategy 중 선택.",
            },
            # max_tokens / thinking_enabled / thinking_budget 은 HarnessConfig top-level
            # (전역 ConfigPanel) 단일 진실 소스. stage_params 에 박혀도 엔진이 안 읽어 박제
            # 였음 (v0.29.1 audit). stage detail 에서 제거하고 전역 패널에서만 만지도록 일원화.
        ],
        "behavior": [
            "에이전트 루프 안에서 본문 LLM 을 호출합니다",
            "응답을 실시간 스트리밍으로 받습니다",
            "일시적 오류 (속도 제한 / 서버 과부하) 시 자동 재시도",
            "모델 응답 실패 시 다른 모델로 자동 전환 (예: Anthropic → OpenAI)",
            "동일한 시스템 프롬프트는 캐시 활용으로 비용 절감",
        ],
    },
    "s01_input": {
        # v0.14.0 s01: **사용자 입력 정규화 전용**. LLM provider/model/temperature
        # 선택은 s00_harness 관할 (본문 LLM 호출 stage) — s01 에서 필드로 노출하지 않음.
        "description_ko": "사용자가 입력한 메시지와 첨부 파일을 검증해서 LLM 이 이해하는 형식으로 변환합니다.",
        "description_en": "Validates the user message and normalizes attachments into LLM-readable blocks.",
        # v0.12.0 self-describing — Planner 가 이 세 필드만 보고 선택/제외/파라미터 조정.
        # 시스템 프롬프트 가이드 대신 "환경이 스스로 말한다" (REAL_HARNESS).
        "when_to_use": "항상 (필수). 빈 입력 거부 + 첨부 파일 정규화 + 첫 user 메시지 push.",
        "when_to_skip": "불가 (REQUIRED_STAGES).",
        "cost_hint": "low",
        "icon": "📥",
        # v0.26.0 — provider 필드 제거 (D1).
        # 이전엔 stage_param 으로 노출했으나 stage.py 자기 docstring 에 "s01 은 읽지도
        # 쓰지도 않는다" 라고 명시. provider 결정은 HarnessConfig top-level (ConfigPanel)
        # 가 단일 진실 소스. 두 곳 노출은 사용자 거짓말 (UI 클릭 → 환경 무반영).
        "fields": [],
        "behavior": [
            "빈 입력은 거부",
            "이미지 첨부는 base64 인코딩, 텍스트 파일은 본문에 포함",
            "변환된 메시지를 대화에 추가",
        ],
    },
    "s02_history": {
        "description_ko": "같은 대화창의 이전 메시지·답변을 자동으로 불러와 멀티턴 맥락을 유지합니다.",
        "when_to_use": "이전 대화 맥락이 필요한 멀티턴 / 이전 답변 재사용 / 관련 문서 상기.",
        "when_to_skip": "단일 메시지 답변 / 새 대화 / 이력 영향 없는 단발 질문.",
        "cost_hint": "medium",
        "description_en": "Loads conversation history, results, and related documents.",
        "icon": "🧠",
        "fields": [
            {
                "id": "max_history",
                "label": "최대 이력 수",
                "type": "number",
                "min": 1,
                "max": 20,
                "default": None,
            },
            # v0.29.1 — embedding_search 전략 임계 2종 노출 (코드는 이미 read 중인데
            # UI 통로가 없어 사용자가 못 박던 것 — audit 로 발견).
            # v1.0.8 — memory_collection UI 필드 제거 (실제 안 쓰임). 코드는
            # get_param("memory_collection", state, "memory") fallback 으로 그대로 동작.
            {
                "id": "memory_top_k",
                "label": "메모리 검색 상위 K",
                "type": "number",
                "min": 1,
                "max": 20,
                "default": None,
                "description": "embedding_search 가 컬렉션에서 가져올 상위 K 결과 수.",
            },
            {
                "id": "memory_score_threshold",
                "label": "메모리 점수 임계",
                "type": "slider",
                "min": 0,
                "max": 1,
                "step": 0.05,
                "default": None,
                "description": "이 점수 이상 결과만 system_prompt 에 주입. 0 이면 전체 포함.",
            },
        ],
        "behavior": [
            "데이터베이스에서 최근 대화 기록을 가져옵니다",
            "여러 저장소 자동 폴백 (실행 로그 → 입출력 기록 → 채팅 세션)",
            "각 메시지는 2,000자까지만 (긴 응답은 잘림)",
            "임베딩 검색으로 관련 과거 메시지만 골라서 주입 (선택 시)",
        ],
    },
    "s03_prompt": {
        "description_ko": "시스템 프롬프트를 조립합니다. 에이전트의 역할 / 행동 규칙 / 사용 가능 도구 안내를 자동으로 구성합니다.",
        "when_to_use": "일반 요청 (역할·규칙·도구 안내가 필요한 경우).",
        "when_to_skip": "이미 완성된 시스템 프롬프트가 있고 추가 조립이 불필요한 경우.",
        "cost_hint": "low",
        "description_en": "Assembles system prompt defining agent role and rules.",
        "icon": "📝",
        "fields": [
            {
                "id": "system_prompt",
                "label": "시스템 프롬프트",
                "type": "textarea",
                "placeholder": "에이전트의 역할을 정의하세요...",
                "default": None,
            },
            # v0.29.1 — prompt_id 필드 제거 (audit). stage.py 어디서도 안 읽음 — dead spec.
            # 향후 prompt store wiring 추가 시 이식측 (harness_bridge) 가 prompt_id 받아
            # state.config.system_prompt 에 fetch & inject 한 뒤 엔진에 넘기는 패턴 권장.
            {
                "id": "include_rules",
                "label": "기본 규칙 포함",
                "type": "toggle",
                "default": None,
            },
            {
                "id": "citation_mode",
                "label": "인용 모드",
                "type": "select",
                "options": ["off", "enabled", "strict", "auto"],
                "default": None,
                "description": "off: 인용 지시 없음 / enabled: [DOC_n] 인용 권장 / strict: 참조 문서 밖 정보 답변 금지 (환각 방지) / auto: RAG context 패턴 감지 후 자동 strict/off (v0.11.17+ 실험적)",
            },
            {
                "id": "citation_auto_doc_tokens",
                "label": "인용 auto — 문서형 토큰 (override)",
                "type": "multi_select",
                "options": [],
                "default": [],
                "description": "citation_mode=auto 휴리스틱에 추가할 문서형 collection 토큰. 기본값(doc/report/regulation/...) 에 OR 결합. 회사·언어 특화 명사 주입용.",
            },
            {
                "id": "citation_auto_prod_tokens",
                "label": "인용 auto — 상품형 토큰 (override)",
                "type": "multi_select",
                "options": [],
                "default": [],
                "description": "citation_mode=auto 휴리스틱에 추가할 상품형 collection 토큰. 기본값(product/stock/inventory/...) 에 OR 결합.",
            },
            # v1.0 — 구 s05_strategy 분해 흡수: thinking_mode 카드 (Strategy 카드와 자동 매핑)
            {
                "id": "thinking_mode",
                "label": "사고 모드 (CoT/ReAct)",
                "type": "select",
                "options": ["auto", "cot", "react", "none"],
                "default": None,
                "description": "⚠ Strategy 카드(cot_planner/react/none) 픽이 우선 — 카드를 골랐으면 이 select 무시. 카드 미선택 시에만 이 값 사용. auto: input_complexity 보고 simple→none / moderate→cot / complex→react 자동 (s01 의 with_classification strategy 가 활성일 때만 실의미). cot: 단계별 계획 지시 추가. react: Thought→Action→Observation 루프. none: 비활성.",
            },
            {
                "id": "planning_instruction_template",
                "label": "사고 모드 raw 템플릿 override",
                "type": "textarea",
                "default": None,
                "description": "비워두면 thinking_mode 의 등록 템플릿 사용. 채우면 그 raw 텍스트를 planning_instruction 으로 사용. register_thinking_mode() 또는 entry_points(xgen_harness.prompt_templates) 로도 등록 가능.",
            },
            {
                "id": "identity_template",
                "label": "Identity 템플릿 이름",
                "type": "text",
                "default": None,
                "description": "register_identity() 또는 entry_points 로 등록한 이름. 기본 'default' 외 외부 패키지가 등록한 이름 가능.",
            },
            {
                "id": "rules_template",
                "label": "Rules 템플릿 이름",
                "type": "text",
                "default": None,
                "description": "register_rules() 또는 entry_points 로 등록한 이름.",
            },
        ],
        "behavior": [
            "섹션 순서: 역할 → 규칙 → 사고 모드 → 도구 안내 → 참고 자료 → 대화 이력",
            "토큰 예산이 부족하면 우선순위가 낮은 섹션부터 줄입니다",
            "엄격 모드: 제공된 문서에 없는 정보는 '모름' 으로 답변 (환각 방지)",
            "사고 가이드는 첫 응답 시도에만 주입 (재시도 시 생략)",
        ],
    },
    "s04_tool": {
        "description_ko": "에이전트가 쓸 도구를 고릅니다. 명시 선택한 도구는 즉시 호출 가능, 그 외 도구는 이름만 보이고 에이전트가 필요할 때 직접 불러와 사용합니다. 컨텍스트가 도구 정보로 무거워지지 않게 자동으로 절약합니다.",
        "when_to_use": "외부 도구·RAG 컬렉션·GraphRAG 중 하나 이상 필요한 경우.",
        "when_to_skip": "LLM 내재 지식만으로 충분한 일반 잡담·단순 QA·창작.",
        "cost_hint": "low",
        "description_en": "Select tools the agent can use. Whitelisted tools are immediately callable; others are listed by name only and loaded on demand by the agent — keeps context lightweight. RAG and GraphRAG collections are exposed as builtin search tools.",
        "icon": "🔧",
        "fields": [
            # v0.25.0 — 단일 도구 공급 경로. source_id → 허용 도구 이름 리스트.
            #   키 없음 = 해당 소스 전체 포함.
            #   빈 리스트 = 소스 비활성.
            #   이름 리스트 = 그 도구만 포함.
            # 프론트가 /api/harness/tool-sources 응답으로 Box 를 동적 렌더한 뒤,
            # 사용자가 Box 안 체크박스를 켠 도구 이름만 채워 준다.
            {
                "id": "selected_tools",
                "label": "Selected Tools (by source)",
                "type": "object",
                "default": {},
                "description": "source_id → 허용 도구 이름 리스트. 키 없음=소스 전체, 빈 리스트=소스 비활성.",
            },
            # 각 ToolSource 의 filter_schema 에 따라 프론트가 sub-UI 를 렌더하고,
            # 사용자가 선택한 값이 여기 저장된다. 예:
            #   {"mcp-sessions": {"session_ids": ["abc"]},
            #    "xgen-nodes":   {"tags": ["api", "database"]}}
            {
                "id": "tool_source_filters",
                "label": "Tool Source Filters",
                "type": "object",
                "default": {},
                "description": "소스별 list_tools 필터 파라미터 맵.",
            },
            {
                "id": "rag_collections",
                "label": "문서 컬렉션 (RAG)",
                "type": "multi_select",
                "options_source": "rag-collections",
                "default": [],
                "description": "검색할 문서 컬렉션을 선택하세요",
            },
            {
                "id": "rag_top_k",
                "label": "검색 결과 수 (Top-K)",
                "type": "number",
                "min": 1,
                "max": 20,
                "default": None,
            },
            {
                "id": "builtin_tools",
                "label": "빌트인 도구",
                "type": "multi_select",
                "options": ["discover_tools", "calculator", "web_search"],
                "default": ["discover_tools"],
            },
            {
                "id": "force_tool_use",
                "label": "도구 호출 강제 (v0.11.19+)",
                "type": "toggle",
                "default": None,
                "description": "활성 시 LLM 이 반드시 tool 하나를 호출하게 강제 (OpenAI tool_choice=required, Anthropic type=any). tool_result 누적 → L3 microcompact 발동 조건.",
            },
            # v1.12.2 — rag_tool_mode 폐기. v1.9.0 Option C 라디칼 (단일 도구 경로) 이후
            # 어디서도 read 안 됨. DEPRECATED_STAGE_PARAM_VALUES 도 같이 제거.
            # capabilities top-level: state.config.capabilities (전역 ConfigPanel) 만 사용.
            # v1.0 — 자연어 intent → capability 자동 발견 (구 s05_strategy 흡수)
            {
                "id": "capability_discovery",
                "label": "Capability 자동 발견 활성",
                "type": "toggle",
                "default": None,
                "description": "켜면 user_input 매칭으로 capability 후보를 자동 발견 + 바인딩. Strategy 카드 'capability_auto' 픽으로도 동일 효과.",
            },
            {
                "id": "capability_top_k",
                "label": "Capability 후보 수",
                "type": "number",
                "min": 1,
                "max": 10,
                "default": None,
                "description": "user_input 매칭으로 가져올 상위 K. CAPABILITY_DISCOVERY_DEFAULTS['top_k'] override.",
            },
            {
                "id": "capability_min_score",
                "label": "Capability 매칭 임계",
                "type": "slider",
                "min": 0,
                "max": 1,
                "step": 0.05,
                "default": None,
                "description": "이 점수 이상 후보만 채택. CAPABILITY_DISCOVERY_DEFAULTS['min_score'] override.",
            },
        ],
        "behavior": [
            "모든 도구는 한 곳에서 자동 수집됩니다 (MCP 서버 / API 도구 / xgen 노드 등)",
            "사용자가 선택한 도구는 즉시 호출 가능 / 선택 안 한 도구는 이름만 보임",
            "에이전트가 필요할 때 도구를 불러오면, 다음 응답부터 직접 사용 가능합니다",
            "도구가 12개 이상이면 에이전트가 키워드로 도구를 검색할 수 있습니다",
            "특정 도구의 사용법(파라미터)만 미리 보기 가능",
            "긴 도구 결과는 미리보기만 보여주고, 본문은 필요할 때 다시 불러옵니다",
            "RAG 컬렉션을 박으면 검색 도구가 자동 등록되어 에이전트가 직접 호출 — 검색 결과는 요약 먼저, 본문은 lazy",
            "GraphRAG 컬렉션을 박으면 그래프 검색 도구도 자동 등록 (빌드 완료된 컬렉션만 동작)",
            "외부 도구 추가는 플러그인 등록만으로 자동 합류 (코드 수정 불필요)",
        ],
    },
    # v1.0 — s05_strategy stage 삭제. Fields 분배:
    #   planning_mode → s03_prompt.thinking_mode (CoT/ReAct)
    #   intent_rules  → s06_context (RAG metadata 자동 라우팅)
    #   capability_*  → s04_tool (자연어 intent → capability 자동 발견)
    "s06_context": {
        "description_ko": "에이전트가 답변할 때 참조할 자료 (RAG 컬렉션 / 데이터베이스 / 폴더 / GraphRAG) 를 선택합니다. 검색은 에이전트가 도구로 직접 호출하므로 매번 자동 검색하지 않습니다. 대화가 길어지면 자동으로 단계별 압축됩니다.",
        "when_to_use": "참조할 자료가 필요한 질문 / 긴 대화에서 자동 압축이 필요할 때.",
        "when_to_skip": "짧은 단발 질문이고 참조 자료가 필요 없을 때.",
        "cost_hint": "medium",
        "description_en": "Searches knowledge resources (RAG collections, DB, folders, files, GraphRAG) and injects them as context before the LLM answers. Auto-compacts (Cascade L3~L5) when over the token budget.",
        "icon": "📊",
        "fields": [
            {
                "id": "context_window",
                "advanced": True,
                "label": "컨텍스트 윈도우",
                "type": "number",
                "min": 10000,
                "max": 1000000,
                "step": 10000,
                "default": None,
            },
            {
                "id": "compaction_threshold",
                "label": "압축 시작 (% 사용)",
                "type": "slider",
                "min": 50,
                "max": 95,
                "step": 5,
                "default": None,
            },
            {
                "id": "score_threshold",
                "label": "RAG 유사도 임계값",
                "type": "slider",
                "min": 0,
                "max": 1,
                "step": 0.05,
                "default": None,
                "description": "0 이면 필터링 없음. 임계 이상 점수의 검색 결과만 컨텍스트에 포함 (precision 도구)",
            },
            {
                "id": "rerank_top_k",
                "advanced": True,
                "label": "리랭크 상위 K",
                "type": "number",
                "min": 1,
                "max": 20,
                "default": None,
                "description": "reranker 가 활성일 때 재정렬 후 유지할 상위 청크 수 (미설정 시 rag_top_k 사용)",
            },
            {
                "id": "reranker",
                "advanced": True,
                "label": "리랭커 활성",
                "type": "toggle",
                "default": None,
                "description": "켜면 xgen-documents 리랭커로 검색 결과 재정렬. provider 는 서버 기동 시 설정된 값 사용",
            },
            # v0.11.23 — 이식 options_source 5종 공식 선언. 이전에 harness_options_registry 수동 매핑.
            {
                "id": "ontology_collections",
                "label": "Ontology / GraphRAG",
                "type": "multi_select",
                "options_source": "ontology-collections",
                "default": [],
                "description": "GraphRAG / ontology 검색용 컬렉션.",
            },
            {
                "id": "folders",
                "label": "Folders (컬렉션 그룹)",
                "type": "multi_select",
                "options_source": "folders",
                "default": [],
                "description": "스토리지 폴더 선택 — 폴더 안 컬렉션 자동 확장.",
            },
            # v0.26.1 — files 필드 부활 (실 wiring 추가).
            # v0.26.0 에선 dead 라 제거했었음. 이제 stage.py 가 이 필드를 read 해서
            # metadata_filter 의 `file_name` 키로 자동 라우팅 (union with 사용자 textarea).
            # frontend 의 files multi_select UI 가 진짜 검색 범위 좁히기로 작동.
            {
                "id": "files",
                "label": "Files (업로드 파일)",
                "type": "multi_select",
                "options_source": "files",
                "default": [],
                "description": "업로드된 파일 개별 선택 — metadata_filter.file_name 으로 자동 라우팅되어 검색 범위 제한.",
            },
            {
                "id": "db_connections",
                "label": "DB Connections",
                "type": "multi_select",
                "options_source": "db-connections",
                "default": [],
                "description": "DB 연결 선택 — 스키마 요약이 system_prompt 에 주입.",
            },
            # v1.12.2 — enhance_prompt 폐기. stage 코드 어디서도 read 안 됨 (builder.py 만
            # programmatic 박지만 stage 가 무시). UI textarea 였다면 사용자 입력 무반응 함정.
            {
                "id": "metadata_filter",
                "label": "메타데이터 필터 (JSON)",
                "type": "textarea",
                "placeholder": '예: {"file_name": "products.csv"}  ← 해당 파일 청크만 검색',
                "default": None,
                "description": "DocumentSearchRequest.filter 로 전달. 특정 파일/폴더로 검색 범위를 좁혀 recall 향상. JSON 객체 문자열",
            },
            {
                "id": "rag_pd_mode",
                "advanced": True,
                "label": "RAG Progressive Disclosure",
                "type": "select",
                "options": ["eager", "progressive"],
                # v1.1.1 — default progressive. eager 는 캔버스 정보검색 노드와 동일 동작이라
                # 하네스 차별화 0. progressive 면 인덱스만 노출 → fetch_pd lazy fetch.
                # 이식측 PORT_POLICY_DEFAULTS 와 정합.
                "default": "progressive",
                "description": "eager: 청크 본문 전체를 system_prompt 에 주입. progressive (기본): 인덱스 한 줄 + snippet 만 주입, 본문은 pd_stores 에 보관 — LLM 이 fetch_pd(kind='rag', id=...) 로 필요한 것만 pull (Claude Code 패턴)",
            },
            # v1.12.2 — rag_ingestion_mode 폐기. v1.9.0 Option C 이후 stage 코드 read 0건.
            {
                "id": "chars_per_token",
                "advanced": True,
                "label": "토큰 당 문자 수 (언어별 조정, v0.11.20+)",
                "type": "number",
                "min": 1,
                "max": 10,
                "default": None,
                "description": "estimated_tokens = total_chars / chars_per_token. 기본 3 은 혼합 언어 평균. 영어 위주면 4, 한국어 위주면 2 권장.",
            },
            {
                "id": "rag_pd_snippet_size",
                "advanced": True,
                "label": "RAG PD snippet 크기 (자)",
                "type": "number",
                "min": 40,
                "max": 500,
                "step": 20,
                "default": None,
                "description": "progressive 모드에서 인덱스 한 줄에 노출할 청크 앞부분 미리보기 크기",
            },
            {
                "id": "strategy",
                "label": "압축 전략",
                "type": "select",
                "options": ["token_budget", "sliding_window", "microcompact", "context_collapse_overlay", "autocompact_llm", "cascade"],
                "default": None,
                "description": "token_budget: 기본 파괴적 3단계 / sliding_window: 최근 N 개 / microcompact (L3): 오래된 tool_result 만 placeholder (Claude Code L3) / context_collapse_overlay (L4): 비파괴 overlay (Claude Code L4) / autocompact_llm (L5): child LLM 9-section summary (Claude Code L5) / cascade: 임계별 L3→L4→L5 자동 선택 (Claude Code Cascade)",
            },
            {
                "id": "cascade_l3_threshold",
                "advanced": True,
                "label": "Cascade L3 발동 임계 (%)",
                "type": "slider",
                "min": 50,
                "max": 90,
                "step": 5,
                "default": None,
                "description": "cascade 전략에서 이 비율 이상이면 L3 microcompact 먼저 시도 (tool_result 교체, 경량). 미설정 시 runtime_default 70% 적용. Pilot #11 에서 조기 발동(<70) 의 -19% 품질 악화 관측.",
            },
            {
                "id": "cascade_l4_threshold",
                "advanced": True,
                "label": "Cascade L4 발동 임계 (%)",
                "type": "slider",
                "min": 60,
                "max": 95,
                "step": 5,
                "default": None,
                "description": "cascade 전략에서 이 비율 이상이면 L4 context_collapse_overlay 추가 발동 (비파괴 overlay, 중량). 미설정 시 runtime_default 85%.",
            },
            {
                "id": "cascade_l5_threshold",
                "advanced": True,
                "label": "Cascade L5 발동 임계 (%)",
                "type": "slider",
                "min": 70,
                "max": 99,
                "step": 1,
                "default": None,
                "description": "cascade 전략에서 이 비율 이상이면 최후 수단 L5 autocompact_llm 발동 (child LLM 요약). 미설정 시 runtime_default 95%.",
            },
            {
                "id": "context_collapse_threshold",
                "advanced": True,
                "label": "L4 Collapse 임계 (%)",
                "type": "slider",
                "min": 50,
                "max": 95,
                "step": 5,
                "default": None,
                "description": "context_collapse_overlay 전략에서 이 비율 이상 토큰 사용 시 collapse 발동 (Claude Code 기본 90%)",
            },
            {
                "id": "context_collapse_keep_tail",
                "advanced": True,
                "label": "L4 보존 tail 메시지 수",
                "type": "number",
                "min": 1,
                "max": 10,
                "default": None,
                "description": "collapse 후 messages 말미에 온전히 남길 최근 메시지 개수",
            },
            {
                "id": "microcompact_threshold",
                "advanced": True,
                "label": "L3 Microcompact 임계 (%)",
                "type": "slider",
                "min": 50,
                "max": 95,
                "step": 5,
                "default": None,
                "description": "microcompact 전략에서 이 비율 이상 토큰 사용 시 오래된 tool_result 를 placeholder 로 교체 (원본은 pd_stores['tool_result'] 에서 복원 가능)",
            },
            {
                "id": "microcompact_keep_recent",
                "advanced": True,
                "label": "L3 유지 tool_result 개수",
                "type": "number",
                "min": 1,
                "max": 20,
                "default": None,
                "description": "messages 내 최근 N 개 tool_result 만 원본 유지, 나머지는 placeholder",
            },
            {
                "id": "autocompact_threshold",
                "advanced": True,
                "label": "L5 Autocompact 임계 (%)",
                "type": "slider",
                "min": 50,
                "max": 95,
                "step": 5,
                "default": None,
                "description": "autocompact_llm 전략에서 이 비율 이상 토큰 사용 시 child LLM 요약 발동 (Claude Code 기본 87%). 연속 실패 3회 시 회로 차단",
            },
            {
                "id": "autocompact_keep_tail",
                "advanced": True,
                "label": "L5 보존 tail 메시지 수",
                "type": "number",
                "min": 1,
                "max": 10,
                "default": None,
                "description": "autocompact 후 messages 말미에 온전히 남길 최근 메시지 개수",
            },
            # v0.29.2 — sliding_window strategy 의 윈도우 크기 (코드는 이미 read 중).
            {
                "id": "window_size",
                "advanced": True,
                "label": "Sliding Window 크기 (메시지 수)",
                "type": "number",
                "min": 1,
                "max": 100,
                "default": None,
                "description": "strategy=sliding_window 일 때 messages 말미에서 유지할 최근 메시지 개수. 다른 압축 strategy 에서는 무시.",
            },
            # v1.0 — Intent Routing 흡수 (구 s05_strategy)
            {
                "id": "intent_rules",
                "advanced": True,
                "label": "Intent Routing 규칙 (JSON)",
                "type": "textarea",
                "placeholder": '예: [{"keywords":["상품","product"],"filter":{"file_name":"products.csv"}}]',
                "default": None,
                "description": "쿼리 키워드 → metadata_filter 자동 매핑. 매칭되면 metadata_filter 가 비어있을 때 auto_metadata_filter 로 사용 (명시 filter 우선). JSON 배열 문자열.",
            },
        ],
        "behavior": [
            "대화가 길어지면 토큰 양에 따라 단계적으로 자동 압축됩니다",
            "1단계: 오래된 도구 결과를 짧은 요약으로 대체 (원본은 보관 — 필요시 다시 가져옴)",
            "2단계: 오래된 메시지를 요약으로 대체 (원본은 보관)",
            "3단계: 별도 LLM 으로 대화 전체를 9개 섹션으로 압축",
            "RAG 컬렉션 검색은 에이전트가 직접 도구로 호출 (매 턴 자동 검색 X — 비용 절감)",
            "GraphRAG 도 마찬가지 — 에이전트가 필요할 때만 호출",
            "검색 결과는 요약만 먼저 보여주고, 본문은 필요할 때 다시 가져옵니다",
            "긴 자료는 모두 같은 방식으로 절약 (도구 결과 / RAG / 그래프 / 대화 이력)",
            "외부 압축 방식 추가는 플러그인 등록만으로 자동 합류",
        ],
    },
    # 본문 LLM 호출 관련 필드는 s00_harness 로 이관.
    "s07_act": {
        "description_ko": "에이전트가 호출한 도구를 실제로 실행합니다 (MCP 도구 / 사용자 API / 빌트인 도구).",
        "when_to_use": "에이전트가 도구를 호출했을 때 자동 진입 (수동 결정 불필요).",
        "when_to_skip": "도구 호출 없이 텍스트만으로 답변하는 요청.",
        "cost_hint": "variable",
        "description_en": "Executes tool calls from LLM response.",
        "icon": "⚡",
        "fields": [
            {
                "id": "timeout",
                "label": "도구 실행 타임아웃 (초)",
                "type": "number",
                "min": 5,
                "max": 300,
                "default": None,
            },
            {
                "id": "result_budget",
                "label": "누적 결과 문자 예산",
                "type": "number",
                "min": 5000,
                "max": 200000,
                "step": 5000,
                "default": None,
                "description": "여러 도구 결과 글자 수의 합계 상한. 합계가 이를 넘으면 추가로 줄여 컨텍스트 비용 절감 (개별 결과는 별도 임계값으로 통제).",
            },
            {
                "id": "tool_result_preview_threshold",
                "label": "PD preview 전환 임계 (자)",
                "type": "number",
                "min": 1000,
                "max": 200000,
                "step": 1000,
                "default": None,
                "description": "개별 도구 결과가 이 크기를 넘으면 미리보기로 자동 압축됩니다. 원본은 별도 저장소에 보존되어 LLM 이 필요할 때 다시 조회 가능.",
            },
            {
                "id": "tool_result_preview_size",
                "label": "PD preview 크기 (자)",
                "type": "number",
                "min": 256,
                "max": 16384,
                "step": 256,
                "default": None,
                "description": "압축할 때 남길 글자 수. LLM 은 별도 조회 도구로 원본을 다시 가져올 수 있습니다.",
            },
        ],
        "behavior": [
            "도구를 순서대로 실행 — 한 도구가 실패해도 다음 도구는 계속 진행",
            "큰 도구 결과는 미리보기로 줄이고 원본은 별도 보관 (필요시 다시 불러옴)",
            "총 결과 양이 많으면 추가로 더 줄여서 컨텍스트 비용을 절약",
            "MCP 도구는 MCP 서버에 HTTP 로 호출",
            "에이전트가 도구 사용법을 미리 보기 가능 (파라미터 / 설명 조회)",
        ],
    },
    # s05_policy: dict 박제 대신 PolicyGateStage.describe_config() self-describing.
    # get_stage_config() 가 Stage 의 describe_config() 를 먼저 조회 — 새 Stage 는
    # 중앙 dict 수정 없이 자체 선언만으로 UI 에 합류.

    # v1.0 — s08_judge stage 격하 흡수: judge_then_loop strategy 로 통합. fields 합쳐짐.
    "s08_decide": {
        "description_ko": "에이전트가 답변을 마무리할지 더 시도할지 결정합니다. 옵션으로 답변 품질을 별도 LLM 으로 평가할 수도 있습니다.",
        "when_to_use": "항상 활성 (필수). 품질 평가가 필요하면 평가 모드 ON.",
        "when_to_skip": "비활성 불가.",
        "cost_hint": "low",
        "description_en": "Decides loop continuation. Optionally runs LLM evaluation when judge_then_loop strategy active.",
        "icon": "🔀",
        # max_iterations 는 top-level (HarnessConfig) 만 사용 — 단일 진실 소스.
        # max_retries 는 stage_param (decide 가 직접 read).
        "fields": [
            {
                "id": "max_retries",
                "label": "최대 재시도 횟수",
                "type": "number",
                "min": 0,
                "max": 10,
                "default": None,
                "description": "응답 품질 평가 점수가 기준 미달일 때 LLM 답변을 다시 시도하는 최대 횟수. 비용/반복/콘텐츠 정책 같은 다른 종료 조건은 정책 단계의 가드로 설정합니다.",
            },
            # v1.0 — s08_judge 격하 흡수 (judge_then_loop strategy 활성 시만 의미)
            {
                "id": "judge_enabled",
                "label": "응답 품질 평가 (judge) 활성",
                "type": "toggle",
                "default": None,
                "description": "켜면 매 응답 후 별도 LLM 으로 품질 점수를 매기고, 기준 미달이면 재시도합니다. 추가 LLM 비용이 발생합니다.",
            },
            {
                "id": "judge_threshold",
                "label": "judge 통과 기준 점수",
                "type": "slider",
                "min": 0,
                "max": 1,
                "step": 0.05,
                "default": None,
                "description": "품질 평가 점수가 이 값 미만이면 답변을 다시 시도합니다 (0~1 범위, 1 에 가까울수록 엄격).",
            },
            {
                "id": "criteria",
                "label": "judge 평가 기준",
                "type": "multi_select",
                "options": ["relevance", "completeness", "accuracy", "clarity"],
                "default": ["relevance", "completeness", "accuracy", "clarity"],
                "description": "품질 평가에 사용할 항목 (관련성 / 완성도 / 정확성 / 명확성). 외부 플러그인으로 항목 추가 가능.",
            },
            {
                "id": "evaluation_strategy",
                "label": "평가 구현체",
                "type": "select",
                "options": ["llm_judge", "rule_based", "none"],
                "default": None,
                "description": "평가 방식: llm_judge (LLM 으로 점수 매김) / rule_based (규칙 기반) / none (평가 안 함).",
            },
            {
                "id": "evaluation_prompt_template",
                "label": "평가 프롬프트 템플릿 이름",
                "type": "text",
                "default": None,
                "description": "평가 프롬프트 템플릿 이름. 비워두면 기본 템플릿 사용. 외부 플러그인으로 커스텀 템플릿 등록 가능.",
            },
            {
                "id": "evaluation_system_prompt",
                "label": "평가 LLM system prompt (선택)",
                "type": "textarea",
                "default": None,
                "description": "평가 LLM 의 시스템 프롬프트. 비우면 기본값 사용. 평가 톤이나 출력 형식을 도메인에 맞게 조정할 때 사용합니다.",
            },
        ],
        "behavior": [
            "비용 한도 초과 시 종료",
            "반복 한도 도달 시 종료",
            "도구 실행 결과를 기다려야 하면 다음 응답으로 계속",
            "품질 평가 점수가 기준 미달이면 재시도",
            "정책 위반 (예산 / 콘텐츠 등) 시 종료",
            "도구 호출 없는 텍스트 응답이면 답변 완료로 종료",
            "옵션: 별도 LLM 으로 답변 품질 점수 매김 (평가 모드 ON 시)",
        ],
    },
    # v1.0 — s10_save stage 격하 흡수: persist strategy 로 통합. fields 합쳐짐.
    "s09_finalize": {
        "description_ko": "최종 답변을 확정하고, 토큰 사용량 / 비용 / 소요 시간 통계를 발행합니다. 옵션으로 데이터베이스에 영구 기록.",
        "when_to_use": "항상 활성 (필수). 영구 저장이 필요하면 저장 모드 ON.",
        "when_to_skip": "비활성 불가.",
        "cost_hint": "low",
        "description_en": "Finalizes output, emits metrics, optionally persists to DB.",
        "icon": "🏁",
        "fields": [
            {
                "id": "output_format",
                "label": "출력 포맷",
                "type": "select",
                "options": ["text", "markdown", "json"],
                "default": None,
                "description": "register_output_formatter() 또는 entry_points(xgen_harness.output_formatters) 로 외부 등록 추가 가능.",
            },
            # v1.0 — s10_save 격하 흡수
            {
                "id": "save_enabled",
                "label": "DB 저장 활성화",
                "type": "toggle",
                "default": None,
                "description": "켜면 실행 결과를 데이터베이스에 영구 저장합니다 (질문 / 답변 / 도구 호출 / 메트릭).",
            },
            {
                "id": "table_name",
                "label": "저장 테이블명",
                "type": "text",
                "default": None,
                "description": "저장할 데이터베이스 테이블명. 비워두면 기본 테이블 사용.",
            },
            {
                "id": "input_text_cap",
                "label": "input_text 길이 cap",
                "type": "number",
                "min": 100,
                "max": 100_000,
                "default": None,
                "description": "데이터베이스에 저장할 사용자 질문 텍스트 최대 길이 (글자 수). 초과 시 잘림.",
            },
            {
                "id": "output_text_cap",
                "label": "output_text 길이 cap",
                "type": "number",
                "min": 1_000,
                "max": 1_000_000,
                "default": None,
                "description": "데이터베이스에 저장할 응답 텍스트 최대 길이 (글자 수). 초과 시 잘림.",
            },
        ],
        "behavior": [
            "최종 답변 텍스트를 확정 (커스텀 포맷터 등록 시 적용)",
            "토큰 사용량 / 비용 / 소요 시간 통계 발행",
            "스트리밍 종료 알림 발송",
            "옵션: 데이터베이스에 실행 로그 영구 저장",
            "데이터베이스 미연결 시 자동 skip (저장 없이 응답만 반환)",
        ],
    },
}


def _inject_dynamic_options(cfg: dict) -> dict:
    """fields 내 provider/model select 옵션을 레지스트리에서 동적으로 주입.

    STAGE_CONFIGS 에 하드코딩된 options 리스트를 providers 레지스트리의 실제
    등록된 값으로 교체. 새 provider/model 등록 시 UI 가 자동 반영됨.
    """
    if not cfg or not isinstance(cfg, dict):
        return cfg
    try:
        from ..providers import list_providers, get_default_model
    except Exception as e:
        _sc_logger.debug("providers registry import 실패, stage_config provider 옵션 정적 유지: %s", e)
        return cfg

    fields = cfg.get("fields")
    if not isinstance(fields, list):
        return cfg

    providers = list_providers()
    # provider 기반 모델 목록 — PROVIDER_MODELS 레지스트리에서 조회 (기본 + 추가 모델).
    # 기존 get_default_model 은 provider 당 1개만 반환 → get_provider_models 로 교체.
    try:
        from ..providers import get_provider_models
    except ImportError:
        get_provider_models = None

    default_models: list[str] = []
    seen = set()
    for p in providers:
        if get_provider_models is not None:
            candidates = get_provider_models(p)
        else:
            m = get_default_model(p)
            candidates = [m] if m else []
        for m in candidates:
            if m and m not in seen:
                default_models.append(m)
                seen.add(m)

    updated_fields = []
    for f in fields:
        if not isinstance(f, dict):
            updated_fields.append(f)
            continue
        fid = f.get("id")
        if fid == "provider" and providers:
            f = {**f, "options": providers}
        elif fid == "model" and default_models:
            # 기존 options 와 합집합 (하드코딩 보존 + 신규 추가)
            existing = f.get("options", [])
            merged = list(dict.fromkeys([*default_models, *existing]))
            f = {**f, "options": merged}
        updated_fields.append(f)

    return {**cfg, "fields": updated_fields}


def _inject_stage_meta(stage_id: str, cfg: dict) -> dict:
    """스테이지별 추가 메타 주입 — UI 배지/라이브 카운터용.

    현재: s04_tool 에 progressive_threshold.
    외부 전략 교체 시에도 일관된 키로 노출되도록 단일 지점 관리.
    """
    if not cfg or not isinstance(cfg, dict):
        return cfg
    if stage_id == "s04_tool":
        try:
            from ..stages.strategies.discovery import get_progressive_threshold
            cfg = {**cfg, "progressive_threshold": get_progressive_threshold()}
        except Exception as e:
            _sc_logger.debug("progressive_threshold 조회 실패, 기본값 유지: %s", e)
    return cfg


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v1.7.1 — Frontend visibility 단일 진실 소스
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 프론트가 stage-detail-panel.tsx 에 SCHEMA_FIELD_HIDE_BY_STAGE / STRATEGY_UI_VISIBLE_STAGES
# 를 하드코딩하던 것을 엔진으로 이동. 외부 Stage 는 두 경로 중 하나로 동일 효과:
#   1) 자기 stage_config dict 의 field 에 직접  "hidden": True 박음
#   2) 자기 stage_config dict 의 stage 메타에  "expose_strategy_picker": True 박음
# 빌트인 stage 의 hide list 는 아래 두 dict 가 단일 진실 소스. 프론트는 f.hidden /
# cfg.expose_strategy_picker 만 read 하면 됨 (외부 stage 자동 합류).
# ─── hidden field — 사용자에게 안 보이게 가릴 stage_param. LLM 자율 결정 또는 R3 PD 정신 위임.
_HIDDEN_FIELDS_BY_STAGE: dict[str, set[str]] = {
    "s00_harness": {"strategy"},                                                # Transport — auto
    "s03_prompt":  {"include_rules", "citation_mode",
                    "citation_auto_doc_tokens", "citation_auto_prod_tokens",
                    "thinking_mode", "planning_instruction_template",
                    "identity_template", "rules_template"},                     # CoT/ReAct·인용 — auto
    "s04_tool":    {"builtin_tools", "force_tool_use",
                    "capability_discovery", "capability_top_k",
                    "capability_min_score"},                                    # v1.12.2 — rag_tool_mode 키 폐기
    "s06_context": {"strategy", "context_window", "compaction_threshold",
                    "score_threshold", "rerank_top_k", "reranker",
                    "metadata_filter",
                    "rag_pd_mode", "chars_per_token",  # v1.12.2 — enhance_prompt / rag_ingestion_mode 폐기
                    "rag_pd_snippet_size",
                    # v1.12.2 — cascade L3/L4/L5 임계는 advanced UI 노출. operator precedence
                    # fix 와 동반 — 사용자가 토큰 압축 시점을 직접 조절 가능 (runtime_defaults
                    # 70/85/95 floor 가 default).
                    "context_collapse_threshold", "context_collapse_keep_tail",
                    "microcompact_threshold", "microcompact_keep_recent",
                    "autocompact_threshold", "autocompact_keep_tail",
                    "window_size", "intent_rules"},                             # R3 — 검색 파라미터는 RAG 도구가 owns
}
# ─── Strategy 카드 노출 stage 화이트리스트. default False (모든 stage 의 strategy UI 는 함정).
# 사용자가 명시적으로 결정해야 의미 있는 두 stage 만 노출.
_EXPOSE_STRATEGY_PICKER: set[str] = {
    "s08_decide",   # 평가 모드 사용자 결정
    # s05_policy 는 Strategy Variants 대신 Guard 조합으로 구성 — list_strategies() 가
    # 빈 list 라 picker UI 가 빈 dropdown 으로 떨어진다. 외부 stage 가 자기 stage_config
    # 에 `expose_strategy_picker=True` 박으면 여전히 합류.
}


def _inject_visibility_meta(stage_id: str, cfg: dict) -> dict:
    """Frontend visibility 메타 통합 (v1.7.1).

    - fields[i].hidden — 빌트인 hide list 박음. 자체 stage_config 가 이미 박은
      hidden=True 는 보존 (외부 Stage self-describing 우선).
    - cfg.expose_strategy_picker — 빌트인 화이트리스트로 박음. 자체 stage_config
      가 이미 박은 값은 보존.
    """
    if not cfg or not isinstance(cfg, dict):
        return cfg
    hides = _HIDDEN_FIELDS_BY_STAGE.get(stage_id) or set()
    if hides:
        new_fields = []
        changed = False
        for f in cfg.get("fields") or []:
            if isinstance(f, dict) and f.get("id") in hides and not f.get("hidden"):
                new_fields.append({**f, "hidden": True})
                changed = True
            else:
                new_fields.append(f)
        if changed:
            cfg = {**cfg, "fields": new_fields}
    if "expose_strategy_picker" not in cfg:
        cfg = {**cfg, "expose_strategy_picker": stage_id in _EXPOSE_STRATEGY_PICKER}
    return cfg


def _resolve_stage_self_describe(stage_id: str) -> Optional[dict]:
    """Stage 클래스의 self-describing 설정 조회.

    조회 순서:
      1. **Stage 가 `describe_config()` 를 override 했으면 그 결과 사용** —
         명시적 i18n(description_ko/en) 분리, machine meta 명시 등 세밀 제어가
         필요할 때 (예: s05_policy). auto-compose 보다 우선.
      2. class attribute (when_to_use/when_to_skip/cost_hint) + param_schema()
         자동 조립 — docstring 첫 단락을 description_ko/en 양쪽에 박는 폴백.
      3. None — 중앙 STAGE_CONFIGS dict 폴백.

    1 번이 2 번보다 우선이어야 하는 이유: auto-compose 는 docstring 을 영문/한글
    구분 없이 그대로 박는다 → override 가 있어도 가려져서 영문 locale 에서 한글이
    노출되는 회귀가 있었다 (s05_policy v0.17.0).
    """
    try:
        from .registry import _get_default_registry
        reg = _get_default_registry()
        cls = reg.get(stage_id, "default")
    except Exception:
        return None

    # 1. 명시적 describe_config() override 우선 — Stage 베이스의 기본 구현은 None 반환.
    try:
        explicit = cls.describe_config() if hasattr(cls, "describe_config") else None
    except Exception:
        explicit = None
    if isinstance(explicit, dict):
        return explicit

    # 2. STAGE_CONFIGS dict 에 항목이 있으면 그쪽 우선 (description_ko/en 명시 분리됨).
    #    class attr (when_to_use 등) 가 있어도 dict 의 설명문이 영문 locale 에서 정확.
    #    auto-compose 는 dict 항목이 없는 외부 Stage 용 폴백.
    if stage_id in STAGE_CONFIGS:
        return None  # 호출자가 STAGE_CONFIGS 폴백을 쓴다 (or 패턴).

    # 3. class attribute 자동 조립 (docstring 폴백 — 외부 Stage 용)
    return _compose_from_class_attrs(cls)


def _compose_from_class_attrs(cls) -> Optional[dict]:
    """v0.17.0 — Stage 의 class attribute + param_schema() 를 UI 용 dict 로 조립.

    Stage 가 `when_to_use` / `when_to_skip` / `cost_hint` 중 하나라도 명시적으로
    override 했거나 `param_schema` 클래스메서드를 override 했으면 새 모델로 판단.
    한국어 리터럴(description_ko/behavior/icon) 은 엔진에 없음 — UI 가 필요하면
    docstring 첫 줄을 보조 설명으로 사용.
    """
    # 새 모델 판별 — 기본값 외에 선언이 있는지
    has_machine_meta = (
        bool(getattr(cls, "when_to_use", "")) or
        bool(getattr(cls, "when_to_skip", "")) or
        (hasattr(cls, "param_schema") and callable(getattr(cls, "param_schema")) and
         cls.param_schema.__qualname__.split(".")[0] != "Stage")  # Stage 기본 구현 아님
    )
    if not has_machine_meta:
        return None

    # param_schema() → UI fields
    fields: list[dict] = []
    try:
        schema = cls.param_schema() if hasattr(cls, "param_schema") else []
        for f in schema or []:
            if hasattr(f, "to_dict"):
                fields.append(f.to_dict())
            elif isinstance(f, dict):
                fields.append(f)
    except Exception as e:
        _sc_logger.debug("[stage_config] %s.param_schema() 호출 실패: %s", cls.__name__, e)

    # docstring 첫 문단 — 인간 UI 보조 설명 (선택). 없어도 Stage 작동.
    doc = (cls.__doc__ or "").strip()
    if doc:
        first_para = doc.split("\n\n", 1)[0].strip()
    else:
        first_para = ""

    return {
        # 인간 UI 보조 — Stage 가 남기는 docstring 기반. 없으면 빈 문자열.
        "description_ko": first_para,
        "description_en": first_para,  # i18n 없음. 필요 시 추후 gettext.
        # 여기부터는 LLM 도 읽는 machine meta
        "when_to_use": getattr(cls, "when_to_use", "") or "",
        "when_to_skip": getattr(cls, "when_to_skip", "") or "",
        "cost_hint": getattr(cls, "cost_hint", "medium") or "medium",
        "fields": fields,
        "behavior": [],  # 엔진에 저장 안 함 — UI 필요하면 docstring 참조
    }


def get_stage_config(stage_id: str) -> dict:
    """스테이지 설정 스키마 반환 — provider/model 옵션은 레지스트리에서 동적 주입.

    구 stage_id 가 들어와도 canonical 로 해석 (v0.11.0 alias 하위호환).
    v0.17.0 — Stage.describe_config() 가 dict 를 반환하면 우선 사용 (self-describing).
    """
    sid = canonical_stage_id(stage_id)
    cfg = _resolve_stage_self_describe(sid) or STAGE_CONFIGS.get(sid, {})
    cfg = _inject_dynamic_options(cfg)
    cfg = _inject_stage_meta(sid, cfg)
    cfg = _inject_visibility_meta(sid, cfg)
    return cfg


def get_all_stage_configs() -> dict:
    """전체 스테이지 설정 스키마 — 각 스테이지에 dynamic options + meta 자동 주입.

    v0.17.0 — STAGE_CONFIGS dict 에 없어도 registry 에 등록된 Stage 가
    `describe_config()` 를 가지고 있으면 결과에 포함.
    """
    out: dict = {}
    seen: set[str] = set()

    # 1. 중앙 dict
    for sid, base in STAGE_CONFIGS.items():
        self_described = _resolve_stage_self_describe(sid)
        cfg = self_described if self_described is not None else base
        cfg = _inject_dynamic_options(cfg)
        cfg = _inject_stage_meta(sid, cfg)
        cfg = _inject_visibility_meta(sid, cfg)
        out[sid] = cfg
        seen.add(sid)

    # 2. registry 에만 있고 self-describe 하는 Stage 들 병합 — dict 박제 없이 UI 합류.
    try:
        from .registry import _get_default_registry
        reg = _get_default_registry()
        for sid in reg.list_stages():
            if sid in seen:
                continue
            self_described = _resolve_stage_self_describe(sid)
            if self_described is None:
                continue
            cfg = _inject_dynamic_options(self_described)
            cfg = _inject_stage_meta(sid, cfg)
            cfg = _inject_visibility_meta(sid, cfg)
            out[sid] = cfg
    except Exception as e:
        _sc_logger.debug("[stage_config] registry self-describe 병합 skip: %s", e)

    return out
