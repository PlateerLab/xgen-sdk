"""이벤트 루프는 작업 스레드가 공용 DB 풀을 다 써도 커넥션을 기다리지 않는다.

2026-09-23 안정성 감사 F9.

무엇이 있었나
-------------
공용 풀(기본 10)을 실행 풀·기본 executor·하위 워크플로 풀·백그라운드 루프가 함께 쓴다.
느린 쿼리가 몰려 풀이 차면 이벤트 루프 위의 **동기** DB 호출이 커넥션을 최대 30초
기다렸다 — 그동안 파드의 모든 요청과 헬스체크가 섰다.

고친 뒤
-------
루프 스레드(서버 루프를 돌리는 메인 스레드)는 작은 예비 풀에서 짧게만 기다린다.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading

import pytest

from xgen_sdk.db import pool_manager as PM


class PoolTimeout(Exception):
    pass


class _FakePool:
    """psycopg_pool.ConnectionPool 의 이 모듈이 쓰는 만큼 — 크기 제한과 대기 상한."""

    made: list = []

    def __init__(self, conninfo=None, *, min_size=0, max_size=1, timeout=30.0, name="",
                 fail=False, **_kw):
        if _FakePool.fail_next:
            _FakePool.fail_next = False
            raise RuntimeError("cannot open")
        self.name = name
        self.max_size = max_size
        self.timeout_default = timeout
        self._sem = threading.BoundedSemaphore(max_size)
        self.closed = False
        self.waits: list = []
        self.drained = 0
        _FakePool.made.append(self)

    fail_next = False

    @contextlib.contextmanager
    def connection(self, timeout=None):
        self.waits.append(timeout)
        if not self._sem.acquire(timeout=timeout):
            raise PoolTimeout(f"{self.name}: no connection in {timeout}s")
        try:
            yield object()
        finally:
            self._sem.release()

    def wait(self, timeout=None):
        return None

    def close(self):
        self.closed = True

    def drain(self):
        self.drained += 1

    def get_stats(self):
        return {"pool_size": self.max_size, "pool_available": self.max_size, "requests_waiting": 0}


@pytest.fixture
def manager(monkeypatch):
    _FakePool.made = []
    _FakePool.fail_next = False
    monkeypatch.setattr(PM, "ConnectionPool", _FakePool)
    m = PM.DatabaseManagerPsycopg3(max_size=2, timeout=30.0)
    m.db_type = "postgresql"
    monkeypatch.setattr(m, "_build_conninfo", lambda: "postgresql://fake")
    assert m._connect_postgresql_pool()
    return m


def _run_on_loop(fn):
    """메인 스레드의 이벤트 루프 **위에서** 동기 함수를 부른다 — 루프 위 동기 DB 호출 그대로."""
    async def _main():
        return fn()
    return asyncio.run(_main())


def _hold_shared_pool(m, n):
    """작업 스레드들이 공용 풀 n 개를 쥐고 놓지 않는다."""
    release = threading.Event()
    held = threading.Barrier(n + 1)

    def _worker():
        with m.get_connection():
            held.wait()
            release.wait(5)

    threads = [threading.Thread(target=_worker, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    held.wait(5)
    return release, threads


class TestWhoIsTheLoop:
    def test_the_main_thread_inside_the_server_loop(self):
        assert _run_on_loop(PM._on_event_loop_thread) is True

    def test_the_main_thread_outside_a_loop(self):
        assert PM._on_event_loop_thread() is False

    def test_a_worker_thread_with_its_own_loop_is_not_the_server_loop(self):
        """턴의 전용 루프(풀 스레드)는 기다려도 그 턴만 늦는다 — 예비를 쓰지 않는다."""
        seen = {}

        def _thread():
            seen["v"] = asyncio.run(_async_check())

        async def _async_check():
            return PM._on_event_loop_thread()

        t = threading.Thread(target=_thread)
        t.start()
        t.join(5)
        assert seen["v"] is False


class TestTheReserve:
    def test_there_are_two_pools(self, manager):
        names = [p.name for p in _FakePool.made]
        assert names == ["plateerag-db-pool", "plateerag-db-loop-pool"]
        assert manager._loop_pool.max_size == 2

    def test_the_loop_does_not_wait_behind_worker_threads(self, manager):
        """공용 풀이 작업 스레드로 꽉 찼다 — 루프 위의 동기 호출은 그래도 곧바로 커넥션을 얻는다."""
        release, threads = _hold_shared_pool(manager, 2)
        try:
            def _query():
                with manager.get_connection() as conn:
                    return conn is not None
            assert _run_on_loop(_query) is True
        finally:
            release.set()
            for t in threads:
                t.join(5)
        assert manager._stats["loop_acquisitions"] >= 1

    def test_the_loop_waits_briefly_at_most(self, manager):
        manager.loop_timeout = 5.0

        def _query():
            with manager.get_connection(timeout=30):
                pass
        _run_on_loop(_query)
        assert manager._loop_pool.waits[-1] == 5.0

    def test_worker_threads_use_the_shared_pool_with_the_normal_wait(self, manager):
        with manager.get_connection():
            pass
        assert manager._pool.waits[-1] == 30.0
        assert manager._loop_pool.waits == []

    def test_an_exhausted_reserve_fails_fast_instead_of_freezing(self, manager):
        manager.loop_timeout = 0.05
        taken = []

        def _nested():
            # 루프 스레드가 예비 둘을 다 쥔 채 또 달라고 한다(드문 중첩) — 짧게 기다리고 실패.
            with manager.get_connection(), manager.get_connection():
                with pytest.raises(PoolTimeout):
                    with manager.get_connection():
                        taken.append(1)
        _run_on_loop(_nested)
        assert taken == []

    def test_health_check_on_the_loop_uses_the_reserve(self, manager):
        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, q):
                return None

        class _Conn:
            def cursor(self):
                return _Cur()

        @contextlib.contextmanager
        def _conn(timeout=None):
            manager._loop_pool.waits.append(timeout)
            yield _Conn()

        manager._loop_pool.connection = _conn
        manager._is_pool_healthy = lambda: True
        _run_on_loop(lambda: manager.health_check(auto_recover=False))
        assert manager._loop_pool.waits, "헬스체크가 루프 위에서 공용 풀을 기다렸다"


class TestLifecycle:
    def test_reserve_zero_means_the_old_behaviour(self, monkeypatch):
        _FakePool.made = []
        monkeypatch.setattr(PM, "ConnectionPool", _FakePool)
        m = PM.DatabaseManagerPsycopg3(max_size=2)
        m.loop_reserve = 0
        m.db_type = "postgresql"
        monkeypatch.setattr(m, "_build_conninfo", lambda: "postgresql://fake")
        m._connect_postgresql_pool()
        assert m._loop_pool is None
        _run_on_loop(lambda: m.get_connection().__enter__())
        assert m._pool.waits

    def test_a_reserve_that_cannot_open_falls_back_to_the_shared_pool(self, monkeypatch):
        _FakePool.made = []
        monkeypatch.setattr(PM, "ConnectionPool", _FakePool)
        m = PM.DatabaseManagerPsycopg3(max_size=2)
        m.db_type = "postgresql"
        monkeypatch.setattr(m, "_build_conninfo", lambda: "postgresql://fake")
        original = m._open_loop_pool

        def _open(conninfo):
            _FakePool.fail_next = True
            original(conninfo)

        monkeypatch.setattr(m, "_open_loop_pool", _open)
        assert m._connect_postgresql_pool() is True
        assert m._loop_pool is None

    def test_disconnect_closes_both(self, manager):
        shared, reserve = manager._pool, manager._loop_pool
        manager.disconnect()
        assert shared.closed and reserve.closed and manager._loop_pool is None

    def test_recovery_reopens_both(self, manager, monkeypatch):
        old_reserve = manager._loop_pool
        monkeypatch.setattr(manager, "_is_pool_healthy", lambda: False)
        assert manager._try_recover_connection()
        assert old_reserve.closed and manager._loop_pool is not None
        assert manager._loop_pool is not old_reserve

    def test_reconnect_drains_both(self, manager):
        manager.reconnect()
        assert manager._pool.drained == 1 and manager._loop_pool.drained == 1

    def test_stats_show_the_reserve(self, manager):
        stats = manager.get_pool_stats()
        assert stats["loop_pool_max_size"] == 2 and "loop_acquisitions" in stats
