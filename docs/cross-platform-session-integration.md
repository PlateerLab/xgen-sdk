# Cross-Platform Session 통합 브랜치

- 기준: `main` (`561315c96bf625e8b135da9212243ece4ab8d590`)
- 상태: Draft. 서버와 클라이언트 계약 및 관련 검증이 끝날 때까지 `main`에 병합하지 않는다.

## SDK 작업 경계

1. Gateway가 발급한 세션 principal의 `sid`, tenant, platform, device, trust 및 audience를 서비스가 일관되게 해석할 계약을 만든다. 호출자가 보낸 사용자 헤더만으로 신원을 확정하지 않는다.
2. Agent Session의 서버 발급 ID, 이벤트 sequence/cursor, 재연결 및 중복 제거 계약을 공통 타입으로 제공한다.
3. 개인 설정 catalog의 타입, 정책 잠금, 버전 충돌과 출처를 반환하는 클라이언트 인터페이스를 제공한다. Secret 값은 일반 설정 응답 타입에 포함하지 않는다.

각 항목은 서버 계약이 확정된 뒤 하위 브랜치와 별도 PR로 구현한다. 현재 브랜치의 문서는 범위와 기준 커밋만 고정한다.
