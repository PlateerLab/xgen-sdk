# Canonical 턴 제출 계약 / Canonical turn submission contract

## 범위 / Boundary

`SubmitAgentSessionTurn`은 Canonical Agent Session에 턴을 제출할 때 쓰는 순수 SDK wire DTO다. 이 모델과 helper는 요청 모양만 검사한다. 호출자를 인증하거나, 파일을 업로드하거나, receipt/HMAC을 검증하거나, 첨부의 서버 소유권·저장 상태·checksum을 증명하지 않는다.

`SubmitAgentSessionTurn` is the pure SDK wire DTO for a canonical Agent Session turn submission. The model and helpers validate request shape only. They do not authenticate a caller, upload content, verify a receipt or HMAC, or prove server ownership, durable storage, or the checksum of an attachment.

## 요청 / Request

| 필드 / Field | 타입 / Type | 제약 / Constraint |
|---|---|---|
| `input_text` | string | 1..262144자, coercion 없음 / 1..262144 characters, no coercion |
| `expected_state_version` | integer | strict integer `>= 1`; bool·문자열 거부 / bool and string rejected |
| `idempotency_key` | string | 1..128자, coercion 없음 / 1..128 characters, no coercion |
| `origin_id` | string or null | 선택, 있으면 1..128자 / optional, 1..128 characters when present |
| `attachments` | array | 선택, 기본 `[]`; 순서가 있는 고유 `AgentAttachmentReference` 최대 10개 / optional, defaults to `[]`; at most ten ordered unique references |

모델은 추가 필드를 거부하고 불변이다. Python의 `attachments`는 내부에서 tuple이며 JSON에서는 배열이다. FastAPI가 JSON 배열을 Python `list`로 전달하는 것은 허용하지만 iterator와 list/tuple subclass는 거부한다. 각 참조는 `attachment_id`와 `sha256`만 포함하며, 이미 만들어진 Pydantic 모델도 우회 생성 여부를 신뢰하지 않고 다시 검사한다.

The model forbids extra fields and is immutable. Python stores `attachments` as a tuple and emits a JSON array. A plain list produced by FastAPI JSON parsing is accepted; iterators and list/tuple subclasses are rejected. Every reference contains only `attachment_id` and `sha256`, and even an existing Pydantic instance is copied and fully revalidated against construction bypasses.

## Helpers와 서버 책임 / Helpers and server responsibilities

- `parse_attachment_references(value)`는 정확한 기본 list/tuple만 받아 순서와 ID 고유성을 검사하고 새 불변 tuple을 반환한다.
- `parse_turn_submission(value)`는 정확한 `SubmitAgentSessionTurn` 인스턴스만 받아 모든 필드를 다시 검사한 새 DTO를 반환한다.
- helper 실패는 원시 입력이나 예외 context를 보존하지 않는 일반 오류만 공개한다.

- `parse_attachment_references(value)` accepts only an exact built-in list or tuple, checks order and unique IDs, and returns a new immutable tuple.
- `parse_turn_submission(value)` accepts only an exact `SubmitAgentSessionTurn` instance and returns a newly validated DTO.
- Helper failures expose only a generic error without raw input or exception context.

서버 계약은 `attachments`가 생략되거나 빈 배열이면 기존 v1 text hash를 유지한다. 비어 있지 않으면 순서가 보존된 참조만 v2 HMAC 입력에 포함한다. 합계 100 MiB 제한은 클라이언트가 보낸 참조가 아니라 authoritative DB metadata로 서버가 검사해야 한다. 서버는 Gateway 인증, 세션과 workflow 소유권, ACTIVE 상태, attachment 소유권·저장 완료·checksum, idempotency 충돌과 실행 권한을 별도로 검증한다. JSON Schema에도 최대 10개 상한을 표시한다.

The server contract retains the existing v1 text hash when `attachments` is omitted or empty. For nonempty attachments, only the ordered references enter the v2 HMAC input. The server must enforce the 100 MiB aggregate cap from authoritative database metadata rather than client references. Gateway authentication, session/workflow and attachment ownership, ACTIVE state, committed storage/checksum, idempotency conflicts, and execution authorization remain server checks. JSON Schema also advertises the ten-item ceiling.
