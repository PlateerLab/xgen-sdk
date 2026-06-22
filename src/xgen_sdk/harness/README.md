# `xgen_sdk.harness` — 내장 에이전트 하네스 엔진

LLM이 도구를 자율 조립해 실행하는 **10-stage 에이전트 파이프라인**. 원래 별도 패키지
(`xgen-harness`)였으나 v1.17.0에서 **SDK에 소스째 내장**됐다. 하네스 수정·배포는 이제
SDK 릴리즈로 나간다.

- **순수 Python** (httpx만) — **LangChain 의존 없음**. provider-agnostic, domain-agnostic.
- **엔진 core는 `xgen_sdk`를 import하지 않는다.** 플랫폼 연결은 `_sdk/` 통합층 한 곳에서만.

## 빠른 사용

```python
# (권장) 앱에 연결된 하네스 — DB 세션영속 + config 키 + logging 자동 wiring
from xgen_sdk import XgenApp
xgen = XgenApp().boot()
h = xgen.harness(provider="anthropic", model="claude-sonnet-4-6", max_iterations=5)
state = await h.run("질문", session_id="user-42")   # 세션 DB 영속 + resume

# (standalone) 직접
from xgen_sdk.harness import Harness
h = Harness(provider="anthropic", model="claude-sonnet-4-6")   # 키는 xgen_sdk.config→env 순 해석
h.delete_step("s06_context")             # 비필수 stage 제거
h.add_step("s_audit", MyAuditStage)      # 커스텀 stage 추가 (Stage 서브클래스)
print(h.steps())
state = await h.run("질문")

# (저수준) 엔진 직접
from xgen_sdk.harness import HarnessConfig, Pipeline
pipe = Pipeline.from_config(HarnessConfig(provider="anthropic", model="..."))
```

## 구조

```
harness/
├── _sdk/          ← SDK 통합층 (유일하게 xgen_sdk.db/config/logging 연결)
│   ├── facade.py        Harness — add/delete/enable_step · steps · run(session_id) · build
│   ├── store.py         XgenDBSessionStore — 세션을 xgen_sdk.db(harness_sessions)에 영속
│   └── events.py        logging_emitter — 실행 이벤트 → xgen_sdk.logging.BackendLogger
│
├── core/          Pipeline · HarnessConfig · registry · session · state · builder ·
│                  execution_context · fs_scanner(stage 자동발견) · planner · sandbox · nom
├── stages/        s00_harness · s01_input · s02_history · s03_prompt · s04_tool ·
│                  s05_policy · s06_context · s07_act · s08_decide · s09_finalize + strategies/
├── providers/     anthropic · openai · base (httpx SSE, OpenAI 호환 shim=google/bedrock/vllm)
├── tools/         ToolSource · gallery · rag_tool · frozen_source · skill_registry
├── capabilities/  Capability 매칭·머티리얼라이즈
├── memory/        SessionStore Protocol · ProgressLog · recall
├── events/        EventEmitter · 이벤트 타입 (SSE 스트리밍)
├── compile/       워크플로우 → npm/wheel standalone 컴파일 (→ compile/README.md)
├── forge/         Self-Forging — config 자가개선 루프(GEPA 반성 진화). **opt-in**, `import xgen_sdk.harness`엔 미로드. `from xgen_sdk.harness.forge import SelfForge, forge_config`
├── adapters/      node_adapters · embedders · resource_registry
├── config/ · errors/ · interfaces/ · api/(FastAPI 라우터) · utils/
```

## 파이프라인 (10-stage)

```
ingress: s00_harness → s01_input → s02_history → s04_tool → s03_prompt
loop:    s05_policy → s06_context → s07_act → s08_decide   (s08이 계속/종료 판정, max_iterations)
egress:  s09_finalize
```

- **REQUIRED 3종** `s01_input` · `s08_decide` · `s09_finalize` — 비활성 거부(`toggle_stage` 가드).
- `ALL_STAGES`(s01~s09) 화이트리스트 + `disabled_stages`로 stage on/off. `s00_harness`=본문 LLM 호출.

## 확장 (코어 수정 0)

- **stage 추가**: `Harness.add_step()` → `register_stage()`, 또는 `stages/sNN_xxx/` 디렉터리만
  만들면 `fs_scanner`가 자동 발견(파일 구조가 곧 카탈로그).
- **plugin entry_points**: `xgen_harness.{stages,strategies,providers,guards,tools,session_stores,…}`
  그룹에 외부 패키지가 등록 → 부팅 시 자동 합류. (그룹명은 **호환 계약**이라 내장 후에도 유지)
- **provider 교체**: `register_provider("google", NativeGeminiProvider)` 또는 entry_points.
- **Guard / Strategy / Orchestrator / ToolSource / Capability** 모두 같은 register_* + entry_points 패턴.

## SDK 통합 (`_sdk/`)

| 무엇 | 어떻게 |
|---|---|
| provider 키 | `xgen_sdk.config.get_config_value(get_api_key_env(provider))` → 없으면 env |
| 세션 영속 | `XgenDBSessionStore`가 엔진 `SessionStore` Protocol을 `xgen_sdk.db`로 구현 (`harness_sessions` 테이블, `session_id`로 resume) |
| 로깅 | `logging_emitter`가 EventEmitter 이벤트를 `BackendLogger`로 전달 |
| 앱 진입 | `XgenApp.harness(provider=, model=, persist=, logger=)` — 위 셋을 자동 wiring |

## 경계 / 의존

- 런타임 의존 = **httpx**(provider) · mcp(MCP 툴). 엔진 core는 `xgen_sdk` 미import — `_sdk/`와
  `__init__`(re-export)만 플랫폼에 닿는다. → `import xgen_sdk`만으론 하네스 미로드(명시 사용 시에만).
- 공개 API는 `xgen_sdk.harness` 최상위에서 re-export (`Harness`, `HarnessConfig`, `Pipeline`,
  `Stage`, `SessionStore`, `EventEmitter`, `register_*`, `ALL_STAGES`, `REQUIRED_STAGES` 등).

## 포인터

- 컴파일(standalone 산출물 + publish 토큰/노드엔진): [`compile/README.md`](compile/README.md)
- 배포: SDK 릴리즈(`publish-pypi.yml`, release published → PyPI). 하네스 수정 = SDK 버전 bump.
