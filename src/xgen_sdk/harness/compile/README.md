# `xgen_sdk.harness.compile` — 워크플로우 컴파일러

하네스 워크플로우(canvas)를 **env-only standalone 산출물**로 컴파일한다.
두 채널: **npm tarball**(기본, v0.29+) / **Python wheel**(병행, v1.10+).

> ⚠️ **컴파일 자체는 토큰이 필요 없다.** compile/ 코드는 *파일 생성*만 하며 import·실행에
> 시크릿을 안 쓴다. 토큰·외부 엔진은 *산출물을 publish/실행*할 때만 필요 — 아래 "전달 요건" 참조.

## 컴파일 로직 (npm 채널)

```
워크플로우 + HarnessConfig
   │  ① snapshot.py — WorkflowSnapshot (결정적 스냅샷)
   ▼
WorkflowSnapshot
   │  ② npm_spec.build_spec → HarnessSpec  (spec.json)
   │     · HarnessConfig 모든 필드를 1:1 로 정규화 (fully equivalent)
   │     · 외부 코드 의존 도구(xgen-nodes/http/rag/mcp/canvas)는 freeze_*() 로
   │       input_schema + 호출 메타를 spec 에 freeze → TS runner 가 직접 호출
   │     · 결정적 (sorted keys, 시간/파일 비의존)
   ▼
spec.json
   │  ③ npm_pack.compile_workflow_to_npm / build_npm_package
   ▼
xgen-harness-{name}-{ver}.tgz
   ├── package.json   { name: "xgen-harness-{name}",
   │                    dependencies: { "@plateer-xgen/harness-engine-node": "^0.31.6" } }
   ├── bin/cli.js     require("@plateer-xgen/harness-engine-node").serve(spec)  ← thin wrapper
   └── spec.json
   │  ④ 실행: npx -y xgen-harness-{name} serve-mcp
   ▼
npm 이 engine-node 를 npmjs 에서 자동 설치 → spec.json 대로 전체 stage/strategy 재현 (env-only)
```

**Python 채널**: `python_compile.transpile_to_python` → `python_pack.build_wheel/build_sdist` → PyPI.

## 채널 / 실행 모드 매트릭스

한 워크플로우를 어느 채널로 뽑든 **stdio MCP 서버로 실행**된다. 배포처만 다르다.

| 채널 | 산출물 | 실행 (stdio MCP) | 배포처 |
|---|---|---|---|
| **npm** | `xgen-harness-{name}.tgz` | `npx -y xgen-harness-{name} serve-mcp` — engine-node 가 stdio 서버 | npm/npx (wrapper 는 minio presigned, engine-node 는 npmjs) |
| **Python** | wheel/sdist + `{module}/mcp.py` | `python -m {module}.mcp` — FastMCP stdio (`claude mcp add … -- python -m {module}.mcp`) | **PyPI** (console_scripts: `{module}` CLI + `{module}-mcp`) |

→ **stdio(MCP)는 npm·Python 양쪽 다, PyPI 배포는 Python 채널.** `include_mcp=True`(기본)면 Python
산출물에 FastMCP 엔트리(`mcp = ["fastmcp>=0.2.0"]` extra)가 포함된다.

## 파일 맵

| 파일 | 역할 |
|---|---|
| `snapshot.py` | `WorkflowSnapshot` — 결정적 워크플로우 스냅샷 |
| `npm_spec.py` | `build_spec`/`HarnessSpec`/`freeze_*` — 설정 정규화 + 도구 freeze |
| `npm_pack.py` | `compile_workflow_to_npm`/`build_npm_package` — tarball + package.json/cli.js |
| `external_inputs.py`, `_env_hints.py` | 산출물 *실행 시* 필요한 env(API key/secret) 스캔·안내 (컴파일엔 불필요) |
| `python_compile.py`, `python_pack.py` | Python(wheel) 채널 |
| `gallery.py`, `local_manifest.py`, `nom_compile.py` | 갤러리 발견 / 매니페스트 / NOM 그래프 |

## 전달(handoff) 요건 — 따로 챙길 것

산출물을 **배포·실행**하려면 (컴파일이 아니라 그 다음 단계):

| 대상 | 무엇 | 필요 자원 |
|---|---|---|
| **노드 런타임 엔진** | `@plateer-xgen/harness-engine-node@^0.31.6` — 별도 npm 패키지. 컴파일 산출물이 `require` 한다 | npmjs `@plateer-xgen` 스코프 **publish 권한(npm 토큰)**. 산출물이 돌려면 이게 npmjs 에 올라가 있어야 함 |
| 컴파일 wrapper(`xgen-harness-{name}.tgz`) | 워크플로우별 tarball | 설계상 **minio presigned** 배포(npm publish 아님) → minio 자격 |
| Python 채널 산출물 | wheel/sdist | **PyPI 토큰** |

**핵심: 진짜 따로 전달할 deliverable = 노드 엔진 `@plateer-xgen/harness-engine-node`(+ 그 npm publish 토큰).**
compile 코드 자체가 아니다 — 코드는 SDK 안에 그대로 있어도 무해(토큰·추가 의존성 0).

> 토큰(npm/PyPI)은 레포에 두지 않는다 — 배포 담당자 자격 보관소에서 관리.
> 상수: `ENGINE_PACKAGE` / `DEFAULT_ENGINE_DEP` (`npm_pack.py`). 버전 갱신 시 여기만 수정.
