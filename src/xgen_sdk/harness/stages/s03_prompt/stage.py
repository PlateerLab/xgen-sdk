"""
S03 System Prompt — 시스템 프롬프트 조립

섹션 우선순위 기반 조립:
1. Identity (역할/페르소나)
2. Rules (행동 규칙)
3. Planning (사고 모드 지시 — CoT/ReAct, v1.0 흡수)
4. Tool Index (도구 메타데이터 — progressive disclosure Level 1)
5. RAG Context (검색된 문서) — 읽기만, 실행은 s06_context 담당
6. History Summary (이전 대화 요약)
7. Custom Sections (사용자 정의)

v1.0: 구 s05_strategy 의 CoT/ReAct planning_instruction + Strategy 카드 매핑 흡수.
      Capability discovery 는 s04_tool, Intent Routing 은 s06_context 로 분배.
v0.9.0: RAG 검색은 s06_context 가 단독 담당 — 이 Stage 는 state.rag_context 를 읽기만.
"""

import logging

from ...core.stage import Stage, StrategyInfo
from ...core.state import PipelineState

logger = logging.getLogger("harness.stage.system_prompt")

# 섹션 우선순위 (낮을수록 높은 우선순위 → 컨텍스트 압축 시 뒤에서부터 제거)
SECTION_PRIORITIES = {
    "identity": 1,
    "rules": 2,
    "planning": 2.5,  # v1.0: CoT/ReAct 지시 — rules 다음, tools 전 (구 s05_strategy 흡수)
    "harness_stages": 2.7,  # v1.11.5: stage 토폴로지 — rules 다음, tools 전
    "meta_tools_by_stage": 2.8,  # v1.11.5: stage 별 도구 매핑
    "tools": 3,
    "rag": 4,
    "history": 5,
    "custom": 6,
    "footer": 7,
}


# ─── Stage Topology (v1.11.5) — 단일 진리원본 ─────────────────────
# LLM 에 노출할 stage 한 줄 설명. 사용자가 캔버스에서 박은 환경값 / 도구 카탈로그 와
# 별개로, **현재 harness 가 어떤 stage 로 구성됐는지** 환경 fact 로 노출.
# PD 정신: LLM 이 자기가 어느 환경 슬롯에 있는지 인지 → 자율 결정.
# 행동 강제 X, fact 만.
STAGE_TOPOLOGY: list[dict[str, str]] = [
    {"id": "s00_harness", "label": "Harness", "desc": "하네스 진입/종료. Planner orchestrator role."},
    {"id": "s01_input", "label": "Input", "desc": "사용자 입력 + external_inputs 결합."},
    {"id": "s02_history", "label": "History", "desc": "대화 이력 + memory_collection."},
    {"id": "s03_prompt", "label": "Prompt", "desc": "system_prompt 조립 (이 stage)."},
    {"id": "s04_tool", "label": "Tool", "desc": "도구 카탈로그 indexing + capability binding + PD builtin 합류."},
    {"id": "s05_policy", "label": "Policy", "desc": "policy_pack 적용."},
    {"id": "s06_context", "label": "Context", "desc": "맥락 관리 — RAG/Ontology 자동 search 폐기, 도구로 위임."},
    {"id": "s07_act", "label": "Act", "desc": "도구 디스패치 (sequential / parallel_read / strict_no_error)."},
    {"id": "s08_decide", "label": "Decide", "desc": "loop 결정 (judge)."},
    {"id": "s09_judge", "label": "Judge", "desc": "응답 평가 / loop 종료 판단."},
    {"id": "s10_finalize", "label": "Finalize", "desc": "egress + done event."},
]


# 도구 tag / category → stage 매핑. 도구 자체 메타 (tags / category) 기반 자동 그룹화.
# 도구가 여러 stage 에 묶이면 첫 매칭 사용. 매핑 안 되면 "기타".
STAGE_TAG_GROUPS: list[tuple[str, set[str]]] = [
    # (stage_id, 이 stage 에 묶이는 tag/category 집합)
    ("s04_tool", {"builtin", "pd", "system"}),
    ("s06_context", {"rag", "ontology"}),
    ("s07_act", {"mcp", "api", "search", "synthesis", "web", "http", "tools", "skill"}),
    ("s09_judge", {"judge"}),
]

# ─── 공개 레지스트리 (v1.0 — 박제 풀기) ───────────────────────────────
# 모든 프롬프트 텍스트를 등록 기반으로 관리. 외부 작업자/사용자가 자기 도메인
# 템플릿 주입 가능. 우선순위:
#   1. stage_params 직접 override (가장 강함)
#   2. entry_points (xgen_harness.prompt_templates) 로 등록된 외부 템플릿
#   3. register_*(...) 으로 등록된 in-process 템플릿
#   4. 본 파일에 등록된 기본 템플릿 (가장 약함)

# Identity / Rules / Planning 기본 템플릿 — 키 = 템플릿 이름, 값 = 텍스트.
# 외부 등록 시 같은 키 덮어쓰면 override.
DEFAULT_IDENTITIES: dict[str, str] = {
    "default": (
        "You are a helpful AI assistant. "
        "Answer the user's questions accurately and concisely. "
        "If you need more information, use the available tools to find it."
    ),
}

