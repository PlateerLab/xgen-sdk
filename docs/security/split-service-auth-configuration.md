# Service trust keys and authorization policy / 서비스 신뢰 키와 인가 정책

## English

The default `XGEN_AUTH_CONFIG_MODE=legacy` preserves the existing
`XGEN_TRUSTED_SERVICES` contract. Enable `split-v1` explicitly to manage secret
keys separately from receiver-specific permission grants. No JWT, HS256, RS256,
audience, header-binding, or token-lifetime wire contract changes are required.

For a receiving service named `xgen-workflow`, set:

```text
XGEN_AUTH_CONFIG_MODE=split-v1
XGEN_SERVICE_NAME=xgen-workflow
```

Put the following JSON in the secret `XGEN_TRUSTED_SERVICE_KEYS`:

```json
{
  "workflow-service": {
    "keys": {
      "current": {"key": "replace-with-a-unique-secret-of-at-least-32-bytes"},
      "previous": {
        "key": "replace-with-another-unique-secret-at-least-32-bytes",
        "not_after": 1798761600
      }
    }
  }
}
```

Each issuer has exactly one active key without `not_after`. Other keys require
a positive integer Unix cutoff; the entire token lifetime must end by that
cutoff. The issuer must sign using the matching `XGEN_SERVICE_KEY_ID`. Split
mode does not accept the unkeyed legacy service assertion or `{key: ...}`
configuration. Remove an entry to revoke its key.

Put this nonsecret receiver policy JSON in
`XGEN_SERVICE_AUTHORIZATION_POLICY`:

```json
{
  "schema_version": 1,
  "receiver": "xgen-workflow",
  "grants": {
    "workflow-service": {"permissions": ["document:read", "workflow:execute"]}
  }
}
```

`receiver` must exactly equal `XGEN_SERVICE_NAME`. Permission lists must contain
unique, finite canonical strings, with no wildcard, whitespace, control
characters, comma separators, or percent-encoded aliases. Each token's declared
permissions must be a subset of that issuer's grant. An issuer with trusted
keys and no grant is denied, even for a token that declares no permissions.
A grant for an issuer without trusted keys fails configuration validation.
An empty permission list explicitly allows an authenticated issuer to declare
no permissions; endpoint authorization still applies.

Remove `XGEN_TRUSTED_SERVICES` entirely when selecting split mode: even an empty
legacy variable is rejected. Both split variables are mandatory. To accept no
incoming service callers, use `XGEN_TRUSTED_SERVICE_KEYS={}` and a complete
policy with `"grants": {}`. Gateway users can still authenticate with the
existing Gateway public key or keyring configuration. Split settings under
legacy mode, unknown modes, duplicate JSON members, non-finite numbers, unknown
schema fields, and malformed keys fail validation. No legacy policy is merged
or used as a fallback.

Call the synchronous startup API before accepting requests:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from xgen_sdk.auth import PrincipalMiddleware, validate_auth_configuration

