"""
Redis Config Manager

PostgreSQL 대신 Redis를 사용한 설정 관리 시스템
psycopg3 ConnectionPool 기반 DB 연동 지원
"""
import os
import redis
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: get_all_configs 의 MGET 배치 크기. 너무 크면 한 번의 왕복이 무거워지고,
#: 너무 작으면 왕복 수가 늘어난다. 수백 개 규모의 config 에서는 한두 번의
#: 왕복으로 끝나는 크기.
_MGET_CHUNK = 256


def _is_db_available(db_manager) -> bool:
    """
    DB 연결이 사용 가능한지 확인 (psycopg3 호환)

    Args:
        db_manager: DB 매니저 인스턴스

    Returns:
        DB 사용 가능 여부
    """
    if db_manager is None:
        return False

    # AppDatabaseManager의 경우 내부 config_db_manager 확인
    actual_manager = db_manager
    if hasattr(db_manager, 'config_db_manager'):
        actual_manager = db_manager.config_db_manager

    # psycopg3: 풀 상태 또는 SQLite 연결 확인
    if hasattr(actual_manager, '_is_pool_healthy'):
        return actual_manager._is_pool_healthy()
    elif hasattr(actual_manager, 'db_type') and actual_manager.db_type == 'sqlite':
        return getattr(actual_manager, '_sqlite_connection', None) is not None
    elif hasattr(actual_manager, 'connection') and actual_manager.connection:
        # 레거시 호환성 (psycopg2 스타일)
        return True

    return False

