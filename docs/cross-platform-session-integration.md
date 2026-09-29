# Cross-Platform Session 통합 브랜치

- 기준: `main` (`561315c96bf625e8b135da9212243ece4ab8d590`)
- 상태: Draft. 서버와 클라이언트 계약 및 관련 검증이 끝날 때까지 `main`에 병합하지 않는다.

## SDK 작업 경계

1. Gateway가 발급한 세션 principal의 `sid`, tenant, platform, device, trust 및 audience를 서비스가 일관되게 해석할 계약을 만든다. 호출자가 보낸 사용자 헤더만으로 신원을 확정하지 않는다.
2. Agent Session의 서버 발급 ID, 이벤트 sequence/cursor, 재연결 및 중복 제거 계약을 공통 타입으로 제공한다.
3. 개인 설정 catalog의 타입, 정책 잠금, 버전 충돌과 출처를 반환하는 클라이언트 인터페이스를 제공한다. Secret 값은 일반 설정 응답 타입에 포함하지 않는다.

Workflow의 계정 포커스 계약이 확정되어 `xgen_sdk.agent_session`에 생성·전환 요청, 포커스·이벤트 응답, 409 충돌 응답 모델을 추가한다. `apply_event_page`는 계정 이벤트의 연속 sequence, 이전 포인터, cursor·snapshot 일관성을 검사하고 중복 응답을 무시한다. 불일치나 서버의 `ACCOUNT_CURSOR_AHEAD`/`ACCOUNT_CURSOR_GAP`에서는 `GET /api/agentflow/me/agent-state`로 스냅샷을 다시 받은 뒤 그 version부터 재조회한다. SDK 모델은 인증을 수행하지 않는다. 호출자는 Gateway에서 Platform Session·DPoP로 검증된 경로만 사용해야 한다.

Workflow의 Agent Session 이벤트 계약도 `AgentSessionSnapshot`, `AgentSessionEventPage`, `AgentSessionEventFrame`, `SessionCursorConflictResponse`로 제공한다. `apply_session_event_page`는 HTTP `after_sequence` 또는 WebSocket `after_seq`로 요청한 순번부터 연속성을 검사하고, 겹쳐 온 페이지를 중복 적용하지 않으며, 마지막으로 검증한 이벤트의 순번과 ID를 보존한다. `SESSION_CURSOR_AHEAD`/`SESSION_CURSOR_GAP` 또는 SDK의 `AgentSessionReplayGap`에서는 이어받기를 중단하고 snapshot과 로컬 투영을 재조정해야 한다. SDK는 이 모델만 제공하며 네트워크 인증이나 자동 재시도를 수행하지 않는다.

나머지 항목은 서버 계약이 확정된 뒤 하위 브랜치와 별도 PR로 구현한다.
