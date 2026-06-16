"""장기실행 메모리 — progress artifact + SessionStore 회귀 테스트.

세션 간 작업상태 인계(progress-artifact)와 영속 store 라운드트립을 고정한다.
모두 standalone (무거운 의존성 0) — file store 는 tmp_path 로 검증.
"""

import pytest

from xgen_sdk.harness import (
    ProgressStatus,
    ProgressItem,
    ProgressLog,
    InMemorySessionStore,
    FileSessionStore,
    register_session_store,
    get_session_store,
    available_session_stores,
    SessionStore,
)


# ── ProgressLog ──

def _log():
    log = ProgressLog()
    log.add(ProgressItem(id="feat-1", description="login", verification="curl /login"))
    log.add(ProgressItem(id="feat-2", description="logout"))
    return log


def test_add_and_get():
    log = _log()
    assert log.get("feat-1").description == "login"
    assert log.get("missing") is None


def test_duplicate_id_rejected():
    log = _log()
    with pytest.raises(ValueError):
        log.add(ProgressItem(id="feat-1"))


def test_update_status_and_pending():
    log = _log()
    log.update_status("feat-1", ProgressStatus.DONE)
    pending_ids = [i.id for i in log.pending()]
    assert pending_ids == ["feat-2"]
    assert log.next_pending().id == "feat-2"


def test_update_unknown_raises():
    with pytest.raises(KeyError):
        _log().update_status("nope", ProgressStatus.DONE)


def test_is_complete_gating():
    log = _log()
    assert log.is_complete() is False
    log.update_status("feat-1", ProgressStatus.DONE)
    log.update_status("feat-2", ProgressStatus.DONE)
    assert log.is_complete() is True


def test_summary_counts():
    log = _log()
    log.update_status("feat-1", ProgressStatus.DONE)
    s = log.summary()
    assert s["total"] == 2
    assert s["done"] == 1
    assert s["pending"] == 1


def test_progress_json_roundtrip():
    log = _log()
    log.update_status("feat-1", ProgressStatus.FAILED, notes="500 on submit")
    restored = ProgressLog.from_json(log.to_json())
    assert restored.get("feat-1").status is ProgressStatus.FAILED
    assert restored.get("feat-1").notes == "500 on submit"
    assert restored.get("feat-1").verification == "curl /login"


# ── SessionStore (InMemory) ──

def test_inmemory_roundtrip():
    store = InMemorySessionStore()
    store.save("s1", {"messages": [1, 2], "meta": {"k": "v"}})
    assert store.load("s1") == {"messages": [1, 2], "meta": {"k": "v"}}
    assert store.list_sessions() == ["s1"]
    assert store.delete("s1") is True
    assert store.load("s1") is None


def test_inmemory_isolation():
    # 저장 후 원본 mutation 이 저장본을 오염시키지 않아야 한다.
    store = InMemorySessionStore()
    data = {"x": [1]}
    store.save("s1", data)
    data["x"].append(2)
    assert store.load("s1") == {"x": [1]}


def test_inmemory_satisfies_protocol():
    assert isinstance(InMemorySessionStore(), SessionStore)


# ── SessionStore (File) ──

def test_file_store_roundtrip(tmp_path):
    store = FileSessionStore(str(tmp_path / "sessions"))
    store.save("sess-abc", {"turn_count": 3})
    assert store.load("sess-abc") == {"turn_count": 3}
    assert "sess-abc" in store.list_sessions()
    assert store.delete("sess-abc") is True
    assert store.load("sess-abc") is None


def test_file_store_rejects_path_traversal(tmp_path):
    store = FileSessionStore(str(tmp_path))
    with pytest.raises(ValueError):
        store.save("../../etc/passwd", {"x": 1})


def test_file_store_missing_is_none(tmp_path):
    assert FileSessionStore(str(tmp_path)).load("never") is None


# ── 레지스트리 ──

def test_default_store_is_memory():
    assert "default" in available_session_stores()
    assert "memory" in available_session_stores()
    assert isinstance(get_session_store("default"), SessionStore)


def test_register_custom_store():
    s = InMemorySessionStore()
    register_session_store("custom_test", s)
    assert get_session_store("custom_test") is s


def test_get_unknown_store_raises():
    with pytest.raises(KeyError):
        get_session_store("does-not-exist")


# ── 세션 간 인계 (장기실행 플로우의 핵심) ──

def test_cross_session_handoff(tmp_path):
    """세션 A 가 진행기록을 남기고 종료 → 세션 B 가 store 에서 복원해 이어받는다."""
    from xgen_sdk.harness import (
        HarnessSession,
        save_session,
        load_session,
        attach_progress,
        read_progress,
    )

    store = FileSessionStore(str(tmp_path / "sess"))

    # 세션 A — 작업 시작, feat-1 완료, feat-2 미완 기록 후 영속.
    sess_a = HarnessSession(session_id="run-001")
    log = ProgressLog()
    log.add(ProgressItem(id="feat-1", description="login", verification="curl /login"))
    log.add(ProgressItem(id="feat-2", description="logout"))
    log.update_status("feat-1", ProgressStatus.DONE)
    attach_progress(sess_a, log)
    save_session(store, sess_a)

    # 세션 B — 백지로 시작했지만 store 에서 복원 → 다음 할 일을 즉시 안다.
    sess_b = load_session(store, "run-001")
    assert sess_b is not None
    assert sess_b.session_id == "run-001"
    restored = read_progress(sess_b)
    assert restored.next_pending().id == "feat-2"
    assert restored.get("feat-1").status is ProgressStatus.DONE


def test_read_progress_empty_when_none():
    from xgen_sdk.harness import HarnessSession, read_progress
    log = read_progress(HarnessSession(session_id="fresh"))
    assert log.items == []
