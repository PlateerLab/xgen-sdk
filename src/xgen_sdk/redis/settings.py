"""Redis 접속 설정 — **한 곳에서만 해석한다.**

이 모듈이 생긴 이유 (2026-08-06 감사):

    ``service/redis_sentinel.py`` 가 xgen-core · xgen-workflow · xgen-documents
    **세 저장소에 복사본**으로 존재했고, 도입 7일 만에 갈라졌다(core 사본에는
    async 변형이 아예 없고, socket timeout 이 2초 대 5초로 달랐다). Sentinel
    마스터 탐색은 페일오버 때 "어느 노드에 붙을지"를 정하는 로직이라 서비스마다
    다르면 장애가 서비스마다 다르게 난다.

    더 나빴던 것은 **SDK 자신이 Sentinel 을 몰랐다**는 점이다. 서비스의 클라이언트는
    Sentinel 로 현재 마스터를 따라가는데 SDK 의 config 매니저는 ``REDIS_HOST`` 로
    직결했다 — 페일오버 뒤 SDK 만 강등된 옛 마스터에 붙어 **쓰기가 read-only 로
    실패**한다.

설계 원칙

  * **인프라를 바꾸지 않는다.** 이미 배포된 환경변수 이름을 그대로 읽는다
    (게이트웨이(Rust)가 읽는 이름과도 일치한다: REDIS_SENTINEL_HOST /
    REDIS_SENTINEL_PORT / REDIS_SENTINEL_MASTER / REDIS_PASSWORD).
  * **환경변수는 기본값일 뿐 강제가 아니다.** 호출처가 명시적으로 값을 줄 수
    있어야 한다 — 스케줄러가 db 를 따로 쓰거나, 구독자가 타임아웃을 끄는 등
    실제 요구가 제각각이다.
  * **추측하지 않는다.** 예전 SDK 는 host 기본값이 사내 IP(192.168.2.242),
    password 기본값이 실제 비밀번호 문자열이었다. 설정이 빠지면 조용히 엉뚱한
    서버에 붙는 것보다 **연결하지 않는 편**이 낫다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

__all__ = ["RedisSettings", "SENTINEL_ENV", "DIRECT_ENV"]

#: Sentinel 을 켜는 데 필요한 환경변수 (게이트웨이와 동일한 이름).
SENTINEL_ENV = ("REDIS_SENTINEL_HOST", "REDIS_SENTINEL_PORT", "REDIS_SENTINEL_MASTER")

#: 직결 접속에 쓰는 환경변수.
DIRECT_ENV = ("REDIS_URL", "REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD", "REDIS_DB")

_DEFAULT_SENTINEL_PORT = 26379
_DEFAULT_PORT = 6379


def _int(raw: Optional[str], fallback):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback


def _float_or_none(raw: Optional[str], fallback: Optional[float]) -> Optional[float]:
    if raw is None or str(raw).strip() == "":
        return fallback
    try:
        return float(raw)
    except (TypeError, ValueError):
        return fallback


def _parse_sentinel_hosts(raw: str, default_port: int) -> List[Tuple[str, int]]:
    """``"a:26379, b, c:26380"`` → ``[("a",26379),("b",<default>),("c",26380)]``."""
    out: List[Tuple[str, int]] = []
    for part in str(raw or "").split(","):
        item = part.strip()
        if not item:
            continue
        if ":" in item:
            host, _, port = item.rpartition(":")
            out.append((host.strip(), _int(port, default_port)))
        else:
            out.append((item, default_port))
    return out


@dataclass(frozen=True)
class RedisSettings:
    """어디에 어떻게 붙을지. 불변이며, ``with_overrides`` 로 파생한다.

    ``from_env()`` 로 배포 환경을 읽고, 호출처가 필요한 것만 덮어쓴다::

        settings = RedisSettings.from_env().with_overrides(db=3, decode_responses=True)
    """

    # ── 접속 대상 ────────────────────────────────────────────────
    host: Optional[str] = None
    port: int = _DEFAULT_PORT
    password: Optional[str] = None
    #: ``None`` = **지정하지 않음**. URL 에 db 가 들어 있으면 그것을 따르고,
    #: 없으면 redis-py 기본(0). 값을 주면 **URL 보다 우선**한다 — 스케줄러·
    #: 세션·보안접근이 서로 다른 db 를 쓰므로 여기서 밀리면 전부 한 db 에
    #: 몰려 키가 섞인다.
    db: Optional[int] = None
    #: 있으면 host/port/password 보다 **우선**한다 (redis://, rediss://).
    url: Optional[str] = None

    # ── Sentinel ────────────────────────────────────────────────
    sentinel_hosts: Tuple[Tuple[str, int], ...] = ()
    sentinel_master: Optional[str] = None
    #: Sentinel 노드 자체의 비밀번호. 보통 데이터 노드와 같지만 다를 수 있다.
    sentinel_password: Optional[str] = None

    # ── 동작 ────────────────────────────────────────────────────
    decode_responses: bool = False
    #: ``None`` = 무한 대기. 블로킹 구독(pub/sub)에는 반드시 None 이어야 한다 —
    #: 타임아웃이 걸리면 조용한 메시지 유실이 아니라 주기적 예외로 나타난다.
    socket_timeout: Optional[float] = 5.0
    socket_connect_timeout: Optional[float] = 5.0
    #: 끊긴 소켓을 붙들고 있지 않도록 (컨테이너 재시작·페일오버 뒤 stale 방지).
    health_check_interval: float = 30.0
    retry_on_timeout: bool = True
    #: 그 밖에 redis-py 로 그대로 넘길 인자.
    extra: Dict[str, Any] = field(default_factory=dict)

    # ── 판정 ────────────────────────────────────────────────────

    @property
    def use_sentinel(self) -> bool:
        """Sentinel 로 마스터를 찾을 것인가.

        호스트 목록과 마스터 이름이 **둘 다** 있어야 한다. 하나만 있으면
        설정 실수이므로 직결로 폴백한다(조용히 아무 데도 못 붙는 것보다 낫다).
        """
        return bool(self.sentinel_hosts and self.sentinel_master)

    @property
    def configured(self) -> bool:
        """접속에 필요한 최소 정보가 있는가.

        없으면 **연결을 시도하지 않는다** — 기본값을 지어내 엉뚱한 서버에 붙는
        사고를 구조로 막는다.
        """
        return bool(self.use_sentinel or self.url or self.host)

    def describe(self) -> str:
        """로그용 한 줄. **비밀번호는 절대 넣지 않는다.**"""
        if self.use_sentinel:
            hosts = ",".join(f"{h}:{p}" for h, p in self.sentinel_hosts)
            return f"sentinel[{hosts}] master={self.sentinel_master} db={self.db if self.db is not None else 0}"
        if self.url:
            parsed = urlparse(self.url)
            return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or _DEFAULT_PORT} db={self.db if self.db is not None else '(URL)'}"
        return f"{self.host}:{self.port} db={self.db if self.db is not None else 0}"

    def url_with_db(self) -> Optional[str]:
        """URL 에 **명시한 db 를 반영**해 돌려준다.

        ⚠ ``redis.Redis.from_url(url, db=3)`` 은 db 를 **무시하고 URL 의 db 를
        쓴다**(실행 확인). 그래서 여기서 URL 자체를 고쳐야 한다 — 안 그러면
        서로 다른 db 를 쓰는 호출처(스케줄러·세션·보안접근)가 전부 URL 의 db
        하나로 몰려 키가 섞인다.
        """
        if not self.url or self.db is None:
            return self.url
        parsed = urlparse(self.url)
        return parsed._replace(path=f"/{self.db}").geturl()

    def with_overrides(self, **kwargs: Any) -> "RedisSettings":
        """일부만 바꾼 새 설정. ``None`` 을 명시적으로 주면 그대로 반영된다
        (구독자가 ``socket_timeout=None`` 으로 무한 대기를 고르는 경우)."""
        if "sentinel_hosts" in kwargs and kwargs["sentinel_hosts"] is not None:
            kwargs["sentinel_hosts"] = tuple(kwargs["sentinel_hosts"])
        if "extra" in kwargs and kwargs["extra"]:
            kwargs["extra"] = {**self.extra, **kwargs["extra"]}
        return replace(self, **kwargs)

    # ── 생성 ────────────────────────────────────────────────────

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None, **overrides: Any) -> "RedisSettings":
        """배포 환경변수에서 읽는다 — **인프라 수정 없이** 기존 이름 그대로.

        읽는 것:
          REDIS_SENTINEL_HOST / REDIS_SENTINEL_PORT / REDIS_SENTINEL_MASTER
          REDIS_URL / REDIS_HOST / REDIS_PORT / REDIS_PASSWORD / REDIS_DB
          REDIS_SOCKET_TIMEOUT / REDIS_CONNECT_TIMEOUT
        """
        e = os.environ if env is None else env
        sentinel_port = _int(e.get("REDIS_SENTINEL_PORT"), _DEFAULT_SENTINEL_PORT)
        hosts = _parse_sentinel_hosts(e.get("REDIS_SENTINEL_HOST", ""), sentinel_port)
        master = (e.get("REDIS_SENTINEL_MASTER") or "").strip() or None
        password = e.get("REDIS_PASSWORD") or None

        base = cls(
            host=(e.get("REDIS_HOST") or "").strip() or None,
            port=_int(e.get("REDIS_PORT"), _DEFAULT_PORT),
            password=password,
            db=_int(e.get("REDIS_DB"), None) if e.get("REDIS_DB") else None,
            url=(e.get("REDIS_URL") or "").strip() or None,
            sentinel_hosts=tuple(hosts),
            sentinel_master=master,
            sentinel_password=e.get("REDIS_SENTINEL_PASSWORD") or password,
            socket_timeout=_float_or_none(e.get("REDIS_SOCKET_TIMEOUT"), 5.0),
            socket_connect_timeout=_float_or_none(e.get("REDIS_CONNECT_TIMEOUT"), 5.0),
        )
        return base.with_overrides(**overrides) if overrides else base

    # ── redis-py 인자 ───────────────────────────────────────────

    def client_kwargs(self) -> Dict[str, Any]:
        """직결/Sentinel 공통으로 redis-py 에 넘길 인자."""
        kwargs: Dict[str, Any] = {
            "decode_responses": self.decode_responses,
            "socket_timeout": self.socket_timeout,
            "socket_connect_timeout": self.socket_connect_timeout,
            "retry_on_timeout": self.retry_on_timeout,
        }
        # health_check_interval=0 은 비활성 의미라 그대로 넘긴다.
        kwargs["health_check_interval"] = self.health_check_interval
        # db 를 지정하지 않았으면 **넘기지 않는다** — URL 안의 db 를 존중한다.
        if self.db is not None:
            kwargs["db"] = self.db
        if self.password:
            kwargs["password"] = self.password
        kwargs.update(self.extra)
        return kwargs
