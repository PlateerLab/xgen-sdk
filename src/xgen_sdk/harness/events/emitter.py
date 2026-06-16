"""
EventEmitter — asyncio.Queue 기반 이벤트 발행 시스템

파이프라인 실행 중 이벤트를 발행하면 SSE 스트리밍 루프에서 비동기로 소비.

v0.11.24 — audit 지적 해소:
  - subscribe() 가 unsubscribe 토큰을 반환하여 long-running 누수 방지
  - _queue._queue (private 접근) 제거 → _last_event 로 추적
  - 콜백 예외는 logger.exception 으로 트레이스까지 기록 (삼킴 방지)
"""

import asyncio
import logging
from typing import AsyncGenerator, Awaitable, Callable, Optional

from .types import DoneEvent, HarnessEvent

logger = logging.getLogger("harness.events")


class EventEmitter:
    """비동기 큐 기반 이벤트 발행기.

    사용:
        emitter = EventEmitter()
        unsubscribe = emitter.subscribe(my_handler)
        ...
        unsubscribe()   # 또는 emitter.unsubscribe(unsubscribe)
    """

    def __init__(self, queue_size: int = 8000):
        # v0.26.0 — queue_size 1000 → 8000 (B7 fix).
        # 라이브 production 에서 `Event queue full, dropping event: MessageEvent` 가
        # 분당 ~75건 발생 (긴 응답 시 SSE 컨슈머가 못 따라가 message.delta drop).
        # 8K 로 올리면 일반 SSE 흐름은 문제 없음. 그래도 leak 가능성은 _drop_count 로 추적.
        self._queue: asyncio.Queue[HarnessEvent] = asyncio.Queue(maxsize=queue_size)
        self._subscribers: dict[int, Callable[[HarnessEvent], Awaitable[None]]] = {}
        self._next_sub_id: int = 0
        self._closed = False
        self._last_event: Optional[HarnessEvent] = None
        # v0.26.0 — drop 누적 카운터. 운영 진단/SSE 백프레셔 알림 용.
        self._drop_count: int = 0

    async def emit(self, event: HarnessEvent) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._drop_count += 1
            # 매번 warning 찍으면 로그 폭주 → 첫 1회 + 100회마다 1회만 노출.
            if self._drop_count == 1 or self._drop_count % 100 == 0:
                logger.warning(
                    "Event queue full, dropping event: %s (cumulative=%d, queue_size=%d)",
                    type(event).__name__, self._drop_count, self._queue.maxsize,
                )
            return
        self._last_event = event

        # 구독자 콜백 — snapshot 으로 순회 (콜백 중 subscribe/unsubscribe 경쟁 대비).
        for cb in list(self._subscribers.values()):
            try:
                await cb(event)
            except Exception:
                logger.exception("Subscriber callback raised (event=%s)", type(event).__name__)

    def subscribe(
        self,
        callback: Callable[[HarnessEvent], Awaitable[None]],
    ) -> Callable[[], None]:
        """구독자 등록. 반환된 callable 을 호출하면 unsubscribe 된다.

        long-running 프로세스에서 이 토큰을 버리면 콜백이 계속 참조되어 leak 발생.
        명시적 unsubscribe 가 필수다.
        """
        sub_id = self._next_sub_id
        self._next_sub_id += 1
        self._subscribers[sub_id] = callback

        def _unsubscribe() -> None:
            self._subscribers.pop(sub_id, None)

        return _unsubscribe

    def unsubscribe(self, token: Callable[[], None]) -> None:
        """subscribe() 가 반환한 token 호출 — 명시 alias."""
        token()

    def clear_subscribers(self) -> None:
        """모든 구독자 해제 (세션 종료 시 안전장치)."""
        self._subscribers.clear()

    async def stream(self) -> AsyncGenerator[HarnessEvent, None]:
        """이벤트를 비동기 제너레이터로 소비. DoneEvent가 오면 종료."""
        while True:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=60.0)
            except asyncio.TimeoutError:
                continue
            yield event
            if isinstance(event, DoneEvent):
                break

    async def close(self) -> None:
        self._closed = True
        # 마지막 이벤트가 DoneEvent 가 아니면 종료 이벤트 강제 발행. _closed=True 상태에서는
        # emit() 이 no-op 이므로 큐에 직접 넣는다. asyncio.Queue 의 private _queue 접근은
        # 사용하지 않는다 (v0.11.24).
        if not isinstance(self._last_event, DoneEvent):
            done = DoneEvent(final_output="", success=False)
            try:
                self._queue.put_nowait(done)
                self._last_event = done
            except asyncio.QueueFull:
                logger.warning("close(): queue full, cannot enqueue terminal DoneEvent")
        self.clear_subscribers()
