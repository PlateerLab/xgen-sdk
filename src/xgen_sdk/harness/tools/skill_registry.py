"""Skill body registry — Claude Code Skills 패턴 (frontmatter + body lazy + session 고정).

v1.8.0 기조:
  - description (frontmatter 격) 은 1~2 문장만 (WHEN + "Skill('이름') 으로 자세히")
  - 진짜 사용 가이드 (HOW / examples / 흔한 실수) 는 markdown body 로 분리
  - LLM 이 `Skill(name="...")` 호출 시 lazy load → state.loaded_skills 에 박힘
  - s03_prompt 의 <loaded_skills> 섹션이 매 turn 본문 자동 박음 (session 고정)

Claude Code Skills (SKILL.md) 의 frontmatter+body 분리 정합. body 가 길어지면 토큰
비용 ↑ 라 호출 시점까지 lazy. 한 번 호출 = session 끝까지 보존.

외부 등록:
  register_skill_body(name, body)  — entry_points 또는 코드에서
"""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger("harness.tools.skills")


_BUILTIN_SKILL_BODIES: dict[str, str] = {
    # ─────────────────────────────────────────────────────────────────────
    # 메타 도구 9 종 SKILL body — markdown.
    # ─────────────────────────────────────────────────────────────────────

    "discover_tools": """\
# discover_tools — Tool catalog inspector

## When
- Need full JSON Schema for a tool you plan to call
- Want to see every eager-loaded tool name+description

## How
- `discover_tools()` — list all eager tools
- `discover_tools(tool_name="X")` — full schema for X

## Returns
- list mode: `- name: description` lines
- name mode: full JSON Schema

## Next
- If the tool isn't in the eager list → `search_tools(query=...)` to find deferred ones,
  then `ToolSearch(names=[...])` to make callable.
""",

    "search_tools": """\
# search_tools — Find tools by keyword (eager + deferred)

## When
- Eager tool list doesn't show what you need
- Most user-installed tools (MCP, API, agents) are deferred by default in v1.8

## How
- `search_tools(query="email", limit=8)` — keyword search
- `search_tools(query="db", category="retrieval")` — category filter

## Returns
- Top matches: `name (score=N): 120-char description`
- Include deferred tools (system_prompt only shows their names)

## Next
- Pick the best match → `ToolSearch(names=["picked_tool"])` → callable next turn
- No matches? Try a different keyword (English root works best)
- Still nothing? Tell the user the requested capability isn't available

## Common mistake
- Calling the tool you found directly without ToolSearch — deferred tools cannot be
  invoked until promoted.
""",

    "ToolSearch": """\
# ToolSearch — Promote deferred tools to callable

## When
- After `search_tools` or `discover_tools` surfaces a tool you want to actually call

## How
- `ToolSearch(names=["mcp_xxx", "api_loader_yyy"])` — exact names from search

## Returns
- "Loaded N tool(s): ..." + already loaded + not found

## Next
- Tools become callable in the **next turn** (not the current one in most providers)
- Emit ToolSearch this turn, then call the tool next turn
""",

    "fetch_synthesize": """\
# fetch_synthesize — Isolated sub-LLM body synthesis (v1.8.0)

## When
- You need a chunk's body content but don't want context bloat (chunks ×N pile-up)
- Especially useful for vLLM/Qwen/short-context models — body stays in sub-LLM, not main

## How
- `fetch_synthesize(kind="rag", id="assort#1", query="최근 주문 5건 요약")`
- query = what to extract from the body in natural language

## Returns
- Short synthesis (1-3 sentences) from sub-LLM
- Body itself (5K-50K chars) stays isolated in sub-LLM call
- Main agent context: only the synthesis (~200 chars)

## Strategy
- Prefer `fetch_synthesize` over `fetch_pd` when body is large or you'll fetch multiple chunks
- One sub-LLM call per chunk — bodies don't accumulate in your context
- For single small chunk: `fetch_pd` is fine (smaller overhead)

## Note
- sub-LLM = same provider, fresh context (stateless)
- Adds 1 extra LLM call per fetch — slower but context-safe
""",

    "fetch_pd": """\
# fetch_pd — Lazy fetch of resource bodies

## When
- An earlier `rag_search` returned an INDEX with snippet but you need the full chunk
- A tool result was truncated to a preview (large outputs)
- Compacted history needs the original

## How
- `fetch_pd(kind="rag", id="assort#2")` — RAG chunk full body
- `fetch_pd(kind="tool_result", id="toolu_xxx")` — original tool output
- `fetch_pd(kind="rag")` (omit id) — list available ids for a kind

## Kinds
- `rag` — RAG chunks (from rag_search)
- `tool_result` — large tool outputs (preview-truncated)
- `history` — compacted conversation
- `db_schema` — DB tables
- `gallery` — gallery tool results
- `graph` — knowledge graph sub-graphs

## Next
- Synthesize from the body
- Don't fetch the same id twice — it's idempotent

## Common mistake
- Calling `fetch_pd` when you don't have an id (list mode + pick id first)
""",

    "check_policy": """\
# check_policy — Pre-validate against active guards

## When
- About to call a sensitive tool (external API, DB write, file delete, anything destructive)
- Want to confirm `<active_policies>` won't block the call

## How
- `check_policy(action="mcp_xxx", args={...})`

## Returns
- `{allowed: bool, reasons: [...], active_guards: [...]}`

## Next
- Allowed → call the tool
- Blocked → tell the user which guard rejected and why; don't retry the same call
""",

    "discover_prompt": """\
# discover_prompt — Browse and load prompt templates

## When
- `<available_prompt_templates>` in system_prompt shows a template that looks relevant

## How
- `discover_prompt(template_type="identity")` — list mode
- `discover_prompt(template_type="identity", name="legal_advisor")` — full body

## Returns
- list: `[{name, description (first 120 chars), length}]`
- name: full template content

## Next
- Incorporate the body into your response style/persona
- Templates are reference material, not callable tools
""",

    "discover_collection": """\
# discover_collection — Sample a RAG collection

## When
- `<reference_resources>` shows a collection name but description is empty/vague
- Want a feel for what's in it before committing to `rag_search`

## How
- `discover_collection(name="assort", sample_size=3)`

## Returns
- Collection metadata + sample document previews (200 chars each)
- OR empty list if no samples available (collection might be empty or
  service unreachable)

## Next (CRITICAL — avoid infinite loop)
1. Samples returned → `rag_search(query=user_intent, collection_name=...)` for the real answer
2. Samples empty → SKIP further `discover_collection` calls. Go directly to
   `rag_search` on the same or another collection — `discover_collection` rarely
   returns more on retry
3. `rag_search` also returns nothing across all attached collections → report
   "no relevant data found in attached collections" and stop
4. NEVER call `discover_collection` on the same name twice
""",

    "rag_search": """\
# rag_search — Vector search the user-attached RAG collections

## When
- The user's question could plausibly be answered by attached document collections
- Domain-specific data, organization records, internal knowledge

## How
- `rag_search(query="...", collection_name="assort", top_k=4)`

## Returns
- INDEX (not full bodies) — each entry shows:
  `[i] id=col#i · source · score · len=N · chunk=M/total · 250-char preview`

## Strategy (avoid infinite loops)
1. Read the index FIRST — decide WHICH chunks are worth fetching
2. `fetch_pd(kind="rag", id="<picked id>")` for each chunk you need
3. Synthesize from the bodies
4. Avoid repeating the same query — if the index doesn't show what you need:
   - Try a different query (synonyms, related concepts)
   - OR try another collection
   - OR report "insufficient data" and stop

## Related: 3 context resource types
- **RAG (vector)** — this tool. Best for semantic similarity.
- **GraphRAG (ontology)** — `query_graph(question, collection)`. Best for entity
  relationships, hierarchies, multi-hop reasoning.
- **Folders** — auto-injected into context (no tool call). User selects in Stage 6.

If a question needs entity relationships > pure semantic similarity → consider
`query_graph` instead.
""",

    "query_graph": """\
# query_graph — Knowledge graph (ontology / GraphRAG) search

## When
- Question involves entity relationships, hierarchies, multi-hop reasoning
- Plain `rag_search` (vector similarity) wouldn't capture the structure
- Examples: "who reports to X?", "what depends on Y?", "trace causality of Z"

## How
- `query_graph(question="natural language question", collection="masahoe")`

## Returns
- Multi-turn ReAct synthesis over SPARQL + semantic chunks + SQL
- Includes citations and reasoning steps

## Next
- Synthesize answer
- If insufficient → consider `rag_search` for additional vector-based context

## Note
- Slower than `rag_search` (multi-turn ReAct backend)
- Use only when relationship/hierarchy question; over-use = high cost
""",

    "compact": """\
# compact — Summarize older context to free budget

## When
- Conversation has large past tool outputs you no longer need verbatim
- Token usage approaching budget

## How
- `compact(scope="tool_results_before:5")` — summarize tool results before turn 5
- `compact(scope="history_before:10", summary_hint="order numbers and amounts only")`
- `compact(scope="pd_store:rag")` — replace pd_store rag bodies with summaries

## Returns
- Count of compacted entries + new summary length

## Warning
- Destructive — original text is replaced by summary
- Subject to HITL approval if guard configured

## Next
- Continue conversation with freed budget
""",
}


