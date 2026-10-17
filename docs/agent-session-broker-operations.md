# Agent Session broker 작업 클라이언트 / Agent Session broker operation clients

## 범위 / Boundary

`AgentSessionBrokerOperationsClient`는 소유한 Agent Session 턴에 연결된 broker 작업의 공개 참조를 나열하고, 한 작업의 공개 metadata를 읽고, 취소를 접수하는 순수 SDK 클라이언트다. 호출자가 인증된 JSON GET과 빈 본문 JSON POST callback을 주입한다. SDK는 credential, base URL, 환경변수, HTTP 인증, retry, polling, 실행, reconcile 또는 수명 연장을 소유하지 않는다.

`AgentSessionBrokerOperationsClient` is a pure SDK client for listing public broker-operation references attached to an owned Agent Session turn, reading public metadata for one operation, and requesting cancellation. The caller injects authenticated JSON GET and empty-body JSON POST callbacks. The SDK owns no credentials, base URL, environment variables, HTTP authentication, retry, polling, execution, reconciliation, or lifetime extension.

## 고정 API / Fixed API

| SDK method | HTTP contract | Public response |
|---|---|---|
| `list(session_id, turn_id)` | `GET /api/agentflow/agent-sessions/{session_id}/turns/{turn_id}/operations` | bare array, maximum 128 exact references |
| `read(session_id, turn_id, reference)` | `GET .../operations/{invocation_id}` | exact seven-field metadata |
| `cancel(session_id, turn_id, reference)` | `POST .../operations/{invocation_id}/cancel`, empty body, HTTP 202 | exact cancellation acknowledgement |

모든 경로는 query가 없다. callback은 `get(path)`와 `post(path)`처럼 경로 인자 하나만 받는다. 특히 `post`에 JSON, 빈 객체, `None`, header 또는 query를 전달하지 않는다. 호출자 transport는 현재 Platform Session/DPoP, HTTPS origin, 응답 크기와 timeout, HTTP status를 처리하고 성공 JSON만 반환해야 한다. SDK는 callback을 한 번만 호출하며 transport 예외와 task cancellation을 그대로 전달한다.

Every path is queryless. Each callback accepts one path argument as `get(path)` or `post(path)`. In particular, the SDK passes no JSON, empty object, `None`, headers, or query to `post`. The caller transport handles the current Platform Session/DPoP, HTTPS origin, response size and timeout, and HTTP status, then returns successful JSON. The SDK calls the callback once and propagates transport errors and task cancellation unchanged.

```python
from xgen_sdk.agent_session import AgentSessionBrokerOperationsClient

# Both callbacks authenticate with the caller's current session.
# authenticated_empty_json_post(path) sends an HTTP request with zero body bytes.
client = AgentSessionBrokerOperationsClient(
    authenticated_json_get,
    authenticated_empty_json_post,
)

references = await client.list(session_id, turn_id)
if references:
    metadata = await client.read(session_id, turn_id, references[0])
    accepted = await client.cancel(session_id, turn_id, references[0])
```

## 공개 DTO와 검증 / Public DTOs and validation

세션, 턴, invocation ID는 nonzero UUID를 lower canonical 문자열로 직렬화했을 때 입력과 정확히 같아야 한다. SDK는 세션과 턴 ID를 transport 호출 전에 검사한다. 참조 목록은 정확한 기본 JSON 배열이어야 하며 최대 128개이고 invocation ID가 고유해야 한다. 반환한 Python tuple과 모든 DTO는 불변이다. 추가 필드, coercion, model subclass, `model_construct`/`model_copy` 우회 값은 parser에서 다시 검사한다.

Session, turn, and invocation identifiers must be nonzero UUIDs whose lower canonical string exactly equals the input. The SDK validates session and turn identifiers before transport. A reference list must be an exact plain JSON array with at most 128 entries and unique invocation identifiers. The returned Python tuple and every DTO are immutable. Parsers revalidate extra fields, coercion, model subclasses, and values forged through `model_construct` or `model_copy`.

참조는 정확히 `invocation_id`, `operation_id`만 가진다. 허용 operation은 다음 여섯 개다.

A reference contains exactly `invocation_id` and `operation_id`. The operation allowlist is:

- `documents.collections.list`
- `documents.collection.documents.list`
- `workflow.actor.authorize`
- `documents.attachment.index`
- `workflow.code.execute`
- `workflow.attachments.stage`

Metadata는 정확히 `invocation_id`, `operation_id`, `state`, `cancel_requested`, `target_cleanup`, `result_available`, `expires_at`만 가진다. `read`는 응답의 invocation과 operation을 모두 호출자가 보존한 expected reference에 묶는다. `state`는 `running`, `completed`, `failed`, `cancelled`, `unknown` 중 하나다. `expires_at`은 1 이상 JavaScript safe integer 이하의 strict integer이며 bool을 거부한다.

Metadata contains exactly `invocation_id`, `operation_id`, `state`, `cancel_requested`, `target_cleanup`, `result_available`, and `expires_at`. `read` binds both response invocation and operation to the caller-retained expected reference. `state` is one of `running`, `completed`, `failed`, `cancelled`, or `unknown`. `expires_at` is a strict integer from 1 through the JavaScript safe-integer maximum; booleans are rejected.

`workflow.code.execute`와 `workflow.attachments.stage`만 Sandbox 작업이다. Sandbox의 `target_cleanup`은 `pending`, `complete`, `unknown` 중 하나이고 `not_applicable`은 금지된다. 다른 네 operation은 `not_applicable`이어야 한다. `result_available=true`는 Sandbox 작업이 `completed`일 때만 허용한다. 공개 metadata에는 결과 본문이 없으며 `result`, grant, lease, 입력 body 같은 private 필드를 거부한다.

Only `workflow.code.execute` and `workflow.attachments.stage` are Sandbox operations. Their `target_cleanup` is `pending`, `complete`, or `unknown`; `not_applicable` is forbidden. The other four operations require `not_applicable`. `result_available=true` is allowed only for a completed Sandbox operation. Public metadata contains no result body and rejects private fields such as `result`, grants, leases, or input bodies.

취소 응답은 정확히 `invocation_id`와 `cancel_requested: true`만 가진다. 이것은 HTTP 202 접수 증거다. 최종 state, process 종료, stage 삭제 또는 `target_cleanup=complete`를 뜻하지 않는다. 정리 상태는 이후 명시적인 `read` 응답에서만 판단한다.

A cancellation response contains exactly `invocation_id` and `cancel_requested: true`. This is HTTP 202 acceptance evidence. It does not mean a final state, process termination, stage deletion, or `target_cleanup=complete`. Cleanup is interpreted only from a later explicit `read` response.

검증 실패는 원시 payload와 exception context를 노출하지 않는 고정 `AgentSessionBrokerOperationValidationError`다. 공개 fixture `tests/fixtures/agent-session-broker-operations.json`은 SDK와 Frontend가 같은 유효·변조 벡터를 사용하도록 하며 credential, grant, lease 또는 실제 결과를 포함하지 않는다.

Validation failures use a fixed `AgentSessionBrokerOperationValidationError` without raw payload or exception context. The public fixture at `tests/fixtures/agent-session-broker-operations.json` lets SDK and Frontend consume the same valid and tampered vectors and contains no credential, grant, lease, or real result.