class RedisConfigManager:
    """Redis를 사용한 설정 관리자"""

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None,
                 db: Optional[int] = None, password: Optional[str] = None,
                 db_manager = None, settings=None):
        """
        Args:
            settings: :class:`xgen_sdk.redis.RedisSettings`. 주면 접속 정보를
                여기서 전부 가져온다. 생략하면 환경변수에서 읽는다.
            host/port/db/password: 하위 호환용 개별 오버라이드.

        ⚠ **접속은 xgen_sdk.redis 팩토리가 만든다.** 예전에는 여기서 직접
        ``redis.Redis(host=..., port=...)`` 를 불렀는데, 그 결과 **config
        계층만 Sentinel 을 몰랐다** — 서비스의 클라이언트는 Sentinel 로 새
        마스터를 따라가는데 config 는 고정 주소에 남아, 페일오버 뒤 설정
        쓰기가 read-only 오류로 실패했다.

        ⚠ 기본값도 없앴다. 예전에는 host 가 사내 IP(192.168.2.242), password
        가 실제 비밀번호 문자열이었다 — 환경변수가 빠지면 조용히 엉뚱한
        서버에 붙었다. 지금은 설정이 없으면 **붙지 않는다**.
        """
        from xgen_sdk.redis import RedisSettings, create_sync_redis, ping_sync

        cfg = settings if settings is not None else RedisSettings.from_env()
        overrides = {}
        if host is not None:
            overrides['host'] = host
        if port is not None:
            overrides['port'] = port
        if db is not None:
            overrides['db'] = db
        if password is not None:
            overrides['password'] = password
        # config 값은 문자열로 다룬다 (기존 계약 유지).
        overrides['decode_responses'] = True
        cfg = cfg.with_overrides(**overrides)
        self._settings = cfg

        host = cfg.host
        port = cfg.port
        db = cfg.db
        password = cfg.password
        socket_timeout = cfg.socket_timeout
        socket_connect_timeout = cfg.socket_connect_timeout

        self._host = host
        self._port = port
        self._connection_available = False
        self.redis_client = None

        # 재연결 상태 — Redis 가 죽어도 서비스는 DB/로컬로 계속 돌고, **살아나면 자동으로
        # 다시 Redis 를 쓴다**. 예전에는 __init__ 에서 한 번만 붙어서, 기동 시점에 Redis 가
        # 없었거나 중간에 한 번 끊기면 그 프로세스는 **영원히** Redis 로 돌아오지 못했다.
        self._last_connect_attempt: float = 0.0
        self._reconnect_cooldown: float = float(os.getenv("REDIS_RECONNECT_COOLDOWN_SEC", "5") or 5)
        #: 재연결 성공 때마다 1 증가. 캐시를 든 상위 계층(ConfigComposer)이 이 값의 변화를
        #: 보고 "Redis 가 돌아왔다 → 다시 맞춰야 한다" 를 알아챈다.
        self.recovery_epoch: int = 0

        self._connect(initial=True)

        # Config 키 Prefix
        self.config_prefix = "config"

        # Multi-Pod 캐시 인밸리데이션용 글로벌 version sentinel 키.
        # 모든 config write 시점에 INCR 되어, 각 Pod 의 PersistentConfig 가
        # in-memory 캐시의 stale 여부를 lazy 하게 판단하는 용도로 사용된다.
        # 별도의 _meta: 네임스페이스를 두어 일반 config 키와 절대 충돌하지 않게 한다.
        self.version_key = f"{self.config_prefix}:_meta:version"

        # 이 매니저 인스턴스가 마지막으로 수행한 write 의 INCR 반환값.
        # composer.update_config 이 "다른 Pod 의 동시 쓰기에 영향받지 않고 본 write
        # 시점의 정확한 version" 을 확보하기 위해 set_config 직후 이 값을 읽는다.
        # FastAPI(uvicorn) 의 asyncio 환경에서 set_config 는 sync 함수이며 await 가
        # 없어 한 워커 안에서 한 번에 하나만 실행되므로 동일 워커 안 race-free.
        self._last_write_version: int = 0

        # get_config_by_name 미스 폴백(전체 스윕)의 결과 인덱스 캐시.
        # 글로벌 version sentinel 로 가드한다 — 어떤 write 든 version 을 INCR
        # 하므로, 값이 바뀌면 다음 조회에서 자동으로 재구성된다. 덕분에
        # "존재하지 않는 config 이름" 을 반복 조회해도 스윕은 version 당 1회.
        self._name_index_cache: Optional[Dict[str, Any]] = None
        self._name_index_tail_cache: Optional[Dict[str, Any]] = None
        self._name_index_version: int = -1

        # DB Manager (선택적)
        self.db_manager = db_manager

    # ========== Config 값 CRUD ==========

    # ──────────────────────────────────────────────────────────────────
    # 연결 수명주기 — 죽어도 계속 돌고, 살아나면 자동 복귀 (1.40.0)
    # ──────────────────────────────────────────────────────────────────

    def _connect(self, initial: bool = False) -> bool:
        """Redis 접속 시도. 성공 여부를 돌려주고 실패해도 예외를 올리지 않는다.

        접속 실패는 **치명적이지 않다** — config 값은 DB(persistent_configs)에도 있고
        PersistentConfig 가 DB 를 먼저 본다. 그래서 여기서는 상태만 기록하고 진행한다.
        """
        from xgen_sdk.redis import create_sync_redis

        self._last_connect_attempt = time.monotonic()
        cfg = self._settings
        try:
            self.redis_client = create_sync_redis(settings=cfg)
            self.redis_client.ping()
            was_down = not self._connection_available
            self._connection_available = True
            if initial:
                logger.info(f"✅ Redis Config Manager 초기화 완료: {cfg.describe()}")
            elif was_down:
                self.recovery_epoch += 1
                logger.info(
                    "🔁 Redis Config Manager 재연결 성공 (epoch=%s): %s",
                    self.recovery_epoch, cfg.describe(),
                )
            return True
        except redis.exceptions.ConnectionError as e:
            if initial:
                logger.warning(f"⚠️  Redis 연결 실패: {self._host}:{self._port}")
                logger.warning(f"   원인: {e}")
                logger.warning(f"   💡 Redis 서버가 실행 중인지 / REDIS_HOST·REDIS_PORT 를 확인하세요.")
                logger.warning(f"   ⏳ Redis 없이 계속 진행합니다 (DB 로 폴백, 복구되면 자동 재연결)")
            else:
                logger.debug(f"Redis 재연결 실패: {e}")
            self._connection_available = False
            return False
        except redis.exceptions.TimeoutError as e:
            if initial:
                logger.warning(f"⚠️  Redis 연결 타임아웃: {self._host}:{self._port} ({e})")
                logger.warning(f"   ⏳ Redis 없이 계속 진행합니다 (DB 로 폴백, 복구되면 자동 재연결)")
            else:
                logger.debug(f"Redis 재연결 타임아웃: {e}")
            self._connection_available = False
            return False
        except Exception as e:  # noqa: BLE001
            if initial:
                logger.warning(f"⚠️  Redis 초기화 중 오류: {e}")
            else:
                logger.debug(f"Redis 재연결 오류: {e}")
            self._connection_available = False
            return False

    def reconnect(self) -> bool:
        """즉시 재연결 시도 (쿨다운 무시). 운영/디버깅용."""
        return self._connect()

    def _ensure_connection(self) -> bool:
        """읽기·쓰기 진입점에서 부르는 자동 복구.

        끊긴 상태면 쿨다운(기본 5초)마다 한 번씩만 재접속을 시도한다. 요청마다
        무제한으로 붙으러 가면 Redis 가 죽어 있는 동안 전 요청이 접속 타임아웃만큼
        느려지므로, "죽어도 서비스는 정상 속도로 돈다" 는 원칙을 지키기 위한 쿨다운이다.
        """
        if self._connection_available:
            return True
        if (time.monotonic() - self._last_connect_attempt) < self._reconnect_cooldown:
            return False
        return self._connect()

    def health_check(self, auto_recover: bool = True) -> bool:
        """Redis 연결 상태 확인.

        끊겨 있으면 ``auto_recover`` 기본값에 따라 재연결을 한 번 시도한다(쿨다운 적용).
        예전에는 여기서 False 만 돌려주고 아무도 다시 붙지 않아, **한 번 끊긴 프로세스는
        영원히 Redis 를 쓰지 못했다**.
        """
        if not self._connection_available:
            if not auto_recover:
                return False
            return self._ensure_connection()
        try:
            return bool(self.redis_client.ping())
        except Exception as e:  # noqa: BLE001
            logger.error(f"Redis health check failed: {e}")
            self._connection_available = False
            return self._ensure_connection() if auto_recover else False

    def get_config_version(self) -> int:
        """글로벌 config version sentinel 값을 조회.

        Multi-Pod 환경에서 각 Pod 의 PersistentConfig 가 in-memory 캐시의 stale
        여부를 판단할 때 사용한다. 모든 write 경로(set_config / delete_config /
        update_config_by_name 등)에서 atomic 하게 INCR 된다.

        Returns:
            현재 버전(int). Redis 미사용 / 장애 / 키 부재 시 0 반환.
            반환값 0 은 단순히 "버전 정보 없음" 을 의미하며 호출처는 안전하게
            "이전과 동일" 로 간주해야 한다.
        """
        if not self._ensure_connection():
            return 0
        try:
            raw = self.redis_client.get(self.version_key)
            return int(raw) if raw is not None else 0
        except Exception as e:
            logger.debug(f"get_config_version failed: {e}")
            return 0

    # ──────────────────────────────────────────────────────────────────
    # probe_* — "못 읽었다" 를 숨기지 않는 조회 (1.39.0)
    #
    # 기존 get_config_value / get_config_version 은 **연결 실패와 값 부재를 똑같이**
    # default(0 / None) 로 돌려준다. 편의 API 로는 괜찮지만, 그 값으로 **보호 여부를
    # 결정하는 호출자**(권한 게이트·승인 게이트 등)에게는 위험하다 — 저장소 장애가
    # "설정 안 함" 으로 둔갑해 보호가 통째로 풀린다(fail-open).
    #
    # 아래 probe_* 는 (상태, 값) 을 돌려준다:
    #     ("ok", value)   읽었다
    #     ("missing", None) 저장소는 정상인데 그런 키가 없다
    #     ("error", None)   읽지 못했다 (연결 실패·예외)
    # 호출자는 "error" 를 보고 DB 폴백·마지막 확인값·fail-closed 를 스스로 정할 수 있다.
    # ──────────────────────────────────────────────────────────────────

    #: probe 결과 상태값.
    PROBE_OK = "ok"
    PROBE_MISSING = "missing"
    PROBE_ERROR = "error"

    def _probe_redis_value(self, env_name: str) -> Tuple[str, Any]:
        """Redis **한 곳만** 본다 — (상태, 값). 사다리의 1단."""
        if not self._ensure_connection():
            return (self.PROBE_ERROR, None)
        try:
            raw = self.redis_client.get(f"{self.config_prefix}:{env_name}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"probe_config_value 실패({env_name}): {e}")
            return (self.PROBE_ERROR, None)
        if raw is None:
            return (self.PROBE_MISSING, None)
        try:
            payload = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"probe_config_value 파싱 실패({env_name}): {e}")
            return (self.PROBE_ERROR, None)
        if "value" not in payload:
            return (self.PROBE_MISSING, None)
        return (self.PROBE_OK, payload.get("value"))

    def _probe_db_row(self, env_name: str, config_path: Optional[str] = None) -> Tuple[str, Any]:
        """DB **한 곳만** 본다 — (상태, 행). 사다리의 2단.

        ``__new__`` 로 만들어진 인스턴스(테스트 픽스처)에서도 안전하도록 getattr.
        """
        db_manager = getattr(self, "db_manager", None)
        if db_manager is None:
            return (self.PROBE_MISSING, None)
        try:
            from xgen_sdk.db.db_config_helper import probe_db_config_row

            return probe_db_config_row(
                db_manager, config_path=config_path, env_name=env_name
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"config DB 조회 실패({env_name}): {e}")
            return (self.PROBE_ERROR, None)

    def _restore_to_redis(self, row: Dict[str, Any]) -> None:
        """DB 에서 읽어 온 값을 Redis 에 되살린다 — **version 은 올리지 않는다**.

        복구는 값의 변경이 아니다. 여기서 sentinel 을 올리면 모든 파드가 "누가 설정을
        바꿨다" 로 오인해 전체 config 를 다시 읽는다(부팅·장애복구 때 stampede).
        """
        if not getattr(self, "_connection_available", False) or not row:
            return
        env_name = row.get("env_name")
        if not env_name:
            return
        config_path = row.get("config_path") or env_name
        category = config_path.split('.')[0] if '.' in config_path else 'unknown'
        payload = {
            'value': row.get("value"),
            'type': row.get("data_type", "string"),
            'category': category,
            'path': config_path,
            'env_name': env_name,
        }
        try:
            self.redis_client.set(f"{self.config_prefix}:{env_name}", json.dumps(payload))
            self.redis_client.sadd(f"{self.config_prefix}:category:{category}", env_name)
            logger.info("config restore: %s ← DB (Redis 에 없어 되살림)", env_name)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"config restore 실패({env_name}): {e}")

    # ──────────────────────────────────────────────────────────────────
    # probe_value — **설정을 읽는 유일한 사다리** (1.41.0)
    #
    #   Redis → DB → (없음)
    #
    # 예전에는 계층마다 사다리가 달랐다. core 의 PersistentConfig 는 DB→Redis,
    # 위성 파드(ConfigClient)는 **Redis 만**, 일부 서비스는 persistent_configs 를
    # 직접 SELECT 했다. 그래서 Redis 에서 키가 사라지면 core 화면은 [설정됨] 인데
    # 워크플로우 파드만 빈 값을 보는 일이 생겼다 — 그 빈 값이 그대로 provider SDK 로
    # 들어가 "Could not resolve authentication method" 가 됐다.
    #
    # 이제 모든 읽기는 이 함수 하나를 지난다. Redis 가 정본 캐시이고, 없거나 못 읽으면
    # DB 가 받아 준다. DB 에서 찾으면 Redis 로 되살려 다음 읽기부터 다시 뜨겁다.
    # ──────────────────────────────────────────────────────────────────
    def probe_value(self, env_name: str, config_path: Optional[str] = None) -> Tuple[str, Any, str]:
        """(상태, 값, 출처). 출처는 ``"redis"`` | ``"db"`` | ``""``.

        상태:
            ok      — 어느 한 곳에서 읽었다
            missing — 두 곳 다 정상인데 그런 설정이 없다
            error   — 읽지 못했다 (연결 실패·예외). **"설정 안 함" 과 절대 같지 않다.**
        """
        redis_status, value = self._probe_redis_value(env_name)
        if redis_status == self.PROBE_OK:
            return (self.PROBE_OK, value, "redis")

        db_status, row = self._probe_db_row(env_name, config_path)
        if db_status == self.PROBE_OK and row is not None:
            # Redis 가 "없다" 고 했을 때만 되살린다 — 못 읽은 상태(error)에서 쓰면
            # 엉뚱한 인스턴스에 쓰거나 끊긴 연결에 던지는 셈이다.
            if redis_status == self.PROBE_MISSING:
                self._restore_to_redis(row)
            return (self.PROBE_OK, row.get("value"), "db")

        if redis_status == self.PROBE_ERROR or db_status == self.PROBE_ERROR:
            return (self.PROBE_ERROR, None, "")
        return (self.PROBE_MISSING, None, "")

    def probe_config_value(self, env_name: str) -> Tuple[str, Any]:
        """설정 값 조회 — (상태, 값). 실패를 default 로 뭉개지 않는다.

        1.41.0 부터 Redis 뿐 아니라 DB 까지 보는 :meth:`probe_value` 를 쓴다.
        """
        status, value, _source = self.probe_value(env_name)
        return (status, value)

    def probe_config_version(self) -> Tuple[str, int]:
        """version sentinel 조회 — (상태, 버전).

        ``get_config_version`` 은 장애도 0 으로 돌려주므로 "확인했는데 0" 과
        "못 읽었다" 가 구분되지 않는다. 캐시 최신성을 **확인했는지** 알아야 하는
        호출자는 이 API 를 쓴다 (키가 아직 없으면 ok/0 — 그것도 확인된 사실이다).
        """
        if not self._ensure_connection():
            return (self.PROBE_ERROR, 0)
        try:
            raw = self.redis_client.get(self.version_key)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"probe_config_version 실패: {e}")
            return (self.PROBE_ERROR, 0)
        try:
            return (self.PROBE_OK, int(raw) if raw is not None else 0)
        except (TypeError, ValueError):
            return (self.PROBE_ERROR, 0)

    def bump_meta_version(self) -> int:
        """글로벌 version sentinel 을 명시적으로 INCR.

        Config 값 자체는 바꾸지 않으면서, 각 Pod 의 캐시된 클라이언트
        인스턴스(GuarderClient · EmbeddingClient 등) 를 강제로 재구성하도록
        유도할 때 사용한다. 예: 사용자가 UI 에서 "설정 초기화" 를 눌렀을 때.

        반환값은 INCR 직후의 새 버전이며, 호출자가 자기 Pod 의 캐시 버전을
        이 값으로 정렬할 때 사용할 수 있다.

        Returns:
            새 버전(int). Redis 미사용 / 장애 시 0.
        """
        if not self._connection_available:
            return 0
        try:
            new_version = self.redis_client.incr(self.version_key)
            if new_version is not None:
                self._last_write_version = int(new_version)
                logger.info(f"Meta version bumped (explicit) to {new_version}")
                return int(new_version)
            return 0
        except Exception as e:
            logger.warning(f"bump_meta_version failed: {e}")
            return 0

    # ========== 범용 캐시 version sentinel ==========
    #
    # config 말고도 **Pod 별 in-memory 캐시**를 두는 곳이 여럿 있다(사용자
    # preferences 게이트 등). 그런 캐시는 TTL 로만 버티면 "쓰기가 반영되기까지
    # 최대 TTL" 이라는 staleness 를 늘 지고 간다. 쓰기 쪽이 INCR 하고 읽기 쪽이
    # 값만 비교하면 그 staleness 가 사라진다 — config 가 이미 쓰는 방식이고,
    # 다른 서비스가 각자 Redis 를 만지지 않도록 SDK 가 표준으로 제공한다.
    #
    # 네임스페이스를 나누는 이유: 한 도메인의 쓰기가 다른 도메인의 캐시까지
    # 무효화하면 무관한 재조회가 연쇄된다.

    @staticmethod
    def _cache_version_key(namespace: str) -> str:
        ns = str(namespace or "default").strip() or "default"
        return f"cache:_meta:version:{ns}"

    def get_cache_version(self, namespace: str) -> int:
        """네임스페이스의 현재 캐시 버전.

        Redis 미사용/장애/키 부재면 ``0``. 호출처는 0 을 "버전 정보 없음"으로
        보고 **자기 TTL 로 안전하게 버텨야 한다** — 0 을 "안 바뀜"으로 믿고
        캐시를 영구히 붙들면 Redis 장애가 곧 stale 고착이 된다.
        """
        if not self._connection_available:
            return 0
        try:
            raw = self.redis_client.get(self._cache_version_key(namespace))
            return int(raw) if raw is not None else 0
        except Exception as e:
            logger.warning(f"get_cache_version({namespace}) failed: {e}")
            return 0

    def bump_cache_version(self, namespace: str) -> int:
        """네임스페이스의 캐시 버전을 INCR — 다른 Pod 의 캐시를 무효화한다.

        쓰기 경로에서 부른다. 실패해도 예외를 올리지 않는다: 캐시 무효화가
        사용자의 쓰기 자체를 실패시키면 안 된다(그 경우 읽기 쪽 TTL 이
        최후 방어선으로 남는다).

        Returns:
            새 버전(int). Redis 미사용/장애 시 0.
        """
        if not self._connection_available:
            return 0
        try:
            new_version = self.redis_client.incr(self._cache_version_key(namespace))
            return int(new_version) if new_version is not None else 0
        except Exception as e:
            logger.warning(f"bump_cache_version({namespace}) failed: {e}")
            return 0

    def set_config(self, config_path: str, config_value: Any,
                   data_type: str = "string", category: Optional[str] = None,
                   env_name: Optional[str] = None) -> bool:
        """
        설정 값 저장

        Args:
            config_path: 설정 경로 (예: "openai.api_key", "vast.vllm.port")
            config_value: 설정 값
            data_type: 데이터 타입 (string, int, float, bool, list, dict)
            category: 설정 카테고리 (예: "openai", "vast")
            env_name: 환경 변수 이름 (예: "OPENAI_API_KEY")

        Returns:
            bool: 성공 여부
        """
        try:
            # 카테고리 자동 추출 (config_path의 첫 번째 부분)
            if not category:
                category = config_path.split('.')[0]

            # env_name이 없으면 config_path 사용
            final_env_name = env_name or config_path

            # ── 입력 정규화 — escape 누적 차단 ──
            # data_type 이 list/dict 인데 config_value 가 string (이미 JSON-serialized)
            # 으로 들어오면 그대로 json.dumps 에 넣어 escape 누적 (`"["..."]"` → 매 boot 폭증)
            # 발생. set 직전에 한 번 deserialize 해서 native list/dict 로 정규화.
            if data_type == "list":
                if isinstance(config_value, str):
                    try:
                        from xgen_sdk.db.config_serializer import _safe_parse_json_list
                        config_value = _safe_parse_json_list(config_value)
                    except Exception as norm_err:
                        logger.warning(f"set_config: list normalize failed for {final_env_name}: {norm_err}")
                elif isinstance(config_value, list):
                    # element 가 escape string 인 경우(=이미 corrupt 된 입력) 복원.
                    try:
                        from xgen_sdk.db.config_serializer import _safe_parse_json_list
                        config_value = _safe_parse_json_list(config_value)
                    except Exception as norm_err:
                        logger.warning(f"set_config: list element-normalize failed for {final_env_name}: {norm_err}")
            elif data_type == "dict" and isinstance(config_value, str):
                try:
                    from xgen_sdk.db.config_serializer import _safe_parse_json_dict
                    config_value = _safe_parse_json_dict(config_value)
                except Exception as norm_err:
                    logger.warning(f"set_config: dict normalize failed for {final_env_name}: {norm_err}")

            # 설정 값과 메타데이터를 JSON으로 저장
            config_data = {
                'value': config_value,
                'type': data_type,
                'category': category,
                'path': config_path,
                'env_name': final_env_name
            }

            # ⚠ Redis 쓰기와 DB 쓰기는 **서로를 막지 않는다** (1.41.0).
            # 예전에는 Redis 쓰기가 한 블록 안에서 먼저 일어나, Redis 가 죽어 있으면
            # 예외가 나면서 **DB 쓰기까지 통째로 건너뛰었다** — 관리자가 UI 에서 저장한
            # 설정이 아무 데도 남지 않고 사라졌다. 영속 저장소는 DB 다.
            redis_ok = False
            if self._ensure_connection():
                try:
                    # Redis에 저장 (키: config:env_name)
                    redis_key = f"{self.config_prefix}:{final_env_name}"
                    self.redis_client.set(redis_key, json.dumps(config_data))

                    # 카테고리별 인덱스도 저장 (키: config:category:name, 값: env_name)
                    category_key = f"{self.config_prefix}:category:{category}"
                    self.redis_client.sadd(category_key, final_env_name)
                    redis_ok = True

                    # 글로벌 version sentinel 을 atomic 하게 증가시켜 다른 Pod 의 캐시 인밸리데이션을 유도.
                    # INCR 반환값(이 write 직후의 정확한 version)을 인스턴스 속성에 기록해 둠으로써
                    # 호출처(composer.update_config)가 다른 Pod 의 동시 쓰기에 영향받지 않고
                    # "내 write 의 정확한 version" 을 안전하게 알 수 있게 한다.
                    # 실패는 silent — set_config 자체의 성공/실패에 영향 주지 않는다.
                    try:
                        new_version = self.redis_client.incr(self.version_key)
                        if new_version is not None:
                            self._last_write_version = int(new_version)
                    except Exception as version_err:
                        logger.warning(f"Failed to bump config version sentinel: {version_err}")
                except Exception as redis_err:  # noqa: BLE001
                    logger.error(f"Config Redis 저장 실패 (DB 는 계속 진행): {config_path} - {redis_err}")
                    self._connection_available = False
            else:
                logger.warning(f"Redis 미연결 — {final_env_name} 은 DB 에만 저장한다")

            db_ok = False

            # DB에도 저장 (DB가 있는 경우)
            if self.db_manager:
                try:
                    # AppDatabaseManager의 update_config 메서드 사용
                    if hasattr(self.db_manager, 'update_config'):
                        # AppDatabaseManager 사용
                        logger.info(f"💾 [DB] Saving to AppDatabaseManager: {final_env_name}")
                        db_success = self.db_manager.update_config(
                            env_name=final_env_name,
                            config_path=config_path,
                            config_value=config_value,
                            data_type=data_type,
                        )
                        db_ok = bool(db_success)
                        if db_ok:
                            logger.info(f"✅ [DB] Config 저장 완료 (AppDatabaseManager): {final_env_name}")
                        else:
                            logger.warning(f"⚠️  DB 저장 실패: {config_path}")
                    else:
                        # 기존 DatabaseManager 사용 (레거시 호환 - psycopg3에도 동작)
                        if _is_db_available(self.db_manager):
                            logger.info(f"💾 [DB] Saving to DatabaseManager (legacy): {final_env_name}")
                            from xgen_sdk.db.db_config_helper import set_db_config
                            db_ok = bool(set_db_config(
                                self.db_manager,
                                config_path,
                                config_value,
                                data_type,
                                final_env_name
                            ))
                            logger.info(f"✅ [DB] Config 저장 완료 (legacy): {final_env_name}")
                        else:
                            logger.warning(f"⚠️  DB Manager has no available connection")
                except Exception as db_error:
                    logger.error(f"❌ DB 저장 실패: {config_path} - {db_error}", exc_info=True)
            else:
                logger.debug(f"No DB Manager configured for config: {final_env_name}")

            # 어느 한 곳이라도 남았으면 성공. **둘 다 실패했는데 True 를 돌려주면**
            # 관리자 화면은 "저장됨" 이라고 말하고 값은 어디에도 없다 — 그 거짓말이
            # 가장 비싸다.
            if not (redis_ok or db_ok):
                logger.error(
                    "Config 저장 실패 — Redis·DB 어느 쪽에도 쓰지 못했다: %s", config_path
                )
                return False
            logger.debug(
                "Config 저장 완료: %s (path=%s, redis=%s, db=%s)",
                final_env_name, config_path, redis_ok, db_ok,
            )
            return True

        except Exception as e:
            logger.error(f"Config 저장 실패: {config_path} - {str(e)}")
            return False

    def get_config_value(self, env_name: str, default: Any = None) -> Any:
        """
        설정 값만 조회 (env_name 기준)

        Args:
            env_name: 환경 변수 이름
            default: 기본값

        Returns:
            설정 값 또는 기본값

        1.41.0 부터 Redis → DB 사다리(:meth:`probe_value`)를 탄다. "못 읽었다" 와
        "없다" 를 구분해야 하는 호출자는 :meth:`probe_value` 를 직접 쓴다.
        """
        status, value, _source = self.probe_value(env_name)
        return value if status == self.PROBE_OK else default

    def get_config(self, env_name: str) -> Optional[Dict[str, Any]]:
        """
        설정 값과 메타데이터 조회 (env_name 기준)

        Args:
            env_name: 환경 변수 이름

        Returns:
            설정 데이터 (value, type, category, path, env_name)
        """
        try:
            redis_key = f"{self.config_prefix}:{env_name}"
            data = self.redis_client.get(redis_key)

            if data:
                return json.loads(data)
            return None

        except Exception as e:
            logger.error(f"Config 조회 실패: {env_name} - {str(e)}")
            return None

    def delete_config(self, env_name: str) -> bool:
        """
        설정 삭제 (env_name 기준)

        Args:
            env_name: 환경 변수 이름

        Returns:
            bool: 성공 여부
        """
        try:
            # 설정 데이터 먼저 조회하여 카테고리 확인
            config_data = self.get_config(env_name)
            if not config_data:
                logger.warning(f"삭제할 Config를 찾을 수 없음: {env_name}")
                return False

            category = config_data.get('category')

            # Redis에서 삭제
            redis_key = f"{self.config_prefix}:{env_name}"
            self.redis_client.delete(redis_key)

            # 카테고리 인덱스에서도 제거
            if category:
                category_key = f"{self.config_prefix}:category:{category}"
                self.redis_client.srem(category_key, env_name)

            # 글로벌 version sentinel 갱신 — 다른 Pod 가 삭제 사실을 다음 read 시 감지.
            try:
                new_version = self.redis_client.incr(self.version_key)
                if new_version is not None:
                    self._last_write_version = int(new_version)
            except Exception as version_err:
                logger.warning(f"Failed to bump config version sentinel on delete: {version_err}")

            logger.debug(f"Config 삭제 완료: {env_name}")
            return True

        except Exception as e:
            logger.error(f"Config 삭제 실패: {env_name} - {str(e)}")
            return False

    def get_category_configs(self, category: str) -> List[Dict[str, Any]]:
        """
        특정 카테고리의 모든 설정 조회 (리스트 형태)

        Args:
            category: 카테고리 이름

        Returns:
            설정 리스트
        """
        try:
            category_key = f"{self.config_prefix}:category:{category}"
            env_names = self.redis_client.smembers(category_key)

            configs = []
            for env_name in env_names:
                config = self.get_config(env_name)
                if config:
                    configs.append(config)

            return configs

        except Exception as e:
            logger.error(f"카테고리 Config 조회 실패: {category} - {str(e)}")
            return []

    def get_category_configs_nested(self, category: str) -> Dict[str, Any]:
        """
        특정 카테고리의 모든 설정 조회 (중첩 딕셔너리 형태)

        Args:
            category: 카테고리 이름

        Returns:
            중첩된 딕셔너리 형태의 설정
            예: {"openai": {"api_key": "...", "model": "..."}}
        """
        try:
            configs = self.get_category_configs(category)
            result = {}

            for config in configs:
                path = config['path']
                value = config['value']

                # 경로를 '.'로 분리하여 중첩 딕셔너리 생성
                keys = path.split('.')
                current = result

                for key in keys[:-1]:
                    if key not in current:
                        current[key] = {}
                    current = current[key]

                current[keys[-1]] = value

            return result

        except Exception as e:
            logger.error(f"카테고리 중첩 Config 조회 실패: {category} - {str(e)}")
            return {}

    def get_all_configs(self) -> List[Dict[str, Any]]:
        """
        모든 설정 조회

        ⚠ 이 메서드는 핫패스에서 불린다 (`get_config_by_name` 의 미스 폴백).
        그래서 두 가지를 지킨다:

        1. 키 스캔은 ``KEYS`` 가 아니라 ``SCAN``. KEYS 는 O(N) 이면서 그동안
           Redis 를 **블로킹**한다 — 공용 Redis 에서는 다른 서비스까지 멈춘다.
        2. 값 조회는 키마다 GET 왕복이 아니라 ``MGET`` 배치. 예전 구현은
           키 1개당 GET 1회를 순차로 돌아, config 240여 개 환경에서 호출
           1회당 240여 회의 왕복(합산 80~120ms)을 만들었다.

        Returns:
            모든 설정 리스트
        """
        if not self._ensure_connection():
            return []
        try:
            pattern = f"{self.config_prefix}:*"
            # category 인덱스 키 및 _meta: 네임스페이스(version sentinel 등) 제외
            keys = [
                key for key in self.redis_client.scan_iter(match=pattern, count=500)
                if ':category:' not in key and ':_meta:' not in key
            ]

            configs = []
            for start in range(0, len(keys), _MGET_CHUNK):
                chunk = keys[start:start + _MGET_CHUNK]
                for key, data in zip(chunk, self.redis_client.mget(chunk) or []):
                    if not data:
                        continue
                    try:
                        configs.append(json.loads(data))
                    except (json.JSONDecodeError, TypeError):
                        # 비-JSON 값(예: 메타 카운터 등 예기치 못한 키)은 조용히 skip
                        logger.debug(f"Skipping non-JSON config key during get_all: {key}")

            return configs

        except Exception as e:
            logger.error(f"전체 Config 조회 실패: {str(e)}")
            return []

    def clear_category(self, category: str) -> bool:
        """
        특정 카테고리의 모든 설정 삭제

        Args:
            category: 카테고리 이름

        Returns:
            bool: 성공 여부
        """
        try:
            category_key = f"{self.config_prefix}:category:{category}"
            env_names = self.redis_client.smembers(category_key)

            # 각 설정 삭제
            for env_name in env_names:
                self.delete_config(env_name)

            # 카테고리 인덱스도 삭제
            self.redis_client.delete(category_key)

            logger.info(f"카테고리 '{category}' 전체 삭제 완료")
            return True

        except Exception as e:
            logger.error(f"카테고리 삭제 실패: {category} - {str(e)}")
            return False

    def exists(self, env_name: str) -> bool:
        """
        설정 존재 여부 확인 (env_name 기준)

        Args:
            env_name: 환경 변수 이름

        Returns:
            bool: 존재 여부
        """
        try:
            redis_key = f"{self.config_prefix}:{env_name}"
            return self.redis_client.exists(redis_key) > 0

        except Exception as e:
            logger.error(f"Config 존재 확인 실패: {env_name} - {str(e)}")
            return False

    def get_all_categories(self) -> List[str]:
        """
        모든 카테고리 목록 조회

        Returns:
            카테고리 목록
        """
        try:
            pattern = f"{self.config_prefix}:category:*"
            keys = self.redis_client.keys(pattern)

            # 카테고리 이름만 추출
            categories = [key.split(':')[-1] for key in keys]
            return sorted(categories)

        except Exception as e:
            logger.error(f"카테고리 목록 조회 실패: {str(e)}")
            return []

    # ========== ConfigComposer 호환성 메서드 ==========

    def get_config_by_name(self, config_name: str) -> Any:
        """
        이름으로 특정 설정 가져오기 (ConfigComposer 호환)

        Args:
            config_name: 설정 이름 (예: "OPENAI_API_KEY", "PORT")

        Returns:
            설정 값

        Raises:
            KeyError: 설정이 존재하지 않는 경우
        """
        status, value, _source = self.probe_value(config_name, config_path=config_name)
        if status == self.PROBE_OK:
            return value

        try:
            # env_name / config_path 로 못 찾으면 path 끝조각으로 한 번 더 본다.
            #    예전에는 여기서 매번 전체 config 를 스윕했다 — 존재하지 않는
            #    (혹은 값이 None 인) config 를 조회할 때마다 Redis 왕복이
            #    config 개수만큼 발생해, 요청 1건에 수백 회 GET 이 찍혔다.
            #    이제는 version 으로 가드된 인덱스를 쓴다.
            exact, tail = self._get_name_index()
            if config_name in exact:
                return exact[config_name]
            if config_name in tail:
                return tail[config_name]
        except Exception as e:  # noqa: BLE001
            logger.error(f"Config 이름 인덱스 조회 실패: {config_name} - {str(e)}")

        raise KeyError(f"Configuration '{config_name}' not found")

    def _get_name_index(self) -> tuple:
        """(exact, tail) 이름 인덱스를 반환. 글로벌 version 이 바뀌면 재구성.

        - exact : env_name / path 전체와 일치하는 이름 → 값
        - tail  : path 의 마지막 조각과 일치하는 이름 → 값 (exact 보다 낮은 우선순위)

        version 조회 실패(Redis 장애 등)는 캐시를 버리지 않는다 — 조회 자체가
        실패하는 상황에서 스윕을 반복해도 얻는 게 없다.
        """
        try:
            current = self.get_config_version()
        except Exception:
            current = getattr(self, "_name_index_version", -1)

        # __new__ 로 만들어진 인스턴스(테스트 픽스처 등)에서도 안전하도록 getattr.
        cached = getattr(self, "_name_index_cache", None)
        cached_tail = getattr(self, "_name_index_tail_cache", None)
        cached_version = getattr(self, "_name_index_version", -1)
        if cached is not None and cached_tail is not None and current == cached_version:
            return cached, cached_tail

        exact: Dict[str, Any] = {}
        tail: Dict[str, Any] = {}
        for config in self.get_all_configs():
            value = config.get('value')
            env_name = config.get('env_name')
            path = config.get('path') or ''
            if env_name:
                exact.setdefault(env_name, value)
            if path:
                exact.setdefault(path, value)
                last = path.split('.')[-1]
                if last:
                    tail.setdefault(last, value)

        self._name_index_cache = exact
        self._name_index_tail_cache = tail
        self._name_index_version = current
        return exact, tail

    def get_config_by_category_name(self, category_name: str) -> Dict[str, Any]:
        """
        카테고리 이름으로 특정 설정 그룹 가져오기 (ConfigComposer 호환)

        Args:
            category_name: 카테고리 이름 (예: "openai", "database", "app")

        Returns:
            해당 카테고리의 모든 설정 (중첩 딕셔너리 형태)

        Raises:
            KeyError: 카테고리가 존재하지 않는 경우
        """
        try:
            configs = self.get_category_configs_nested(category_name)

            if not configs:
                raise KeyError(f"Configuration category '{category_name}' not found")

            return configs

        except Exception as e:
            logger.error(f"카테고리 Config 조회 실패: {category_name} - {str(e)}")
            raise KeyError(f"Configuration category '{category_name}' not found")

    def update_config_by_name(self, config_name: str, new_value: Any) -> None:
        """
        이름으로 특정 설정 업데이트 (ConfigComposer 호환)

        Args:
            config_name: 설정 이름
            new_value: 새로운 값

        Raises:
            KeyError: 설정이 존재하지 않는 경우
        """
        try:
            # 1. env_name으로 직접 검색 시도
            if self.exists(config_name):
                config_data = self.get_config(config_name)
                self.set_config(
                    config_path=config_data.get('path', config_name),
                    config_value=new_value,
                    data_type=config_data.get('type', 'string'),
                    category=config_data.get('category'),
                    env_name=config_data.get('env_name')
                )
                logger.info(f"Config 업데이트 완료: {config_name} = {new_value}")
                return

            # 2. 모든 config에서 검색하여 업데이트
            all_configs = self.get_all_configs()
            for config in all_configs:
                if config.get('env_name') == config_name or config['path'] == config_name:
                    self.set_config(
                        config_path=config['path'],
                        config_value=new_value,
                        data_type=config['type'],
                        category=config['category'],
                        env_name=config.get('env_name')
                    )
                    logger.info(f"Config 업데이트 완료: {config_name} = {new_value}")
                    return

                # path의 마지막 부분이 config_name과 일치하는 경우
                path_parts = config['path'].split('.')
                if path_parts[-1] == config_name:
                    self.set_config(
                        config_path=config['path'],
                        config_value=new_value,
                        data_type=config['type'],
                        category=config['category'],
                        env_name=config.get('env_name')
                    )
                    logger.info(f"Config 업데이트 완료: {config['path']} = {new_value}")
                    return

            raise KeyError(f"Configuration '{config_name}' not found")

        except Exception as e:
            logger.error(f"Config 업데이트 실패: {config_name} - {str(e)}")
            raise

    def get_all_config(self, **kwargs) -> Dict[str, Any]:
        """
        모든 설정을 카테고리별로 구조화하여 반환 (ConfigComposer 호환)

        Returns:
            Dict: {
                "category_name": {nested configs},
                "all_configs": [list of all configs]
            }
        """
        try:
            result = {}

            # 모든 카테고리 가져오기
            categories = self.get_all_categories()

            for category in categories:
                result[category] = self.get_category_configs_nested(category)

            # 전체 config 리스트 추가
            result["all_configs"] = self.get_all_configs()

            return result

        except Exception as e:
            logger.error(f"전체 Config 조회 실패: {str(e)}")
            return {"all_configs": []}

    def get_config_summary(self) -> Dict[str, Any]:
        """
        모든 설정의 요약 정보 반환 (ConfigComposer 호환)

        Returns:
            Dict: 설정 요약 정보
        """
        try:
            all_configs = self.get_all_configs()
            categories = self.get_all_categories()

            # 카테고리별 요약
            categories_summary = {}
            for category in categories:
                try:
                    category_configs = self.get_category_configs(category)
                    categories_summary[category] = {
                        "count": len(category_configs),
                        "configs": [
                            {
                                "path": cfg["path"],
                                "type": cfg["type"],
                                "has_value": cfg["value"] is not None
                            }
                            for cfg in category_configs
                        ]
                    }
                except Exception as e:
                    logger.error(f"Failed to get summary for {category}: {e}")
                    categories_summary[category] = {"error": str(e)}

            return {
                "total_configs": len(all_configs),
                "discovered_categories": categories,
                "categories": categories_summary,
                "persistent_summary": self.export_config_summary()
            }

        except Exception as e:
            logger.error(f"Config 요약 정보 조회 실패: {str(e)}")
            return {
                "total_configs": 0,
                "discovered_categories": [],
                "categories": {},
                "persistent_summary": {},
                "error": str(e)
            }

    def update_config(self, config_name: str, new_value: Any) -> Dict[str, Any]:
        """
        설정값을 업데이트하고 결과를 반환하는 통합 메서드 (ConfigComposer 호환)

        Args:
            config_name: 설정 이름
            new_value: 새로운 값

        Returns:
            Dict: 업데이트 결과 정보

        Raises:
            KeyError: 설정이 존재하지 않는 경우
            ValueError: 타입 변환 실패 시
        """
        try:
            # 기존 설정 가져오기
            old_value = self.get_config_by_name(config_name)

            # 설정 업데이트
            self.update_config_by_name(config_name, new_value)

            logger.info(f"Config 업데이트 성공: {config_name}: {old_value} -> {new_value}")

            return {
                "config_name": config_name,
                "old_value": old_value,
                "new_value": new_value,
                "success": True
            }

        except KeyError:
            logger.error(f"Config '{config_name}' not found")
            raise KeyError(f"Config '{config_name}' not found")
        except Exception as e:
            logger.error(f"Config 업데이트 실패: {config_name} - {str(e)}")
            raise ValueError(f"Failed to update config '{config_name}': {str(e)}")

    def refresh_all(self) -> None:
        """
        모든 설정을 Redis에서 다시 로드 (ConfigComposer 호환)
        Redis는 항상 최신 상태이므로 실제로는 아무 작업도 하지 않음
        """
        logger.info("=== Redis configs are always up-to-date, no refresh needed ===")

    def export_config_summary(self) -> Dict[str, Any]:
        """
        모든 설정의 요약 정보 반환 (PersistentConfig 형태)

        Returns:
            Dict: 설정 요약 정보
        """
        try:
            all_configs = self.get_all_configs()

            return {
                "total_configs": len(all_configs),
                "storage_type": "redis",
                "configs": [
                    {
                        "env_name": config.get("env_name", ""),
                        "config_path": config.get("path", ""),
                        "current_value": config.get("value"),
                        "default_value": config.get("env_value"),
                        "is_saved": config.get("config_value") is not None,
                        "data_type": config.get("type", "string"),
                        "category": config.get("category")
                    }
                    for config in all_configs
                ]
            }
        except Exception as e:
            logger.error(f"설정 요약 정보 생성 실패: {str(e)}")
            return {
                "total_configs": 0,
                "storage_type": "redis",
                "configs": [],
                "error": str(e)
            }

    def get_registry_statistics(self) -> Dict[str, Any]:
        """
        레지스트리 통계 정보 반환

        Returns:
            Dict: 통계 정보
        """
        try:
            all_configs = self.get_all_configs()
            config_paths = [config['path'] for config in all_configs]
            env_names = [config['env_name'] for config in all_configs]

            # 중복 검사
            duplicate_paths = []
            duplicate_names = []

            seen_paths = set()
            seen_names = set()

            for path in config_paths:
                if path in seen_paths:
                    duplicate_paths.append(path)
                seen_paths.add(path)

            for name in env_names:
                if name in seen_names:
                    duplicate_names.append(name)
                seen_names.add(name)

            return {
                "total_configs": len(all_configs),
                "unique_config_paths": len(set(config_paths)),
                "unique_env_names": len(set(env_names)),
                "duplicate_config_paths": duplicate_paths,
                "duplicate_env_names": duplicate_names,
                "has_duplicates": len(duplicate_paths) > 0 or len(duplicate_names) > 0,
                "categories": self.get_all_categories(),
                "storage_type": "redis"
            }
        except Exception as e:
            logger.error(f"통계 정보 조회 실패: {str(e)}")
            return {
                "total_configs": 0,
                "error": str(e)
            }

    def save_all(self) -> None:
        """
        모든 설정을 Redis에 저장 (ConfigComposer 호환)
        Redis는 즉시 저장되므로 실제로는 아무 작업도 하지 않음
        """
        logger.info("=== Redis configs are auto-saved, no manual save needed ===")

    def validate_critical_configs(self) -> Dict[str, Any]:
        """
        중요한 설정들이 올바르게 설정되었는지 검증 (ConfigComposer 호환)

        Returns:
            Dict: 검증 결과
        """
        validation_results = {
            "valid": True,
            "warnings": [],
            "errors": []
        }

        try:
            # 포트 번호 검증
            try:
                port = self.get_config_by_name("PORT")
                if port is not None:
                    port_int = int(port)
                    if not (1 <= port_int <= 65535):
                        validation_results["errors"].append(f"Invalid port number: {port}")
                        validation_results["valid"] = False
            except (KeyError, ValueError):
                pass

            # API 키 존재 여부 확인
            try:
                api_key = self.get_config_by_name("OPENAI_API_KEY")
                if not api_key or api_key.strip() == "":
                    validation_results["warnings"].append("OpenAI API Key is not set")
            except KeyError:
                pass

            # 데이터베이스 연결 정보 확인
            try:
                db_host = self.get_config_by_name("DATABASE_HOST")
                if not db_host:
                    validation_results["warnings"].append("Database host is not set")
            except KeyError:
                pass

        except Exception as e:
            logger.error(f"Config 검증 실패: {str(e)}")
            validation_results["errors"].append(f"Validation error: {str(e)}")
            validation_results["valid"] = False

        return validation_results