DEFAULT_RULES: dict[str, str] = {
    # v1.8.0 — 사용자 검증 (claude-cli-test/bench/harnesses.py RESTRICTIONS_ONLY) 정합:
    # "Don't" 명령형 12줄 ~120 tok = Qwen 같은 약한 모델에서 검증된 +31% 효과 패턴.
    # 친절한 길고 친절한 가이드 (~500 tok 이상) 보다 짧고 명령형이 더 강함.
    #
    # v1.11.5 (5/17): 조각조각 박힌 RESTRICTIONS_ONLY 톤은 사용자 검증 환경 패턴.
    # 큰 합성 강제 (SYNTHESIS MODE / reasoning trace 예시 박기) 만 PD 위반이고
    # 이런 short directive 는 환경의 일부로 유지. 폐기 시 LLM 이 RAG 컬렉션
    # 무시하는 회귀 적발 (5/17 사용자 로그).
    "default": (
        "<rules>\n"
        "If <active_resources> lists ANY resource → TRY the matching tool BEFORE saying \"no tools available\". Don't claim absence without trying.\n"
        "Don't call the same tool with the same args twice.\n"
        "Don't repeat a search query that returned 0 results — change keywords or stop.\n"
        "Don't call discover_collection on the same collection name twice.\n"
        "Don't keep trying after all attached collections returned empty — STOP and tell the user.\n"
        "Don't speculate when tools return no data — say \"no relevant data found\" and stop.\n"
        "Don't fetch_pd the same id twice — it's idempotent.\n"
        "Don't add filler. Lead with the answer, not the reasoning.\n"
        "Trust tool results — don't second-guess them.\n"
        "Cite source when using reference documents.\n"
        "Use the same language as the user.\n"
        "If a tool fails, try an alternative ONCE — don't keep retrying.\n"
        "If exhausted, report briefly and STOP. Don't loop.\n"
        "</rules>"
    ),
}

# Thinking mode 템플릿 — 키 = 모드 이름, 값 = planning_instruction 텍스트.
# "none" 은 빈 문자열 → planning 섹션 생략.
THINKING_MODE_TEMPLATES: dict[str, str] = {
    "none": "",
    "cot": (
        "<planning_instruction>\n"
        "Before answering, think step by step about what information you need "
        "and which tools to use. Create a brief plan, then execute it.\n"
        "</planning_instruction>"
    ),
    "react": (
        "<planning_instruction>\n"
        "Use the ReAct (Reason + Act) framework:\n"
        "1. Thought: Analyze the current situation and decide the next action.\n"
        "2. Action: Execute a tool or generate a response.\n"
        "3. Observation: Review the result and decide if more steps are needed.\n"
        "Repeat until the task is complete.\n"
        "</planning_instruction>"
    ),
}

# Strategy 카드 → thinking_mode 매핑. 외부에서 새 카드 등록 시 같이 늘리면 됨.
STRATEGY_CARD_TO_MODE: dict[str, str] = {
    "cot_planner": "cot",
    "react": "react",
    "none": "none",
}


def register_identity(name: str, template: str) -> None:
    """Identity 템플릿 등록. 사용자가 stage_params.identity_template = name 으로 선택."""
    DEFAULT_IDENTITIES[name] = template


def register_rules(name: str, template: str) -> None:
    """Rules 템플릿 등록. 사용자가 stage_params.rules_template = name 으로 선택."""
    DEFAULT_RULES[name] = template


def register_thinking_mode(name: str, template: str, *, card_alias: str | None = None) -> None:
    """Thinking mode 등록. card_alias 주면 Strategy 카드 픽도 자동 매핑.

    예) register_thinking_mode("tree_of_thought", "<planning>...</planning>",
                                card_alias="tot")
        → stage_params.thinking_mode = "tree_of_thought" 로 선택,
          또는 active_strategies[s03_prompt] = "tot" 카드 픽으로 자동 매핑.
    """
    THINKING_MODE_TEMPLATES[name] = template
    if card_alias:
        STRATEGY_CARD_TO_MODE[card_alias] = name


_PROMPT_TEMPLATES_DISCOVERED = False


def _discover_prompt_templates_from_entry_points() -> None:
    """entry_points 그룹 ``xgen_harness.prompt_templates`` 자동 발견. idempotent.

    외부 패키지 등록 예:
      [project.entry-points."xgen_harness.prompt_templates"]
      lotte_pack = "lotte_harness.prompts:register_all"

    register_all() 콜러블이 호출되며, 안에서 register_identity/register_rules/
    register_thinking_mode 를 자유롭게 호출.
    """
    global _PROMPT_TEMPLATES_DISCOVERED
    if _PROMPT_TEMPLATES_DISCOVERED:
        return
    _PROMPT_TEMPLATES_DISCOVERED = True
    try:
        from importlib.metadata import entry_points
    except Exception:
        return
    try:
        eps = entry_points()
        group = "xgen_harness.prompt_templates"
        items = eps.select(group=group) if hasattr(eps, "select") else eps.get(group, [])  # type: ignore[arg-type]
        for ep in items:
            try:
                fn = ep.load()
                if callable(fn):
                    fn()
            except Exception as e:
                logger.warning("[prompt_templates] entry_point %s 로드 실패: %s", ep.name, e)
    except Exception as e:
        logger.debug("[prompt_templates] entry_points discovery 실패: %s", e)


_discover_prompt_templates_from_entry_points()


