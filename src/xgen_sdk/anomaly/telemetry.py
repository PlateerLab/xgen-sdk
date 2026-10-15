"""시스템 알람이 보는 운영 이벤트 적재: core · workflow · documents 가 같은 함수를 쓴다.

xgen-core 의 시스템 알람(``service.anomaly_alerts``)은 표 ``anomaly_runtime_events`` 에 들어온 행을 트리거로
받아 규칙(야간 대량 입력 · 대량 다운로드 · LLM 연속 시간 초과)을 판정한다. 표는 core 가 만들고(:data:`DDL`)
다른 서비스는 넣기만 한다.

남기는 것은 식별자와 결과뿐이다. 입력 글 · 응답 · 파일 이름 · 질의 문자열 · 자격 증명 · 예외 메시지는 넣지 않는다.

넣기는 요청을 붙잡지 않는다. :func:`record_event` 는 메모리 대기열에 넣고 바로 돌아오고, 프로세스마다 쓰기
스레드 하나가 묶어서 넣는다. 그래서 DB 가 느리거나 끊겨도 요청 · LLM 호출 · 이벤트 루프 · 공용 실행기 스레드는
기다리지 않는다. 연결은 짧게(1초) 한 번만 기다리고, 대기열이 차면 새 이벤트를 버리고 경고를 남긴다. 알람의
근거라서 일부를 잃는 것이 요청을 늦추는 것보다 낫다.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("xgen-sdk.anomaly-telemetry")

#: 표 정의. core 만 실행한다(advisory lock 안에서). 다른 서비스는 이 표를 만들지 않는다.
DDL = """CREATE TABLE IF NOT EXISTS anomaly_runtime_events (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, service TEXT NOT NULL,
    user_id TEXT, workflow_id TEXT, target TEXT, provider TEXT, model TEXT,
    outcome TEXT NOT NULL, environment TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())"""

KINDS = frozenset({"input", "download", "llm"})
OUTCOMES = frozenset({"success", "timeout", "error", "cancelled"})

#: 대기열 상한. 넘치면 새 이벤트를 버린다.
QUEUE_MAX = 10_000
#: 한 번에 넣는 행 수.
BATCH_MAX = 200
#: 쓰기 스레드가 연결을 기다리는 시간(초). 공용 풀이 차 있으면 오래 기다리지 않고 그 묶음을 버린다.
CONNECT_TIMEOUT_S = 1.0
#: 실패한 묶음을 한 번 더 넣기 전에 쉬는 시간(초).
RETRY_DELAY_S = 1.0
#: 버린 이벤트 경고의 최소 간격(초).
_WARN_EVERY_S = 60.0

_INSERT = """INSERT INTO anomaly_runtime_events
    (id,kind,service,user_id,workflow_id,target,provider,model,outcome,environment)
    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING"""


def environment() -> str:
    return (os.getenv("XGEN_ENV") or os.getenv("ENVIRONMENT") or "default")[:80]


def _manager(app_db: Any) -> Any:
    return getattr(app_db, "config_db_manager", None) or app_db


def _connection(manager: Any):
    try:
        return manager.get_connection(timeout=CONNECT_TIMEOUT_S, auto_recover=False)
    except TypeError:  # 시험용 대역처럼 인자를 받지 않는 관리자
        return manager.get_connection()


def insert_rows(manager: Any, rows: List[Tuple]) -> None:
    """행들을 한 트랜잭션으로 넣는다(쓰기 스레드가 부른다)."""
    with _connection(manager) as conn, conn.transaction(), conn.cursor() as cur:
        cur.executemany(_INSERT, rows)


class _Writer:
    """프로세스마다 하나. 대기열을 비우며 묶어서 넣는 데몬 스레드."""

    def __init__(self) -> None:
        self._queue: "queue.Queue[Tuple[Any, Tuple]]" = queue.Queue(maxsize=QUEUE_MAX)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._pid = 0
        self.dropped = 0
        self._last_warn = 0.0

    def _ensure(self) -> None:
        if self._thread is not None and self._thread.is_alive() and self._pid == os.getpid():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and self._pid == os.getpid():
                return
            if self._pid != os.getpid():
                # fork 된 자식은 부모의 대기열 · 스레드를 이어받지 않는다.
                self._queue = queue.Queue(maxsize=QUEUE_MAX)
            self._pid = os.getpid()
            self._thread = threading.Thread(target=self._run, name="xgen-anomaly-telemetry", daemon=True)
            self._thread.start()

    def submit(self, manager: Any, row: Tuple) -> bool:
        self._ensure()
        try:
            self._queue.put_nowait((manager, row))
            return True
        except queue.Full:
            self.dropped += 1
            now = time.monotonic()
            if now - self._last_warn >= _WARN_EVERY_S:
                self._last_warn = now
                logger.warning("Anomaly telemetry queue full; dropped=%d", self.dropped)
            return False

    def _run(self) -> None:
        while True:
            items = [self._queue.get()]
            while len(items) < BATCH_MAX:
                try:
                    items.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            groups: Dict[int, Tuple[Any, List[Tuple]]] = {}
            for manager, row in items:
                groups.setdefault(id(manager), (manager, []))[1].append(row)
            for manager, rows in groups.values():
                self._write(manager, rows)
            for _ in items:
                self._queue.task_done()

    @staticmethod
    def _write(manager: Any, rows: List[Tuple]) -> None:
        for attempt in range(2):
            try:
                insert_rows(manager, rows)
                return
            except Exception:  # noqa: BLE001 - 적재 실패는 요청에 닿지 않는다
                if attempt == 0:
                    time.sleep(RETRY_DELAY_S)
        # DB 인자 · 원래 예외는 로그에 남기지 않는다(식별자가 섞일 수 있다).
        logger.error("Anomaly telemetry persistence failed: rows=%d", len(rows))

    def flush(self, timeout: float) -> bool:
        """대기열이 빌 때까지 기다린다(시험 · 종료용). 다 비었으면 True."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._queue.all_tasks_done.wait(left)
        return True


