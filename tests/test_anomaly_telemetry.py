"""시스템 알람 이벤트 적재(xgen_sdk.anomaly).

지키는 것 셋:
- 기록은 요청을 붙잡지 않는다: DB 가 느려도 ``record_event`` 는 바로 돌아오고, 쓰기 스레드가 묶어서 넣는다.
- 에이전트 런타임의 시간 상한(``asyncio.wait_for``)이 끊은 LLM 호출은 ``timeout`` 이다. 사람이 멈춘 호출은
  ``cancelled`` 다. 둘 다 안쪽에서는 ``CancelledError`` 로 보인다.
- LangChain 콜백은 실행기 스레드를 거치지 않는다(``run_inline``), 붙이지 못해도 모델은 그대로다.
"""
from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager

import pytest

from xgen_sdk.anomaly import llm, telemetry


class _Cursor:
    def __init__(self, sink):
        self.sink = sink

    def executemany(self, sql, rows):
        self.sink.extend(rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, sink):
        self.sink = sink

    @contextmanager
    def transaction(self):
        yield

    def cursor(self):
        return _Cursor(self.sink)


class _Manager:
    def __init__(self, delay=0.0):
        self.rows = []
        self.delay = delay
        self.calls = []

    @contextmanager
    def get_connection(self, timeout=None, auto_recover=True):
        self.calls.append((timeout, auto_recover))
        if self.delay:
            time.sleep(self.delay)
        yield _Conn(self.rows)


def test_record_event_queues_and_the_writer_inserts_in_batches():
    db = _Manager()
    assert telemetry.record_event(db, "input", service="workflow", user_id=7, workflow_id="wf") is True
    assert telemetry.record_event(db, "llm", service="workflow", target="t", provider="openai", model="m",
                                  outcome="timeout", event_id="e1") is True
    assert telemetry.record_event(db, "download", service="core", user_id=0) is False, "로그인 사용자 없는 입력·다운로드는 넣지 않는다"
    with pytest.raises(ValueError):
        telemetry.record_event(db, "chat", service="workflow", user_id=1)
    assert telemetry.flush(5)
    kinds = [(r[1], r[2], r[3], r[8]) for r in db.rows]
    assert ("input", "workflow", "7", "success") in kinds and ("llm", "workflow", None, "timeout") in kinds
    assert [r[0] for r in db.rows if r[1] == "llm"] == ["e1"]
    assert db.calls[0] == (telemetry.CONNECT_TIMEOUT_S, False), "연결은 짧게 한 번만 기다리고 풀 복구를 부르지 않는다"


def test_a_slow_database_never_blocks_the_caller():
    db = _Manager(delay=1.0)
    started = time.monotonic()
    for i in range(50):
        telemetry.record_event(db, "llm", service="workflow", target="t", provider="p", model="m", event_id=f"s{i}")
    assert time.monotonic() - started < 0.2, "넣기는 대기열에만 한다"
    assert telemetry.flush(10)
    assert len([r for r in db.rows if r[0].startswith("s")]) == 50


def test_a_full_queue_drops_new_events_instead_of_waiting(monkeypatch):
    writer = telemetry._Writer()
    monkeypatch.setattr(writer, "_ensure", lambda: None)  # 쓰기 스레드 없이 대기열만
    writer._queue = __import__("queue").Queue(maxsize=2)
    assert writer.submit(object(), ("a",)) and writer.submit(object(), ("b",))
    assert writer.submit(object(), ("c",)) is False and writer.dropped == 1


def _llm_rows(db):
    assert telemetry.flush(5)
    return [r[8] for r in db.rows if r[1] == "llm"]


def test_a_runtime_timeout_is_timeout_and_a_user_stop_is_cancelled():
    db = _Manager()
    llm.configure(db, service="workflow")

    async def slow(**_kw):
        await asyncio.sleep(5)
        return "late"

    async def fast(**_kw):
        return "ok"

    async def boom(**_kw):
        raise RuntimeError("bad request")

    wrapped_slow = llm.wrap_method(slow, "openai")

    async def scenario():
        # 1) 런타임 시간 상한: wait_for 가 안쪽을 취소하고 TimeoutError 로 바꾼다.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(wrapped_slow(model="m"), timeout=0.05)
        await asyncio.sleep(0)
        # 2) 사람이 멈춘다: 바깥에서 작업을 취소한다.
        task = asyncio.create_task(wrapped_slow(model="m"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        # 3) 성공 4) 오류
        assert await llm.wrap_method(fast, "openai")(model="m") == "ok"
        with pytest.raises(RuntimeError):
            await llm.wrap_method(boom, "openai")(model="m")

    before = len(db.rows)
    asyncio.run(scenario())
    assert _llm_rows(db)[before:] == ["timeout", "cancelled", "success", "error"]


def test_a_stream_cut_by_the_chunk_watchdog_is_timeout():
    db = _Manager()
    llm.configure(db, service="workflow")

    async def stream(**_kw):
        yield "first"
        await asyncio.sleep(5)
        yield "never"

    async def scenario():
        iterator = llm.wrap_method(stream, "anthropic")(model="m").__aiter__()
        assert await asyncio.wait_for(iterator.__anext__(), timeout=1) == "first"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(iterator.__anext__(), timeout=0.05)
        await iterator.aclose()
        await asyncio.sleep(0)

    before = len(db.rows)
    asyncio.run(scenario())
    assert _llm_rows(db)[before:] == ["timeout"]


def test_cli_providers_and_nested_calls_are_not_counted():
    db = _Manager()
    llm.configure(db, service="workflow")

    async def inner(**_kw):
        return "x"

    async def outer(**_kw):
        return await llm.wrap_method(inner, "openai")(model="m")

    async def scenario():
        await llm.wrap_method(inner, "codex")(model="m")
        await llm.wrap_method(outer, "openai")(model="m")

    before = len(db.rows)
    asyncio.run(scenario())
    assert _llm_rows(db)[before:] == ["success"], "CLI 는 세지 않고, 감싼 호출 안의 호출은 한 번만 센다"


def test_the_target_hides_credentials_and_keeps_routing():
    a = llm.target_key("bedrock", "m", "https://u:secret@h/x?key=1", {"aws_region": "us-east-1", "api_key": "k"})
    b = llm.target_key("bedrock", "m", "https://h/x", {"aws_region": "us-east-1"})
    c = llm.target_key("bedrock", "m", "https://h/x", {"aws_region": "ap-northeast-2"})
    assert a == b and b != c


def test_langchain_callbacks_run_inline_and_attach_never_breaks_the_model():
    pytest.importorskip("langchain_core")

    class Model:
        callbacks = None

    model = llm.attach_langchain(Model(), provider="openai", model="m")
    [callback] = model.callbacks
    assert callback.run_inline is True
    llm.attach_langchain(model, provider="openai", model="m")
    assert len(model.callbacks) == 1, "두 번 붙이지 않는다"

    class Frozen:
        def __setattr__(self, name, value):
            raise AttributeError("read-only")

    frozen = Frozen()
    assert llm.attach_langchain(frozen, provider="openai", model="m") is frozen