class SystemPromptStage(Stage):
    """시스템 프롬프트 섹션 기반 조립"""

    @property
    def stage_id(self) -> str:
        return "s03_prompt"

    @property
    def order(self) -> int:
        # v1.2.0 — s04_tool 가 ingress 의 도구 카탈로그를 먼저 채우도록 s03 을 뒤로 민다.
        # 기존 (order=3) 에서는 _build_tool_index_section 이 항상 빈 tool_index 를 봐서
        # <available_tools> 섹션이 사실상 미렌더 → progressive disclosure 의 system_prompt
        # 측 가시성이 죽어있던 회귀. 이제 s04 → s03 순으로 ingress 가 흐른다.
        return 4

    async def execute(self, state: PipelineState) -> dict:
        config = state.config
        sections: list[tuple[int, str, str]] = []  # (priority, name, content)

        # stage_params에서 설정 읽기 (3-level fallback)
        system_prompt_override = self.get_param(
            "system_prompt", state, config.system_prompt if config else ""
        )
        include_rules: bool = self.get_param("include_rules", state, True)

        # 프롬프트 스토어에서 선택된 프롬프트 내용 가져오기
        # stage_params.prompt_content에 프론트에서 미리 복사해둔 내용이 있음
        prompt_content = self.get_param("prompt_content", state, None)
        if prompt_content:
            system_prompt_override = prompt_content

        # 1. Identity — 사용자 지정 시스템 프롬프트
        if system_prompt_override:
            sections.append((SECTION_PRIORITIES["identity"], "identity", system_prompt_override))
        else:
            sections.append((SECTION_PRIORITIES["identity"], "identity", self._default_identity(state)))

        # 2. Rules — 기본 행동 규칙 (include_rules=False면 건너뛰기)
        if include_rules:
            sections.append((SECTION_PRIORITIES["rules"], "rules", self._default_rules(state)))

        # 3. Tool Index — Level 1 메타데이터 (progressive disclosure)
        # v1.2.0 — eager (tool_index) + deferred (state.deferred_tools) 두 그룹 표시.
        # eager 는 곧바로 호출 가능 / deferred 는 ToolSearch 로 승격 후 호출.
        deferred_list = getattr(state, "deferred_tools", None) or []
        if state.tool_index or deferred_list:
            tool_section = self._build_tool_index_section(state.tool_index, deferred_list)
            sections.append((SECTION_PRIORITIES["tools"], "tools", tool_section))

        # 4. RAG Context — v0.9.0+: 실행은 s06_context 가 단독 담당.
        # 여기서는 이미 채워진 state.rag_context 를 읽기만 한다.
        # (PHILOSOPHY §2 s03 "비담당" — Documents API 호출 금지)
        if state.rag_context:
            rag_section = f"<reference_documents>\n{state.rag_context}\n</reference_documents>"
            sections.append((SECTION_PRIORITIES["rag"], "rag", rag_section))

        # v1.5.4 — R3 회귀 fix. rag_tool_mode='tool' (default) 일 때 state.rag_context 가
        # 비어 있고 <reference_documents> 섹션이 미렌더되어 LLM 이 "참조 자료 있음" 사실
        # 자체를 모르고 DB 도구로 추론하던 회귀 (사용자 호소: "assort 컬렉션 박았는데
        # rag_search 안 부르고 DB 연결 알려달라고 답"). 박힌 컬렉션 list 를 명시하고
        # 도구 우선 사용 안내를 system_prompt 에 박는다.
        rag_collections_attached = list(
            self.get_param("rag_collections", state, []) or []
        )
        # s04 가 rag_collections 를 stage_param 에 두는 경로 (rag/ontology) — s06 진입 전
        # state.metadata 에 노출됨. 둘 다 fallback.
        if not rag_collections_attached:
            rag_collections_attached = list(
                state.metadata.get("rag_collections") or []
            )
        ontology_collections_attached = list(
            self.get_param("ontology_collections", state, []) or []
        )
        if not ontology_collections_attached:
            ontology_collections_attached = list(
                state.metadata.get("ontology_collections") or []
            )
        # v1.5.5 — db_connections / files 도 reference_resources 에 합류 (RAG/Ontology isomorphic).
        # 사용자가 박은 모든 자원이 LLM 손에 메타로 노출되어 자율 판단 가능.
        db_connections_attached = list(
            self.get_param("db_connections", state, []) or []
        )
        if not db_connections_attached:
            db_connections_attached = list(
                state.metadata.get("db_connections") or []
            )
        files_attached = list(
            self.get_param("files", state, []) or []
        )
        if not files_attached:
            files_attached = list(
                state.metadata.get("files") or []
            )

        if (rag_collections_attached or ontology_collections_attached
                or db_connections_attached or files_attached):
            # v1.6 — Anthropic Skills frontmatter Level 1 패턴 isomorphic.
            # 메타 (지도) 만 노출 — name + description + total_documents 등.
            # 도구 이름 / 호출 가이던스는 박지 않음 — 도구는 system_prompt 의 <available_tools>
            # 섹션과 도구 description 통해 LLM 이 자율 발견. 강제 instruction X.
            #
            # 풍부 메타 (Pack 2) — state.metadata 의 *_meta dict 에서 fetch:
            #   rag_collections_meta:  {name → {description, total_documents}}
            #   ontology_collections_meta: {name → {description, ...}}
            #   db_connections_meta:   {name → {type, schema: {tables: [...]}}}
            #   files_meta:            {name → {size, type, modified_at}}
            #   mcp_sessions_meta:     {name → {tool_count, when_to_use}}
            # 이식측이 state.metadata['*_meta'] 박으면 자동 풍부 노출. 안 박혀있어도 정상 동작.
            rag_meta = state.metadata.get("rag_collections_meta") or {}
            onto_meta = state.metadata.get("ontology_collections_meta") or {}
            db_meta = state.metadata.get("db_connections_meta") or {}
            files_meta = state.metadata.get("files_meta") or {}

            def _meta_dict(maybe_dict, default_name=""):
                if isinstance(maybe_dict, dict):
                    return maybe_dict
                return {"name": str(maybe_dict) or default_name}

            # v1.11.4 — PD 정신 회복: <active_resources> 는 사용자가 박은 환경의
            # 노출일 뿐. "MUST / do not assume / 반드시 즉시 호출" 등 LLM 행동 강제
            # 톤은 폐기. 각 항목 옆에 호출 도구만 같이 박는다 — 활용 여부 / 시점 /
            # 방식은 LLM 자율.
            lines: list[str] = ["<active_resources>"]
            lines.append(
                "These resources are attached to the workflow. "
                "Each item below pairs with → the tool that operates on it."
            )
            if rag_collections_attached:
                lines.append("- 문서 (의미적 유사도 검색 → `rag_search(query, collection_name)`):")
                # v1.7.2 — description 빈 칸인 컬렉션 추적 → 가이드 라인 끝에 추가.
                missing_desc_count = 0
                for col in rag_collections_attached:
                    m = rag_meta.get(col, {}) if isinstance(rag_meta.get(col), dict) else {}
                    desc = (m.get("description") or "").strip()
                    total = m.get("total_documents", 0)
                    make_name = (m.get("make_name") or "").strip()
                    # 친화 이름 우선 (assort_uuid → 'assort'). UUID 만 있는 collection_name 보다
                    # LLM 이 컬렉션 의미 추측하기 쉬움. UUID 는 도구 호출 시 정확 ID 로 옆에 박음.
                    if make_name and make_name != col:
                        line = f"  · {make_name} (id={col})"
                    else:
                        line = f"  · {col}"
                    if desc:
                        line += f": {desc}"
                    else:
                        missing_desc_count += 1
                    if total:
                        line += f" ({total:,} docs)"
                    lines.append(line)
                # v1.11.4 — PD 정신: 환경 상태 (description 빈 칸인 컬렉션 N개) 만
                # 노출. "직접 검색해 확인하세요" 같은 행동 지시는 폐기.
                if missing_desc_count > 0:
                    lines.append(
                        f"  ※ {missing_desc_count} 컬렉션은 description 메타 없음."
                    )
            if ontology_collections_attached:
                lines.append(
                    "- 지식 그래프 (관계·계층 검색 → `query_graph(question, collection)`):"
                )
                for col in ontology_collections_attached:
                    m = onto_meta.get(col, {}) if isinstance(onto_meta.get(col), dict) else {}
                    desc = (m.get("description") or "").strip()
                    nodes = m.get("nodes") or m.get("node_count")
                    line = f"  · {col}"
                    if desc:
                        line += f": {desc}"
                    if nodes:
                        line += f" ({nodes:,} nodes)" if isinstance(nodes, int) else f" ({nodes})"
                    lines.append(line)
            if db_connections_attached:
                lines.append(
                    "- DB 연결 (SQL 쿼리 → `mcp_DatabaseLoader` / `mcp_DatabaseReader` / "
                    "`mcp_postgresql_mcp` 등; deferred 면 `search_tools(query='database')` 로 발견 후 호출):"
                )
                for conn in db_connections_attached:
                    if isinstance(conn, dict):
                        cn = conn.get("name") or conn.get("connection_name") or str(conn)
                    else:
                        cn = str(conn)
                    m = db_meta.get(cn, {}) if isinstance(db_meta.get(cn), dict) else (
                        conn if isinstance(conn, dict) else {}
                    )
                    ct = m.get("type") or m.get("db_type") or ""
                    schema = m.get("schema") or {}
                    tables = schema.get("tables") if isinstance(schema, dict) else None
                    line = f"  · {cn}"
                    if ct:
                        line += f" ({ct})"
                    if tables and isinstance(tables, list):
                        sample = ", ".join(str(t) for t in tables[:5])
                        more = f", +{len(tables)-5}" if len(tables) > 5 else ""
                        line += f" — tables: {sample}{more}"
                    lines.append(line)
            if files_attached:
                lines.append(
                    "- 파일 (자동 컨텍스트 주입 — 별도 도구 호출 X; 표/CSV 는 "
                    "`file_system_table_data_mcp`, 일반 read/write 는 `file_system_filesystem_storage`):"
                )
                for fname in files_attached:
                    if isinstance(fname, dict):
                        fn = fname.get("name") or fname.get("file_name") or str(fname)
                        m = fname
                    else:
                        fn = str(fname)
                        m = files_meta.get(fn, {}) if isinstance(files_meta.get(fn), dict) else {}
                    size = m.get("size") or m.get("file_size")
                    ftype = m.get("type") or m.get("file_type") or ""
                    line = f"  · {fn}"
                    parts = []
                    if size:
                        # bytes → human
                        try:
                            sz = int(size)
                            for unit in ("B", "KB", "MB", "GB"):
                                if sz < 1024:
                                    parts.append(f"{sz:.0f} {unit}" if unit == "B" else f"{sz:.1f} {unit}")
                                    break
                                sz /= 1024
                        except Exception:
                            parts.append(str(size))
                    if ftype:
                        parts.append(str(ftype).upper())
                    if parts:
                        line += " (" + " · ".join(parts) + ")"
                    lines.append(line)
            # MCP sessions 메타 (사용자가 mcp_sessions 박았으면 ToolSource 가 등록 + 메타 전달)
            mcp_meta = state.metadata.get("mcp_sessions_meta") or {}
            if mcp_meta:
                lines.append(
                    "- MCP 세션 (각 세션의 도구는 deferred — `search_tools(query=...)` 로 발견 후 "
                    "`ToolSearch(names=[...])` 으로 승격):"
                )
                for sname, m in mcp_meta.items():
                    if not isinstance(m, dict):
                        m = {}
                    tool_count = m.get("tool_count") or m.get("tools")
                    when = m.get("when_to_use") or m.get("description") or ""
                    line = f"  · {sname}"
                    if tool_count:
                        line += f" ({tool_count} 도구)"
                    if when:
                        line += f": {when[:80]}"
                    lines.append(line)
            lines.append("</active_resources>")
            ref_section = "\n".join(lines)
            # v1.8.0 — T1 인식 prominent: rules 직후 (planning 전) 우선. LLM 이 더 빨리
            # 본 후 즉시 행동 trigger. 옛 위치 (rag - 0.1) → rules + 0.5 로 승격.
            sections.append((SECTION_PRIORITIES["rules"] + 0.5, "active_resources", ref_section))

        # v1.7.5 — `<available_prompt_templates>` 섹션. PD isomorphic 정합.
        # L1 (system_prompt 메타) 에 등록된 prompt template 의 이름 + 첫 줄 (description
        # 대용) 박음 → LLM 이 "이런 게 있구나" 인지 → 필요 시 discover_prompt 자율 호출
        # 로 본문 lazy fetch. 사용자 정신: 사용자가 박은 prompt 가 본문이고, 등록된
        # template 은 추가 참조 풀. 이름만 박혀있어도 LLM 이 점진적으로 찾아갈 수 있다.
        # 빈 reg 는 skip — default 만 있어도 default 라는 이름 자체가 LLM 에게 의미.
        try:
            registries = [
                ("identity", DEFAULT_IDENTITIES),
                ("rules", DEFAULT_RULES),
                ("thinking_mode", THINKING_MODE_TEMPLATES),
            ]
            tpl_lines: list[str] = []
            any_template = False
            for ttype, reg in registries:
                if not reg:
                    continue
                tpl_lines.append(f"- {ttype}:")
                for tname, body in reg.items():
                    # 본문 첫 줄 (~120자) 을 description 대용. 본문 비어있으면 이름만.
                    first_line = ""
                    if isinstance(body, str):
                        s = body.strip()
                        if s:
                            first_line = s.split("\n", 1)[0][:120]
                    if first_line:
                        tpl_lines.append(f"  · {tname}: {first_line}")
                    else:
                        tpl_lines.append(f"  · {tname}")
                    any_template = True
            if any_template:
                tpl_section = (
                    "<available_prompt_templates>\n"
                    "사용자가 박은 prompt 가 본문이고, 아래는 추가 참조 가능한 등록된 template "
                    "목록입니다. 적합한 게 있으면 discover_prompt(template_type=..., name=...) "
                    "로 본문을 lazy fetch 하세요.\n"
                    + "\n".join(tpl_lines)
                    + "\n</available_prompt_templates>"
                )
                # rag 다음 우선순위 — 자원 인덱스 끝에 자연스럽게 합류
                sections.append((SECTION_PRIORITIES["rag"], "available_prompt_templates", tpl_section))
        except Exception as _e:
            # 메타 노출 실패는 본문 prompt 흐름 깨면 안 됨 — graceful skip.
            logger.debug("[s03_prompt] available_prompt_templates 섹션 skip: %s", _e)

        # v1.8.0 — Claude Code Skills 패턴: <loaded_skills> 섹션. LLM 이 Skill('이름')
        # 호출로 lazy load 한 markdown body 들이 매 turn system_prompt 에 박힘
        # (session 고정). 한 번 load 하면 재호출 X — body 가 system_prompt 에 이미 있음.
        try:
            loaded = dict(getattr(state.tool, "loaded_skills", {}) or {})
            if loaded:
                skill_lines = ["<loaded_skills>"]
                skill_lines.append(
                    "다음은 이번 session 에서 Skill('이름') 으로 lazy load 한 메타 도구 "
                    "사용 가이드입니다. 이미 system_prompt 에 박혀있으므로 같은 skill 을 "
                    "재호출하지 마세요."
                )
                for sname, sbody in loaded.items():
                    skill_lines.append(f"\n### {sname}\n")
                    skill_lines.append(sbody)
                skill_lines.append("\n</loaded_skills>")
                skill_section = "\n".join(skill_lines)
                # tools 섹션 다음 우선순위 — 도구 카탈로그 직후 사용 가이드 자연 합류
                sections.append(
                    (SECTION_PRIORITIES["tools"] + 0.1, "loaded_skills", skill_section)
                )
        except Exception as _e:
            logger.debug("[s03_prompt] loaded_skills 섹션 skip: %s", _e)

        # v1.6 — Policy guards LLM 가시화. data-driven (register API + entry_points).
        # 빌트인 4 종 (max_iterations / cost_budget_usd / context_window / s05_guards) +
        # 외부 wheel 이 register_active_policy_renderer() 또는 entry_points 로 추가 가능.
        # 정책 종 hardcoded list X — 자기서술 (사용자 정신: 확장성).
        from ...core.active_policies import render_all as _render_active_policies
        config = state.config
        active_policies_lines = _render_active_policies(config) if config else []
        if active_policies_lines:
            policy_section = (
                "<active_policies>\n"
                + "\n".join(active_policies_lines)
                + "\n</active_policies>"
            )
            # rules 다음 우선순위 — 행동 가이드 끝에 자기 제약 자연스럽게 합류
            sections.append((SECTION_PRIORITIES["rules"] + 0.4, "active_policies", policy_section))

        # 5. Citation — 문서 인용 형식 지시
        # citation_mode 우선, 하위 호환으로 citation_enabled 도 여전히 읽습니다.
        #   - off      : 인용 지시 없음
        #   - enabled  : [DOC_n] 인용 형식 권장 (기존 citation_enabled=True 와 동일)
        #   - strict   : enabled 규칙 + 검색 결과에 없는 정보는 답하지 않는다는 강한 규칙 추가
        #   - auto     : v0.11.17+ RAG context 패턴으로 자동 판정 (문서 인용형 → strict, 아니면 off)
        raw_mode = self.get_param("citation_mode", state, None)
        legacy_enabled = bool(self.get_param("citation_enabled", state, False))
        if raw_mode is None:
            citation_mode = "enabled" if legacy_enabled else "off"
        else:
            citation_mode = str(raw_mode).strip().lower() or "off"
            if citation_mode not in ("off", "enabled", "strict", "auto"):
                citation_mode = "enabled" if legacy_enabled else "off"

        # v0.11.17 — auto 모드: RAG context 에서 문서형 신호 감지
        if citation_mode == "auto":
            auto_detected = self._detect_citation_need(state)
            citation_mode = "strict" if auto_detected else "off"
            logger.info("[s03] citation_mode=auto → %s (detected=%s)",
                        citation_mode, auto_detected)

        if citation_mode in ("enabled", "strict"):
            citation_instructions = self.get_param(
                "citation_instructions_template", state, None
            ) or (
                "<citation_instructions>\n"
                "When referencing information from provided documents, cite your sources "
                "using [DOC_1], [DOC_2] format. Each citation should correspond to the "
                "numbered document tags in the reference materials. Always include citations "
                "when stating facts derived from the provided documents.\n"
                "</citation_instructions>"
            )
            sections.append((SECTION_PRIORITIES["rules"] + 0.5, "citation", citation_instructions))

        if citation_mode == "strict":
            # 폴백 멘트의 응답 언어는 호출자가 지정 가능. 미지정 시 영어로만 규칙 서술.
            strict_guard = self.get_param(
                "grounding_rules_template", state, None
            ) or (
                "<grounding_rules>\n"
                "Only answer using information present in <reference_documents>. "
                "If the answer cannot be derived from the provided documents, "
                "state that the information is not available in the provided materials "
                "and do not fabricate.\n"
                "</grounding_rules>"
            )
            sections.append((SECTION_PRIORITIES["rules"] + 0.6, "grounding", strict_guard))

        # 5.5 Planning (CoT/ReAct, v1.0 흡수 from 구 s05_strategy)
        # 첫 루프에만 주입 (재계획 불필요).
        if state.loop_iteration <= 1:
            thinking_mode = self._resolve_thinking_mode(state)
            planning_instruction = self._build_planning_instruction(thinking_mode, state)
            if planning_instruction:
                sections.append((SECTION_PRIORITIES["planning"], "planning", planning_instruction))

        # 6. History Summary (이전 결과)
        if state.previous_results:
            history = "\n---\n".join(state.previous_results[-3:])  # 최근 3개
            sections.append((
                SECTION_PRIORITIES["history"],
                "history",
                f"<previous_results>\n{history}\n</previous_results>",
            ))

        # 7. (v1.11.5) Harness Stages — 환경 fact. LLM 이 자기 환경 토폴로지 인지.
        harness_stages_section = self._build_harness_stages_section()
        if harness_stages_section:
            sections.append((
                SECTION_PRIORITIES["harness_stages"],
                "harness_stages",
                harness_stages_section,
            ))

        # 8. (v1.11.5) Meta Tools by Stage — 현재 indexed 된 도구 카탈로그를 stage 별
        #    그룹화 (도구 자체 tags / category 자동 매핑). 환경 fact.
        meta_tools_section = self._build_meta_tools_by_stage_section(state)
        if meta_tools_section:
            sections.append((
                SECTION_PRIORITIES["meta_tools_by_stage"],
                "meta_tools_by_stage",
                meta_tools_section,
            ))

        # 조립: 우선순위 순서대로
        sections.sort(key=lambda x: x[0])
        assembled = "\n\n".join(content for _, _, content in sections)
        state.system_prompt = assembled

        result = {
            "prompt_chars": len(assembled),
            "sections": [name for _, name, _ in sections],
            "message_count": len(state.messages),
            "rag_included": bool(state.rag_context),
            # v1.0 — UI 가시성: 사고 모드 / 인용 모드 결과 노출.
            # _build_planning_instruction 에서 auto → cot/react/none resolve 한 결과를
            # state.metadata 에 박아 두면 더 정확하지만, 우선 thinking_mode_resolved 키로 직접 표시.
            "thinking_mode_resolved": (state.metadata.get("thinking_mode_resolved")
                                       or self._resolve_thinking_mode(state)),
            "citation_mode": citation_mode,
        }
        logger.info("[System Prompt] %d chars, sections=%s, thinking=%s",
                    len(assembled), result["sections"], result.get("thinking_mode_resolved"))
        return result

    # 기본 도메인 토큰 — 일반 명사만. 회사/프로젝트 고유명사는 포함하지 않는다.
    # 이식 측이 도메인 특화 토큰을 사용하려면 stage_params 로 override:
    #   citation_auto_doc_tokens: ["규정", "지침", ...]
    #   citation_auto_prod_tokens: ["stock", ...]
    _DEFAULT_DOC_TOKENS: tuple[str, ...] = (
        "doc", "document", "report", "regulation", "policy", "manual",
        "rule", "guide", "spec", "pdf", "hwp", "docx", "pptx",
    )
    _DEFAULT_PROD_TOKENS: tuple[str, ...] = (
        "product", "commerce", "stock", "inventory", "catalog", "sku",
        "price", "item", "sales", "csv", "json", "xlsx",
    )

    def _detect_citation_need(self, state: PipelineState) -> bool:
        """v0.11.17 — 도메인 자동 감지 (auto-router, 실험적).

        s03 는 s06 RAG 주입 전 실행되므로 rag_context 는 보통 빔. 따라서
        **collection 이름 + stage_params.s06_context.rag_collections** 를 먼저 감지.

        휴리스틱 우선순위:
          1. Collection 이름 토큰 (중립 명사만; override 가능)
          2. RAG context 에 파일 확장자 (.pdf vs .csv) — rag_context 주입된 경우만
          3. 내용 신호 (연도·metadata) fallback

        **확장 지점**:
          - `citation_auto_doc_tokens` / `citation_auto_prod_tokens` stage_param
            으로 회사·언어 특화 토큰 주입 (기본값과 OR 결합).

        본 판정은 휴리스틱이라 완전하지 않음. 사용자가 명시 off/strict 주면 override.
        """
        import re as _re

        # 1차 — collection 이름 토큰
        rag_collections: list[str] = self.get_param("rag_collections", state, []) or []
        if not rag_collections:
            rag_collections = (state.metadata or {}).get("rag_collections", []) or []
        col_text = " ".join(str(c).lower() for c in rag_collections)

        extra_doc = self.get_param("citation_auto_doc_tokens", state, []) or []
        extra_prod = self.get_param("citation_auto_prod_tokens", state, []) or []
        doc_tokens = tuple(self._DEFAULT_DOC_TOKENS) + tuple(str(t).lower() for t in extra_doc)
        prod_tokens = tuple(self._DEFAULT_PROD_TOKENS) + tuple(str(t).lower() for t in extra_prod)

        doc_col_match = sum(1 for t in doc_tokens if t in col_text)
        prod_col_match = sum(1 for t in prod_tokens if t in col_text)
        if doc_col_match + prod_col_match > 0:
            decision = doc_col_match >= prod_col_match and doc_col_match >= 1
            logger.info(
                "[s03] auto-detect (collection): doc=%d prod=%d → %s",
                doc_col_match, prod_col_match,
                "strict" if decision else "off",
            )
            return decision

        rag_ctx = state.rag_context or ""
        if not rag_ctx:
            return False
        # 1차 — 파일 확장자 signal (가장 robust)
        doc_ext = (
            rag_ctx.count(".pdf") + rag_ctx.count(".docx")
            + rag_ctx.count(".hwp") + rag_ctx.count(".pptx")
        )
        struct_ext = (
            rag_ctx.count(".csv") + rag_ctx.count(".json")
            + rag_ctx.count(".xlsx") + rag_ctx.count(".parquet")
            + rag_ctx.count(".tsv")
        )
        if doc_ext + struct_ext > 0:
            decision = doc_ext >= struct_ext and doc_ext >= 1
            logger.info(
                "[s03] auto-detect (ext): doc=%d struct=%d → %s",
                doc_ext, struct_ext, "strict" if decision else "off",
            )
            return decision

        # 2차 fallback — 내용 신호
        year_pat = len(_re.findall(r"\d{4}년도?[\s_\-][가-힣]{2,}", rag_ctx))
        meta_signal = (
            rag_ctx.count("Document-Metadata")
            + rag_ctx.count("작성자")
            + rag_ctx.count("제목:")
            + rag_ctx.count("마지막 수정자")
        )
        product_signal = (
            len(_re.findall(r"G\d{4,}", rag_ctx))
            + rag_ctx.count("원")
            + rag_ctx.count("₩")
        )
        doc_score = year_pat * 2 + meta_signal * 3
        prod_score = product_signal
        decision = doc_score >= 1 and doc_score >= prod_score
        logger.info(
            "[s03] auto-detect (content): doc=%d (year=%d meta=%d) prod=%d → %s",
            doc_score, year_pat, meta_signal, prod_score,
            "strict" if decision else "off",
        )
        return decision

    # ---------- Planning / Thinking Mode (v1.0 흡수 from 구 s05_strategy) ----------

    def _resolve_thinking_mode(self, state: PipelineState) -> str:
        """thinking_mode 결정 — Strategy 카드 우선, stage_params.thinking_mode 폴백.

        반환 값: "auto" | "none" | "cot" | "react"
        """
        # 1. Strategy 카드 (active_strategies) 가 picked 됐으면 그 값 매핑 사용
        active = ""
        if hasattr(state, "config") and state.config:
            picked = (state.config.active_strategies or {}).get(self.stage_id)
            if isinstance(picked, str):
                active = picked.strip()
        if active and active in STRATEGY_CARD_TO_MODE:
            return STRATEGY_CARD_TO_MODE[active]

        # 2. stage_params.thinking_mode 폴백 (default=auto)
        return self.get_param("thinking_mode", state, "auto")

    def _build_harness_stages_section(self) -> str:
        """v1.11.5 — STAGE_TOPOLOGY 를 보고 <harness_stages> 영역 문자열 생성.

        환경 fact 만. 행동 강제 X. 모든 stage 가 default 로 활성이므로 disabled_stages
        등 사용자 환경값을 검사할 수도 있지만 (s00 의 책임 영역), 여기서는 단순
        토폴로지 노출. PD 정신: LLM 이 자기가 어느 환경에 있는지 인지하게 한다.
        """
        if not STAGE_TOPOLOGY:
            return ""
        lines = ["<harness_stages>"]
        for st in STAGE_TOPOLOGY:
            lines.append(f"- {st['id']} ({st['label']}): {st['desc']}")
        lines.append("</harness_stages>")
        return "\n".join(lines)

    def _build_meta_tools_by_stage_section(self, state: PipelineState) -> str:
        """v1.11.5 — 현재 indexed 된 도구를 stage 별 그룹화.

        도구의 tags / category 자체를 보고 STAGE_TAG_GROUPS 매핑으로 자동 그룹.
        매핑 안 되면 "기타" 그룹. 도구 자체 메타가 단일 진리원본 — 새 노드/도구
        추가 시 그 노드/도구의 tags 만 박으면 자동 분류.
        """
        defs = state.tool_definitions or []
        if not defs:
            return ""
        groups: dict[str, list[str]] = {}  # stage_id → tool name list
        unmapped: list[str] = []
        for td in defs:
            name = td.get("name") if isinstance(td, dict) else None
            if not name:
                continue
            tags_raw = td.get("tags") if isinstance(td, dict) else None
            cat_raw = td.get("category") if isinstance(td, dict) else None
            haystack: set[str] = set()
            if isinstance(tags_raw, list):
                haystack.update(str(t).lower() for t in tags_raw if t)
            if isinstance(cat_raw, str) and cat_raw:
                haystack.add(cat_raw.lower())
            matched = None
            for stage_id, tag_set in STAGE_TAG_GROUPS:
                if haystack & tag_set:
                    matched = stage_id
                    break
            if matched is None:
                unmapped.append(name)
            else:
                groups.setdefault(matched, []).append(name)
        if not groups and not unmapped:
            return ""
        lines = ["<meta_tools_by_stage>"]
        # STAGE_TAG_GROUPS 의 순서대로 출력 (작성된 순서가 표시 순서)
        for stage_id, _ in STAGE_TAG_GROUPS:
            tools = groups.get(stage_id)
            if not tools:
                continue
            lines.append(f"- {stage_id}: {', '.join(tools)}")
        if unmapped:
            lines.append(f"- 기타: {', '.join(unmapped)}")
        lines.append("</meta_tools_by_stage>")
        return "\n".join(lines)

    def _build_planning_instruction(self, mode: str, state: PipelineState) -> str:
        """thinking_mode 에 따라 planning_instruction 문자열 생성.

        해석 순서 (위에서부터, 박제 0):
          1. stage_params.planning_instruction_template — 사용자가 raw 텍스트 직접 주입
          2. THINKING_MODE_TEMPLATES[mode] — registry/entry_points 로 등록된 템플릿
          3. mode == "auto" 면 input_complexity → mode 자동 결정 후 다시 lookup
          4. 없으면 빈 문자열 (planning 섹션 생략)
        """
        # 1. stage_params raw override (가장 강함)
        raw_override = self.get_param("planning_instruction_template", state, None)
        if isinstance(raw_override, str) and raw_override.strip():
            return raw_override

        # 2. auto 는 complexity 로 mode 결정
        if mode == "auto":
            complexity = state.metadata.get("input_complexity", "moderate")
            if complexity == "simple":
                mode = "none"
            elif complexity == "complex":
                mode = "react"
            else:
                mode = "cot"
            logger.info("[Prompt] thinking_mode auto → '%s' (complexity=%s)", mode, complexity)

        # 3. registry lookup (사용자/외부 등록 + 기본)
        return THINKING_MODE_TEMPLATES.get(mode, "")

    def _default_identity(self, state: PipelineState | None = None) -> str:
        """Identity 텍스트 — registry + stage_params override 지원 (박제 0)."""
        if state is not None:
            raw = self.get_param("identity_template_raw", state, None)
            if isinstance(raw, str) and raw.strip():
                return raw
            name = self.get_param("identity_template", state, "default")
        else:
            name = "default"
        return DEFAULT_IDENTITIES.get(name, DEFAULT_IDENTITIES["default"])

    def _default_rules(self, state: PipelineState | None = None) -> str:
        """Rules 텍스트 — registry + stage_params override 지원 (박제 0)."""
        if state is not None:
            raw = self.get_param("rules_template_raw", state, None)
            if isinstance(raw, str) and raw.strip():
                return raw
            name = self.get_param("rules_template", state, "default")
        else:
            name = "default"
        return DEFAULT_RULES.get(name, DEFAULT_RULES["default"])

    def _build_tool_index_section(
        self,
        tool_index: list[dict],
        deferred_list: list[dict] | None = None,
    ) -> str:
        """Progressive Disclosure Level 1 — data-driven 그룹화 (v1.6).

        도구의 ``category`` 필드 (자기서술) 로 자동 그룹. hardcoded 카테고리 list X.
        eager / deferred 둘 다 같은 그룹화 규칙 — 미래 영역 (policy / prompt / evaluation
        등) 추가 시 도구가 자기 category 박기만 + entry_points → 자동 합류. 본체 수정 0.

        - 1단계 (사용자 PD 기조 — 찾는 도구): [system] 그룹 (search_tools / fetch_pd 등)
        - 2단계 (사용자 박은 자원 도구): [retrieval] / [mcp_station] / [custom_api] / [xgen-nodes] / [policy] / [prompt] 등
        - deferred: 별도 [deferred] 그룹 (ToolSearch 로 schema 승격)
        """
        from collections import defaultdict
        groups: dict[str, list[dict]] = defaultdict(list)
        for tool in tool_index or []:
            cat = (tool.get("category") or "general").strip() or "general"
            groups[cat].append(tool)
        if deferred_list:
            for tool in deferred_list:
                groups["deferred"].append(tool)

        lines = ["<available_tools>"]
        # system 그룹 우선 (찾는 도구), 그 다음 알파벳, deferred 마지막
        priority = {"system": 0}
        sorted_cats = sorted(groups.keys(), key=lambda c: (priority.get(c, 1), 1 if c == "deferred" else 0, c))
        # deferred 항상 마지막
        if "deferred" in sorted_cats:
            sorted_cats = [c for c in sorted_cats if c != "deferred"] + ["deferred"]

        for cat in sorted_cats:
            if cat == "deferred":
                # v1.8.0 — strict PD: deferred 도구 list 자체를 system_prompt 에서 제거.
                # 이름/desc 박으면 system_prompt 두꺼워지고 (~3000자) 사용자 의도 (지도 간편화)
                # 위반. LLM 은 search_tools(query=...) 또는 discover_tools() 무조건 호출해야
                # deferred 도구 발견 가능 — 진짜 strict PD. 첫 응답 1 turn 늘어남은 trade-off.
                count = len(groups[cat])
                lines.append(
                    f"\n[deferred] ({count} tools — list hidden. Use search_tools(query=...) "
                    f"or discover_tools() to find, then ToolSearch(names=[...]) to load.)"
                )
                continue
            lines.append(f"\n[{cat}]")
            for tool in groups[cat]:
                name = tool.get("name", "unknown")
                desc = tool.get("description", "")
                line = f"- {name}: {desc}" if desc else f"- {name}"
                lines.append(line)
        lines.append("\n</available_tools>")

        return "\n".join(lines)

    def list_strategies(self) -> list[StrategyInfo]:
        # v1.4.0 — 사용자 픽 카드 hide. 사고 모드는 LLM 자율.
        # 코드 (section_priority/cot_planner/react/none) 보존 — active_strategies 직접 셋 가능.
        return []