@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_auth_configuration()  # Raises PrincipalConfigurationError on invalid configuration.
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(PrincipalMiddleware)
```

The validator performs no network requests and does not include key values in
errors. Split mode requires a valid receiving service name and Gateway verifier,
as well as the two split JSON variables. Legacy mode validates configured
receiving components and permits unconfigured standalone local development.
`PrincipalMiddleware` also validates split settings during ASGI lifespan
startup; services with custom startup paths should call the API explicitly.
Request verification reads the current environment without a cache, so removing
a key or grant takes effect for subsequent verifications in that process.
Deploy updated environment values to each worker to apply changes in production.

For migration, first install a tested SDK build while retaining legacy mode.
Configure a signing key ID on issuers and deploy keyed assertions together with
the matching legacy receiver keyring. Then deploy both split maps, the exact
receiver, and `split-v1` while removing the legacy map in the same rollout.
Rollback requires a matching legacy map and mode together. Never publish local
test builds to a public package index.

## 한국어

기본 `XGEN_AUTH_CONFIG_MODE=legacy`는 기존 `XGEN_TRUSTED_SERVICES` 동작을
유지합니다. 비밀 키와 수신 서비스별 권한을 따로 관리할 때 `split-v1`을 명시적으로
선택합니다. JWT, HS256, RS256, audience, 헤더 바인딩, 토큰 수명 계약은 유지됩니다.
위 JSON 예시처럼 비밀 키는 `XGEN_TRUSTED_SERVICE_KEYS`, 비밀이 아닌 수신 정책은
`XGEN_SERVICE_AUTHORIZATION_POLICY`에 넣습니다.

발신자마다 `not_after`가 없는 활성 키가 정확히 하나 있어야 합니다. 이전 키에는
양의 정수 Unix 종료 시각이 필요하며 토큰의 `exp`가 그 시각을 넘으면 거부됩니다.
발신자의 `XGEN_SERVICE_KEY_ID`는 수신 키맵의 키 ID와 일치해야 합니다. split 모드는
키 ID가 없는 기존 서비스 토큰이나 `{key: ...}` 설정을 허용하지 않습니다.
키 항목을 삭제하면 해당 키를 폐기할 수 있습니다.

정책의 `schema_version`은 정수 `1`, `receiver`는 `XGEN_SERVICE_NAME`과 정확히
같아야 합니다. 권한은 중복 없는 유한한 문자열이어야 하며 와일드카드, 공백,
제어 문자, 쉼표 구분자, 퍼센트 인코딩 별칭을 허용하지 않습니다. 토큰이 선언한
권한은 발신자에게 부여된 목록의 부분집합이어야 합니다. 키만 있고 grant가 없는
발신자는 권한을 전혀 선언하지 않은 토큰도 거부됩니다. 반대로 키가 없는 발신자를
참조하는 grant는 설정 오류입니다. 빈 권한 목록을 명시한 grant는 권한이 없는
인증 토큰만 허용하며 실제 엔드포인트의 인가 검사는 그대로 적용됩니다.

split 모드에서는 `XGEN_TRUSTED_SERVICES`를 빈 값까지 완전히 제거해야 합니다.
두 split 설정은 필수입니다. 서비스 발신자를 받지 않는 경우 키맵은 `{}`, 정책은
필수 스키마와 정확한 receiver를 포함한 `"grants": {}`로 설정합니다. 기존 Gateway
공개 키나 키링을 통한 사용자 인증은 계속 동작합니다. legacy 모드에 split 설정을
넣거나 알 수 없는 모드, 중복 JSON 필드, 유한하지 않은 숫자, 알 수 없는 스키마
필드, 잘못된 키를 넣으면 검증에 실패합니다. 기존 정책과 합치거나 대체하지 않습니다.

위 Python 예시처럼 요청 처리 전 lifespan에서 동기 함수
`validate_auth_configuration()`을 호출합니다. 잘못된 설정은 비밀 키 값을 노출하지
않는 `PrincipalConfigurationError`로 중단하며 네트워크 호출은 하지 않습니다.
split 모드에서는 수신 서비스 이름, Gateway 검증 키, 두 JSON 설정이 모두 필요합니다.
legacy 모드는 설정된 수신 인증 요소만 검증하며 설정이 없는 단독 로컬 개발도
허용합니다. `PrincipalMiddleware`도 split 모드의 ASGI lifespan 시작 시 검증하지만
별도 시작 경로를 쓰는 서비스는 API를 직접 호출합니다. 요청 검증은 캐시 없이 현재
환경을 읽으므로 같은 프로세스에서 키나 grant를 제거하면 다음 검증부터 반영됩니다.
운영 환경에서는 모든 worker에 변경된 환경 설정을 배포해야 합니다.

이관 시 검증한 SDK를 먼저 legacy 모드로 설치합니다. 발신자의 키 ID 설정과 키 ID가
있는 토큰을 수신자의 기존 키링 설정과 맞춰 배포한 뒤 두 split 맵, 정확한 receiver,
`split-v1`을 배포하면서 기존 맵을 함께 제거합니다. 롤백도 기존 맵과 legacy 모드를
함께 복구합니다. 로컬 테스트 빌드는 공개 패키지 인덱스에 게시하지 않습니다.
