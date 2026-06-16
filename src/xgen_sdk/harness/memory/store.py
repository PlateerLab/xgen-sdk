"""SessionStore — 세션 상태/진행 기록의 영속 통로 (provider-agnostic).

## 철학 (②연동성)
엔진은 특정 DB(Postgres/Redis/MinIO)에 결합되면 안 된다. SessionStore 는 **Protocol**
이고, 엔진은 인터페이스만 안다. 빌트인 2종(InMemory / File)은 무거운 의존성 0 으로
standalone 동작하고, 플랫폼은 entry_points `xgen_harness.session_stores` 로 자기
DB 백엔드를 코어 수정 없이 끼운다.

저장 페이로드는 임의 dict — 보통 `HarnessSession.to_dict()` (messages·cost·metadata,
metadata 안에 ProgressLog([[progress]]) 가능). 다음 세션이 load 로 복원해 백지 시작을 면한다.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger("harness.memory.store")


@runtime_checkable
class SessionStore(Protocol):
    """세션 영속 인터페이스. 구현체는 이 4 메서드만 만족하면 된다."""

    def save(self, session_id: str, data: dict[str, Any]) -> None: ...
    def load(self, session_id: str) -> Optional[dict[str, Any]]: ...
    def list_sessions(self) -> list[str]: ...
    def delete(self, session_id: str) -> bool: ...


class InMemorySessionStore:
    """프로세스 메모리 저장 — 테스트·단일 프로세스 기본값. 재시작 시 휘발."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    def save(self, session_id: str, data: dict[str, Any]) -> None:
        # 깊은 복사로 호출자 mutation 격리 (round-trip 안전).
        self._data[session_id] = json.loads(json.dumps(data, ensure_ascii=False))

    def load(self, session_id: str) -> Optional[dict[str, Any]]:
        v = self._data.get(session_id)
        return json.loads(json.dumps(v, ensure_ascii=False)) if v is not None else None

    def list_sessions(self) -> list[str]:
        return sorted(self._data.keys())

    def delete(self, session_id: str) -> bool:
        return self._data.pop(session_id, None) is not None


class FileSessionStore:
    """JSON 파일 저장 — standalone 산출물·로컬 멀티세션용. 무거운 의존성 0.

    `{root_dir}/{session_id}.json`. atomic write (tmp → os.replace) 로 부분쓰기 방지.
    session_id 는 파일명 안전 문자만 허용 (디렉터리 탈출 차단).
    """

    def __init__(self, root_dir: str) -> None:
        self._root = os.path.abspath(root_dir)
        os.makedirs(self._root, exist_ok=True)

    def _path(self, session_id: str) -> str:
        # 경로 구분자/상위참조는 조용히 정화하지 않고 거부 — id 충돌·traversal 동시 차단.
        if (not session_id) or ("/" in session_id) or ("\\" in session_id) or (".." in session_id):
            raise ValueError(f"안전하지 않은 session_id: {session_id!r}")
        safe = "".join(c for c in session_id if c.isalnum() or c in ("-", "_", "."))
        if not safe or safe in (".", ".."):
            raise ValueError(f"안전하지 않은 session_id: {session_id!r}")
        return os.path.join(self._root, f"{safe}.json")

    def save(self, session_id: str, data: dict[str, Any]) -> None:
        path = self._path(session_id)
        fd, tmp = tempfile.mkstemp(dir=self._root, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load(self, session_id: str) -> Optional[dict[str, Any]]:
        path = self._path(session_id)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def list_sessions(self) -> list[str]:
        if not os.path.isdir(self._root):
            return []
        return sorted(
            fn[:-5] for fn in os.listdir(self._root) if fn.endswith(".json")
        )

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        if os.path.exists(path):
            os.unlink(path)
            return True
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  레지스트리 + entry_points 발견
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_STORE_REGISTRY: dict[str, SessionStore] = {}
_DISCOVERY_DONE = False


def register_session_store(name: str, store: SessionStore) -> None:
    """SessionStore 인스턴스를 이름으로 등록 (런타임 주입).

    정식 확장은 entry_points `xgen_harness.session_stores` 권장.
    """
    if not isinstance(store, SessionStore):
        raise TypeError(f"register_session_store: {store!r} 는 SessionStore 프로토콜 미충족")
    _STORE_REGISTRY[name] = store


def _discover_once() -> None:
    global _DISCOVERY_DONE
    if _DISCOVERY_DONE:
        return
    _DISCOVERY_DONE = True
    # 빌트인 default — 외부가 "default" 를 override 하면 그게 우선.
    _STORE_REGISTRY.setdefault("memory", InMemorySessionStore())
    _STORE_REGISTRY.setdefault("default", _STORE_REGISTRY["memory"])
    try:
        from importlib.metadata import entry_points
        try:
            eps = entry_points(group="xgen_harness.session_stores")
        except TypeError:  # py3.9
            eps = entry_points().get("xgen_harness.session_stores", [])
    except Exception as e:  # pragma: no cover
        logger.debug("[session_stores] entry_points backend 없음: %s", e)
        return
    for ep in eps:
        try:
            factory = ep.load()
            store = factory() if callable(factory) else factory
            if isinstance(store, SessionStore):
                _STORE_REGISTRY[ep.name] = store
        except Exception as e:  # pragma: no cover
            logger.warning("[session_stores] %s 로드 실패: %s", ep, e)


def get_session_store(name: str = "default") -> SessionStore:
    """등록된 SessionStore 조회 (기본 'default' = InMemory)."""
    _discover_once()
    store = _STORE_REGISTRY.get(name)
    if store is None:
        raise KeyError(f"SessionStore 없음: {name!r} (available={available_session_stores()})")
    return store


def available_session_stores() -> list[str]:
    _discover_once()
    return sorted(_STORE_REGISTRY.keys())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HarnessSession ↔ store 편의 함수 (session.py 무수정)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_PROGRESS_KEY = "progress"


def save_session(store: SessionStore, session: Any) -> None:
    """HarnessSession 을 store 에 영속. (session.to_dict() 직렬화)"""
    store.save(session.session_id, session.to_dict())


def load_session(store: SessionStore, session_id: str, config: Any = None) -> Optional[Any]:
    """store 에서 HarnessSession 복원. 없으면 None."""
    data = store.load(session_id)
    if data is None:
        return None
    from ..core.session import HarnessSession  # lazy — 순환 import 회피
    session = HarnessSession.from_dict(data)
    if config is not None:
        session.state.config = config
    return session


def attach_progress(session: Any, log: Any) -> None:
    """ProgressLog 를 세션 metadata 에 실어 to_dict()/저장 시 함께 영속."""
    session.state.metadata[_PROGRESS_KEY] = log.to_dict()


def read_progress(session: Any) -> Any:
    """세션 metadata 에서 ProgressLog 복원. 없으면 빈 로그."""
    from .progress import ProgressLog
    raw = (getattr(session.state, "metadata", {}) or {}).get(_PROGRESS_KEY)
    return ProgressLog.from_dict(raw) if raw else ProgressLog()