def get_skill_body(name: str) -> Optional[str]:
    """Skill body lookup. None 이면 등록 안 됨."""
    return _BUILTIN_SKILL_BODIES.get(name)


def list_skill_names() -> list[str]:
    """등록된 skill 이름 list. discover 용."""
    return sorted(_BUILTIN_SKILL_BODIES.keys())


def register_skill_body(name: str, body: str) -> None:
    """외부 등록 — 외부 wheel 또는 호스트가 자기 skill 추가.

    빈 body 또는 빈 name 은 무시. 동명 등록 = override (마지막 등록 우선).
    """
    if not name or not isinstance(body, str) or not body.strip():
        logger.warning("[skills] register_skill_body skip: name=%r body_empty=%s", name, not body)
        return
    _BUILTIN_SKILL_BODIES[name] = body
    logger.debug("[skills] registered skill body: %s (%d chars)", name, len(body))


def _discover_from_entry_points() -> None:
    """entry_points 그룹 ``xgen_harness.skill_bodies`` 자동 발견.

    [project.entry-points."xgen_harness.skill_bodies"]
    my_skill = "my_pkg:get_skill_body"   # callable () -> str
    """
    try:
        from importlib.metadata import entry_points
        eps = entry_points()
        group = "xgen_harness.skill_bodies"
        if hasattr(eps, "select"):
            items = eps.select(group=group)
        else:
            items = eps.get(group, [])
        for ep in items:
            try:
                fn = ep.load()
                body = fn() if callable(fn) else fn
                if isinstance(body, str):
                    register_skill_body(ep.name, body)
            except Exception as e:
                logger.warning("[skills] entry_point %s 로드 실패: %s", ep.name, e)
    except Exception as e:
        logger.debug("[skills] entry_points discovery 실패: %s", e)


_discover_from_entry_points()
