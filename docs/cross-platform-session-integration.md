# Cross-Platform Session 통합 브랜치

- 기준: `main` (`561315c96bf625e8b135da9212243ece4ab8d590`)
- 상태: Draft. 서버와 클라이언트 계약 및 관련 검증이 끝날 때까지 `main`에 병합하지 않는다.

## SDK 작업 경계

1. Gateway가 발급한 세션 principal의 `sid`, tenant, platform, device, trust 및 audience를 서비스가 일관되게 해석할 계약을 만든다. 호출자가 보낸 사용자 헤더만으로 신원을 확정하지 않는다.
2. Agent Session의 서버 발급 ID, 이벤트 sequence/cursor, 재연결 및 중복 제거 계약을 공통 타입으로 제공한다.
3. 개인 설정 catalog의 타입, 정책 잠금, 버전 충돌과 출처를 반환하는 클라이언트 인터페이스를 제공한다. Secret 값은 일반 설정 응답 타입에 포함하지 않는다.

Workflow의 계정 포커스 계약이 확정되어 `xgen_sdk.agent_session`에 생성·전환 요청, 포커스·이벤트 응답, 409 충돌 응답 모델을 추가한다. `apply_event_page`는 계정 이벤트의 연속 sequence, 이전 포인터, cursor·snapshot 일관성을 검사하고 중복 응답을 무시한다. 불일치나 서버의 `ACCOUNT_CURSOR_AHEAD`/`ACCOUNT_CURSOR_GAP`에서는 `GET /api/agentflow/me/agent-state`로 스냅샷을 다시 받은 뒤 그 version부터 재조회한다. SDK 모델은 인증을 수행하지 않는다. 호출자는 Gateway에서 Platform Session·DPoP로 검증된 경로만 사용해야 한다.

Workflow의 Agent Session 이벤트 계약도 `AgentSessionSnapshot`, `AgentSessionEventPage`, `AgentSessionEventFrame`, `SessionCursorConflictResponse`로 제공한다. `apply_session_event_page`는 HTTP `after_sequence` 또는 WebSocket `after_seq`로 요청한 순번부터 연속성을 검사하고, 겹쳐 온 페이지를 중복 적용하지 않으며, 마지막으로 검증한 이벤트의 순번과 ID를 보존한다. `SESSION_CURSOR_AHEAD`/`SESSION_CURSOR_GAP` 또는 SDK의 `AgentSessionReplayGap`에서는 이어받기를 중단하고 snapshot과 로컬 투영을 재조정해야 한다. SDK는 이 모델만 제공하며 네트워크 인증이나 자동 재시도를 수행하지 않는다.

Workflow의 `GET /api/agentflow/agent-sessions/{id}/messages`에 맞춰 `AgentSessionMessagePage`, `AgentSessionMessageCursor`, `AgentSessionMessageConflictResponse`와 `apply_message_page`를 제공한다. 이 API는 저널에 연결된 완료 턴만 돌려주므로 메시지 sequence는 이벤트 사이에서 건너뛸 수 있고, `has_more=false`여도 `next_cursor`가 `snapshot_sequence`보다 작을 수 있다. SDK는 해당 세션의 마지막 턴 ID로 겹친 페이지를 확인하고 중복을 제외한다. cursor·턴 anchor·응답 구조가 어긋나면 `AgentSessionMessageGap`으로 중단한다. 입력·출력 텍스트가 없을 수 있으며 `content_complete`와 `source`가 그 상태와 일치해야 한다. 이 페이지를 전체 대화 snapshot으로 취급하거나 Agent Session 이벤트 cursor를 전진시키지 않는다. 인증·소유권 검사와 409 `SESSION_MESSAGE_LINK_INVALID` 처리 책임은 Gateway·Workflow 및 호출자에게 있다.

나머지 항목은 서버 계약이 확정된 뒤 하위 브랜치와 별도 PR로 구현한다.

## Canonical 첨부 참조 계약 (2026-10-06)

[공통 첨부 계약](canonical-attachment-reference-contract.md)의 `AgentAttachmentScope`, `AgentAttachmentReceipt`, `AgentAttachmentReference`와 엄격한 parse/prepare helper를 제공한다. origin·계정·세션·workflow를 정확히 비교하고, ID/checksum 참조를 복사한 frozen 모델/tuple로 반환한다. 중복 ID·잘못된 Unicode·크기/개수/합계 초과와 경로·URL·raw bytes·권한 주장을 거부한다. helper 예외는 원시 입력을 노출하지 않는다. SDK는 업로드·인증·checksum 계산·턴 제출을 수행하지 않으며 현재 서버의 text-only 계약은 유지한다.

DEX와 동일한 JSON 입력 벡터를 적용하며 Python3.11/3.12 계약 CI를 추가한다. CI는 source 모델 테스트만 실행하고 패키지를 발행하지 않는다. 배포 없이 Workflow의 임시 source overlay에서 실제 import와 동작을 확인한다. 서버 업로드·소유권/저장 checksum 확인·저널 hash 및 실행 전달은 후속 단계다. `main` 대상 상위 PR53은 Draft로 유지한다.

The SDK supplies strict, scoped attachment metadata models and immutable ordered reference preparation, using the same fixtures as DEX. Parsing cannot authenticate a receipt or authorize stored bytes. Python3.11/3.12 CI validates unreleased source contracts without publishing. The local Workflow overlay checks real imports; upload, storage ownership/checksum, journal and execution integration remain subsequent work. Parent PR53 remains Draft.