_writer = _Writer()


def record_event(
    app_db: Any,
    kind: str,
    *,
    service: str,
    user_id: Any = None,
    workflow_id: Any = None,
    target: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    outcome: str = "success",
    event_id: Optional[str] = None,
) -> bool:
    """이벤트 하나를 대기열에 넣는다. 넣었으면 True(DB 에 들어갔다는 뜻은 아니다).

    ``kind`` 가 입력 · 다운로드면 로그인한 사용자가 있어야 한다(없으면 넣지 않는다). 호출자는 서버가 정한
    식별자만 넘긴다. ``event_id`` 는 같은 일을 두 번 넣지 않게 하는 열쇠다(재시도 · 콜백 중복).
    """
    if kind not in KINDS or outcome not in OUTCOMES:
        raise ValueError("Invalid telemetry event")
    if kind != "llm" and (not user_id or str(user_id) in {"0", "None"}):
        return False
    if app_db is None:
        return False
    row = (
        str(event_id or uuid.uuid4()), kind, str(service)[:40],
        str(user_id)[:100] if user_id else None,
        str(workflow_id)[:200] if workflow_id else None,
        target, provider, model, outcome, environment(),
    )
    return _writer.submit(_manager(app_db), row)


def flush(timeout: float = 5.0) -> bool:
    """대기열에 든 이벤트를 다 넣을 때까지 기다린다(시험 · 종료용). 이벤트 루프에서 부르지 않는다."""
    return _writer.flush(timeout)


def dropped_count() -> int:
    """대기열이 차서 버린 이벤트 수(이 프로세스)."""
    return _writer.dropped


class DownloadTelemetryMiddleware:
    """인증된 첨부 응답을 끝까지 보낸 것을 다운로드 한 번으로 센다(저장소 읽기 횟수가 아니다).

    서비스 엔드포인트와 같은 게이트웨이 신원 헤더(``x-user-id``)를 쓴다. 권한 판정은 엔드포인트의 몫이다.
    200 이고 ``Content-Disposition: attachment`` 이며 본문을 끝까지 보낸 응답만 센다. HEAD · Range · 미리보기 ·
    오류 · 중간에 끊긴 응답은 세지 않는다. 디스크에 저장했다는 증명이 아니라 서버가 다 보냈다는 뜻이다.
    """

    def __init__(self, app, *, service: str):
        self.app, self.service = app, service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        actor = headers.get(b"x-user-id", b"").decode("ascii", errors="ignore")
        eligible = (scope.get("method") in {"GET", "POST"} and actor.isdigit() and int(actor) > 0
                    and b"range" not in headers)
        attachment, completed = False, False

        async def observe(message):
            nonlocal attachment, completed
            if message["type"] == "http.response.start":
                response_headers = dict(message.get("headers", []))
                attachment = (message["status"] == 200 and
                              response_headers.get(b"content-disposition", b"").lower().startswith(b"attachment"))
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True

        await self.app(scope, receive, observe)
        if eligible and attachment and completed and not scope.get("state", {}).get("xgen_webhook"):
            db = getattr(getattr(scope.get("app"), "state", None), "app_db", None)
            if db is not None:
                try:
                    record_event(db, "download", service=self.service, user_id=actor)
                except Exception:  # noqa: BLE001
                    logger.debug("download telemetry skipped", exc_info=True)


__all__ = [
    "BATCH_MAX",
    "CONNECT_TIMEOUT_S",
    "DDL",
    "DownloadTelemetryMiddleware",
    "KINDS",
    "OUTCOMES",
    "QUEUE_MAX",
    "dropped_count",
    "environment",
    "flush",
    "insert_rows",
    "record_event",
]
