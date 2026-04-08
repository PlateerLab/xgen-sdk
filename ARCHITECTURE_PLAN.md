# xgen-sdk 아키텍처 설계 계획서

## 1. 현황 분석 (As-Is)

### 1.1 현재 아키텍처

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────────┐
│ xgen-workflow │     │  xgen-documents  │     │    xgen-core     │
│              │     │                  │     │                  │
│ DatabaseClient├────►│ DatabaseClient   ├────►│ AppDatabaseManager│──► PostgreSQL
│ (HTTP proxy) │     │ (HTTP proxy)     │     │ (psycopg3 pool)  │
│              │     │                  │     │                  │
│ ConfigClient ├────►│ ConfigClient     ├────►│ RedisConfigManager│──► Redis
│ (HTTP proxy) │     │ (HTTP proxy)     │     │                  │
│              │     │                  │     │                  │
│ minio_client │     │ minio_client     │     │ minio_client     │──► MinIO
│ (직접 연결)   │     │ (직접 연결)       │     │ (직접 연결)       │
│              │     │                  │     │                  │
│ RedisClient  │     │ (없음)           │     │ Redis 직접 연결   │──► Redis
│ (세션용)      │     │                  │     │                  │
│              │     │                  │     │                  │
│ controllerHlp│     │ controllerHelper │     │ controllerHelper │
│ (권한 헤더)   │     │ (권한 헤더)       │     │ (권한+DB조회)     │
└──────────────┘     └──────────────────┘     └──────────────────┘
```

### 1.2 핵심 문제점

| # | 문제 | 상세 |
|---|------|------|
| 1 | **코드 중복** | `DatabaseClient` 99% 동일 (workflow/documents), `ConfigClient` 100% 동일, `minio_client` 60% 동일, `controllerHelper` 80% 동일 |
| 2 | **HTTP 병목** | workflow/documents의 모든 DB 조작이 xgen-core HTTP를 경유 → 네트워크 레이턴시 + 직렬화 오버헤드 |
| 3 | **권한 시스템 파편화** | xgen-core: DB조회 기반 ABAC, workflow: 헤더 파싱만 (2/100+ 엔드포인트), documents: 거의 없음 |
| 4 | **설정 관리 비일관성** | xgen-core: Redis 직접 + Local fallback, 나머지: HTTP proxy (redis 직접 접근 없음) |
| 5 | **독립 배포 불가** | 공유 로직 변경 시 3개 컨테이너 모두 수동으로 동일 수정 필요 |

---

## 2. 목표 아키텍처 (To-Be)

### 2.1 설계 원칙

1. **Direct Connection First**: 각 컨테이너가 SDK를 통해 DB/Redis/MinIO에 직접 연결
2. **Schema Governance Centralized**: DB 마이그레이션만 xgen-core에서 수행
3. **Single Source of Truth**: 공유 로직은 오직 xgen-sdk에만 존재
4. **Zero HTTP Proxy**: 컨테이너 간 DB 조작용 HTTP 호출 전면 제거
5. **pip install**: 각 컨테이너는 `xgen-sdk`를 의존성으로 설치

### 2.2 목표 구조

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────────┐
│ xgen-workflow │     │  xgen-documents  │     │    xgen-core     │
│              │     │                  │     │                  │
│  ┌─────────┐ │     │  ┌─────────┐    │     │  ┌─────────┐    │
│  │xgen-sdk │ │     │  │xgen-sdk │    │     │  │xgen-sdk │    │
│  │         │ │     │  │         │    │     │  │         │    │
│  │ XgenDB ─┼─┼─────┼──┼─► PostgreSQL ◄┼────┼──┼─ XgenDB │    │
│  │ XgenRedis┼─┼─────┼──┼─► Redis     ◄┼────┼──┼─ XgenRedis   │
│  │ XgenStore┼─┼─────┼──┼─► MinIO     ◄┼────┼──┼─ XgenStore   │
│  │ XgenAuth │ │     │  │ XgenAuth │    │     │  │ XgenAuth │    │
│  └─────────┘ │     │  └─────────┘    │     │  └─────────┘    │
│              │     │                  │     │                  │
│ (워크플로우   │     │ (문서 처리       │     │ (어드민/인증     │
│  비즈니스)    │     │  비즈니스)       │     │  + DB 마이그레이션)│
└──────────────┘     └──────────────────┘     └──────────────────┘
```

### 2.3 HTTP Proxy 제거 효과

**Before** (현재):
```
xgen-workflow → HTTP(serialize) → xgen-core → psycopg3 → PostgreSQL → xgen-core → HTTP(deserialize) → xgen-workflow
~50ms per query (network + serialization overhead)
```

**After** (SDK):
```
xgen-workflow → xgen-sdk(XgenDB) → psycopg3 → PostgreSQL
~5ms per query (direct connection)
```

---

## 3. xgen-sdk 패키지 구조

```
xgen-sdk/
├── pyproject.toml
├── README.md
├── src/
│   └── xgen_sdk/
│       ├── __init__.py              # 최상위 export
│       │
│       ├── db/                      # 데이터베이스 모듈
│       │   ├── __init__.py
│       │   ├── pool_manager.py      # psycopg3 ConnectionPool 관리 (현 database_manager_psycopg3.py)
│       │   ├── app_db.py            # AppDatabaseManager (현 connection_psycopg3.py)
│       │   ├── base_model.py        # BaseModel 추상 클래스
│       │   ├── config_serializer.py # 직렬화 유틸
│       │   └── retry.py             # with_retry 데코레이터 + 복구 로직
│       │
│       ├── config/                  # 설정 관리 모듈 
│       │   ├── __init__.py
│       │   ├── redis_config.py      # RedisConfigManager (현 redis_config_manager.py)
│       │   ├── local_config.py      # LocalConfigManager (현 local_config_manager.py)
│       │   ├── config_factory.py    # create_config_manager() 팩토리
│       │   └── config_utils.py      # dict_to_namespace, get_config_dict
│       │
│       ├── storage/                 # 오브젝트 스토리지 모듈
│       │   ├── __init__.py
│       │   └── minio_client.py      # MinIO 통합 클라이언트 (3개 컨테이너 합본)
│       │
│       ├── auth/                    # 인증/인가 모듈
│       │   ├── __init__.py
│       │   ├── gateway_headers.py   # 게이트웨이 헤더 파싱 (X-User-* 추출)
│       │   ├── permission.py        # 권한 매칭 (has_permission + 와일드카드)
│       │   ├── permission_constants.py # ALL_PERMISSIONS 정의
│       │   ├── permission_registry.py  # PermissionRegistry + require_perm 데코레이터
│       │   ├── permission_resolver.py  # resolve_user_permissions (DB 조회)
│       │   └── supervision.py       # 감독 범위 해석
│       │
│       └── redis/                   # Redis 유틸리티 모듈
│           ├── __init__.py
│           └── client.py            # 범용 Redis 클라이언트 (연결 + 세션)
```

---

## 4. 모듈별 상세 설계

### 4.1 `xgen_sdk.db` — 데이터베이스 모듈

**출처**: `xgen-core/service/database/` 전체

#### 핵심 클래스: `XgenDB`

```python
from xgen_sdk.db import XgenDB

# 각 컨테이너 startup에서:
db = XgenDB(
    host="postgres",
    port=5432,
    database="xgen",
    user="xgen",
    password="...",
    # 또는 환경변수 자동 로드
)

# CRUD — 기존 AppDatabaseManager와 100% 동일 인터페이스
record_id = db.insert(user_model)
db.update(user_model)
db.delete(UserModel, record_id)
records = db.find_by_condition(UserModel, {"status": "active"}, limit=100)
record = db.find_by_id(UserModel, 123)
result = db.execute_raw_query("SELECT ...", params)

# Pool 관리
stats = db.get_pool_stats()
db.close()
```

#### 이전 계획

| 현재 위치 | SDK 위치 | 변경 사항 |
|-----------|---------|----------|
| `xgen-core/service/database/database_manager_psycopg3.py` | `xgen_sdk/db/pool_manager.py` | 그대로 이동 |
| `xgen-core/service/database/connection_psycopg3.py` | `xgen_sdk/db/app_db.py` | 클래스명 `AppDatabaseManager` → `XgenDB` (alias 유지) |
| `xgen-core/service/database/models/base_model.py` | `xgen_sdk/db/base_model.py` | 그대로 이동 |
| `xgen-core/service/database/config_serializer.py` | `xgen_sdk/db/config_serializer.py` | 그대로 이동 |
| `xgen-workflow/service/database/database_client.py` | **삭제** | SDK의 XgenDB로 교체 |
| `xgen-documents/service/database/database_client.py` | **삭제** | SDK의 XgenDB로 교체 |

#### DB 마이그레이션 (xgen-core에 유지)

```
xgen-core/
├── service/database/
│   ├── models/           # ← 50+ 모델 정의는 xgen-core에 유지
│   │   ├── __init__.py   #    APPLICATION_MODELS 레지스트리
│   │   ├── user.py
│   │   ├── role.py
│   │   └── ...
│   └── migrations/       # ← 마이그레이션도 xgen-core에 유지
│       ├── seed_permissions.py
│       └── ...
```

**이유**: 모델 정의와 스키마 마이그레이션은 한 곳에서만 수행해야 데이터 정합성 보장. SDK는 DB 연결 + CRUD만 제공.

#### xgen-core의 Data API 제거 대상

현재 xgen-core가 HTTP API로 노출하는 다음 엔드포인트들은 **더 이상 불필요**:
- `POST /api/data/db/insert`
- `POST /api/data/db/update`
- `POST /api/data/db/delete`
- `POST /api/data/db/find-by-id`
- `POST /api/data/db/find-by-condition`
- `POST /api/data/db/query`
- (총 14개 엔드포인트)

**단, 즉시 삭제가 아닌 deprecated 마킹 후 단계적 제거.**

---

### 4.2 `xgen_sdk.config` — 설정 관리 모듈

**출처**: `xgen-core/service/redis_client/` 전체

#### 핵심 클래스: `XgenConfig`

```python
from xgen_sdk.config import XgenConfig

# 각 컨테이너 startup에서:
config = XgenConfig(
    redis_host="redis",
    redis_port=6379,
    redis_db=0,
    db_manager=db,          # Optional: DB 동기화용
    fallback_to_local=True, # Redis 실패 시 Local fallback
)

# CRUD
config.set("openai.api_key", "sk-...", category="openai", env_name="OPENAI_API_KEY")
value = config.get("OPENAI_API_KEY", default=None)
category_configs = config.get_category("openai")
config.delete("OPENAI_API_KEY")

# 편의 기능
ns = config.as_namespace()  # SimpleNamespace (attribute access)
flat = config.as_dict(flatten=True)
```

#### 이전 계획

| 현재 위치 | SDK 위치 | 변경 사항 |
|-----------|---------|----------|
| `xgen-core/service/redis_client/redis_config_manager.py` | `xgen_sdk/config/redis_config.py` | 그대로 이동 |
| `xgen-core/service/redis_client/local_config_manager.py` | `xgen_sdk/config/local_config.py` | 그대로 이동 |
| `xgen-core/service/redis_client/config_utils.py` | `xgen_sdk/config/config_utils.py` | 그대로 이동 |
| `xgen-workflow/service/config/config_client.py` | **삭제** | SDK의 XgenConfig로 교체 |
| `xgen-documents/service/config/config_client.py` | **삭제** | SDK의 XgenConfig로 교체 |

**Config HTTP API도 단계적 제거 대상** (`/api/data/config/*` 11개 엔드포인트)

---

### 4.3 `xgen_sdk.storage` — 오브젝트 스토리지 모듈

**출처**: 3개 컨테이너의 minio_client.py 통합

#### 핵심 클래스: `XgenStorage`

```python
from xgen_sdk.storage import XgenStorage

storage = XgenStorage(
    endpoint="minio:9000",
    access_key="minioadmin",
    secret_key="...",
    secure=False,
    public_endpoint=None,   # presigned URL용 외부 주소
)

# 기본 CRUD (xgen-core 수준)
storage.upload("bucket", "path/file.pdf", local_path)
storage.download("bucket", "path/file.pdf", local_path)
storage.delete("bucket", "path/file.pdf")
exists = storage.exists("bucket", "path/file.pdf")
info = storage.get_info("bucket", "path/file.pdf")

# 확장 기능 (xgen-workflow 수준)
folders = storage.list_folders("bucket", "prefix/")
files = storage.list_files("bucket", "prefix/")
storage.copy("bucket", "src.pdf", "dst.pdf")

# 고급 기능 (xgen-documents 수준)
url = storage.get_presigned_url("bucket", "path/file.pdf", expires=3600)
storage.ensure_bucket("bucket_name")
```

#### 이전 계획

| 현재 위치 | SDK 위치 | 변경 사항 |
|-----------|---------|----------|
| `xgen-core/service/storage/minio_client.py` (165줄) | 통합 | 기본 CRUD |
| `xgen-workflow/service/storage/minio_client.py` (265줄) | 통합 | + list, copy |
| `xgen-documents/service/storage/minio_client.py` (245줄) | 통합 | + presigned URL |
| 3개 파일 | `xgen_sdk/storage/minio_client.py` | 3개의 superset 통합 |

**참고**: `xgen-documents/service/storage/storage_service.py` (600줄 StorageManager)는 문서 도메인 로직이므로 xgen-documents에 유지.

---

### 4.4 `xgen_sdk.auth` — 인증/인가 모듈

**출처**: `xgen-core/service/permission/` 전체 + 3개 컨테이너의 controllerHelper.py

#### 4.4.1 게이트웨이 헤더 파싱

```python
from xgen_sdk.auth import get_user_context

# FastAPI Request에서 게이트웨이 헤더 추출
@router.get("/something")
async def handler(request: Request):
    ctx = get_user_context(request)
    # ctx.user_id: str
    # ctx.username: str
    # ctx.is_superuser: bool
    # ctx.roles: List[str]
    # ctx.permissions: List[str]
    # ctx.groups: List[str]
    # ctx.supervision_full: List[str]
    # ctx.supervision_monitor: List[str]
    # ctx.supervision_audit: List[str]
```

#### 4.4.2 권한 검사 데코레이터

```python
from xgen_sdk.auth import require_perm, require_any_perm, require_superuser

@router.get("/users")
async def list_users(request: Request, _=Depends(require_perm("admin.user:read"))):
    ...

@router.post("/workflow")
async def create(request: Request, _=Depends(require_any_perm("workflow:create", "workflow:manage"))):
    ...

@router.delete("/system/reset")
async def reset(request: Request, _=Depends(require_superuser())):
    ...
```

#### 4.4.3 권한 매칭 엔진

```python
from xgen_sdk.auth import has_permission

# 와일드카드 지원
has_permission({"admin.*:*"}, "admin.user:read")   # True
has_permission({"workflow:*"}, "workflow:create")    # True
has_permission({"*:*"}, "anything:anything")         # True (superuser)
```

#### 4.4.4 권한 해석 (DB 조회)

```python
from xgen_sdk.auth import resolve_user_permissions, resolve_supervision_scope

# DB에서 사용자 권한 해석 (xgen-core + 게이트웨이에서 사용)
perms = resolve_user_permissions(db, user_id=123, is_superuser=False, groups=["team_a"])
# → {"admin.user:read", "workflow:create", ...}

# 감독 범위 해석
scope = resolve_supervision_scope(db, user_id=1, is_superuser=False)
# → {"full": ["developer"], "monitor": ["operator"], "audit": []}
```

#### 4.4.5 권한 레지스트리 + 상수

```python
from xgen_sdk.auth import registry, ALL_PERMISSIONS, load_constants, validate_and_sync

# 앱 시작 시
load_constants()
result = validate_and_sync(db)
# {registered: 114, inserted: 0, orphaned: [], warnings: []}
```

#### 이전 계획

| 현재 위치 | SDK 위치 | 변경 사항 |
|-----------|---------|----------|
| `xgen-core/service/permission/permission_constants.py` | `xgen_sdk/auth/permission_constants.py` | 그대로 이동 |
| `xgen-core/service/permission/permission_registry.py` | `xgen_sdk/auth/permission_registry.py` | 그대로 이동 |
| `xgen-core/service/permission/permission_resolver.py` | `xgen_sdk/auth/permission_resolver.py` | `app_db` → `XgenDB` 타입 변경 |
| `xgen-core/service/permission/supervision_resolver.py` | `xgen_sdk/auth/supervision.py` | `app_db` → `XgenDB` 타입 변경 |
| `xgen-core/controller/helper/controllerHelper.py` (공통 부분) | `xgen_sdk/auth/gateway_headers.py` | 헤더 파싱 + 유저 컨텍스트 |
| `xgen-workflow/controller/helper/controllerHelper.py` (공통 부분) | **삭제** | SDK로 교체 |
| `xgen-documents/controller/helper/controllerHelper.py` (공통 부분) | **삭제** | SDK로 교체 |

---

### 4.5 `xgen_sdk.redis` — Redis 유틸리티 모듈

```python
from xgen_sdk.redis import XgenRedis

redis = XgenRedis(
    host="redis",
    port=6379,
    password="...",
    db=0,
)

# 범용 작업
redis.set("key", "value", ttl=3600)
value = redis.get("key")
redis.delete("key")

# 세션 관리 (현 xgen-workflow RedisClient)
redis.save_session("workflow:session:123", data, ttl=86400)
session = redis.get_session("workflow:session:123")
sessions = redis.list_sessions("workflow:session:*")
```

**이 모듈은 xgen-core의 RedisConfigManager와 별개**. RedisConfigManager는 `xgen_sdk.config`에 속하고, 이 모듈은 범용 Redis 래퍼.

---

## 5. 패키지 배포 전략

### 5.1 pyproject.toml

```toml
[project]
name = "xgen-sdk"
version = "1.0.0"
description = "XGen Platform Shared SDK"
requires-python = ">=3.11"
dependencies = [
    "psycopg[binary]>=3.1.0",
    "psycopg-pool>=3.1.0",
    "redis>=5.0.0",
    "minio>=7.2.0",
    "httpx>=0.25.0",
    "pydantic>=2.0.0",
    "fastapi>=0.100.0",
]

[project.optional-dependencies]
# 가벼운 설치 (DB만 필요한 경우)
db = ["psycopg[binary]>=3.1.0", "psycopg-pool>=3.1.0"]
config = ["redis>=5.0.0"]
storage = ["minio>=7.2.0"]
auth = ["fastapi>=0.100.0"]
# 전체 설치
all = ["xgen-sdk[db,config,storage,auth]"]
```

### 5.2 설치 방식

**개발 환경** (로컬):
```bash
# 워크스페이스 내에서 editable install
pip install -e ../xgen-sdk

# 또는 pyproject.toml 의존성에 경로 지정
# dependencies = ["xgen-sdk @ file:///path/to/xgen-sdk"]
```

**Docker / K8s** (배포):
```dockerfile
# 빌드 시 SDK를 먼저 설치
COPY xgen-sdk/ /build/xgen-sdk/
RUN pip install /build/xgen-sdk/

# 그 다음 애플리케이션 설치
COPY xgen-core/ /app/
```

**향후**: Private PyPI 또는 Git URL로 배포 가능:
```
pip install git+https://github.com/x2bee/xgen-sdk.git@v1.0.0
```

---

## 6. 마이그레이션 계획 (단계별)

### Phase 1: SDK 프로젝트 생성 + DB 모듈 (Core)

**목표**: xgen-sdk 패키지 생성, DB 연결 코드를 SDK로 이동

1. `xgen-sdk/` 프로젝트 디렉토리 + pyproject.toml 생성
2. `xgen_sdk/db/` 모듈 작성:
   - `pool_manager.py` ← `database_manager_psycopg3.py` 이동
   - `app_db.py` ← `connection_psycopg3.py` 이동, 클래스명 `XgenDB` (+ `AppDatabaseManager` alias)
   - `base_model.py` ← `models/base_model.py` 이동
   - `config_serializer.py` 이동
   - `retry.py` ← `with_retry` 데코레이터 추출
3. xgen-core가 SDK의 DB 모듈을 import하도록 변경:
   ```python
   # Before: from service.database.connection_psycopg3 import AppDatabaseManager
   # After:  from xgen_sdk.db import XgenDB as AppDatabaseManager
   ```
4. xgen-core의 원본 파일은 SDK import로 redirect하는 shim으로 교체 (하위 호환):
   ```python
   # service/database/connection_psycopg3.py (shim)
   from xgen_sdk.db import XgenDB as AppDatabaseManager
   __all__ = ["AppDatabaseManager"]
   ```
5. 테스트: xgen-core가 기존과 동일하게 동작하는지 확인

### Phase 2: Config + Redis 모듈

1. `xgen_sdk/config/` 모듈 작성:
   - `redis_config.py` ← `redis_config_manager.py`
   - `local_config.py` ← `local_config_manager.py`
   - `config_factory.py` ← `create_config_manager()`
   - `config_utils.py` 이동
2. `xgen_sdk/redis/` 모듈 작성:
   - `client.py` ← 범용 Redis 클라이언트
3. xgen-core에서 SDK import로 전환 + shim 생성

### Phase 3: Storage + Auth 모듈

1. `xgen_sdk/storage/` 모듈 작성:
   - 3개 컨테이너의 minio_client.py 통합
2. `xgen_sdk/auth/` 모듈 작성:
   - 게이트웨이 헤더 파싱
   - 권한 매칭 엔진
   - 권한 상수 + 레지스트리
   - 권한 해석 (DB 조회)
   - 감독 범위 해석
   - `require_perm()` 데코레이터
3. xgen-core에서 SDK auth import로 전환

### Phase 4: xgen-workflow 마이그레이션

**핵심 변경**: HTTP proxy → Direct DB connection

1. `xgen-workflow/pyproject.toml`에 `xgen-sdk` 의존성 추가
2. `main.py` startup 변경:
   ```python
   # Before:
   # from service.database.database_client import DatabaseClient
   # app_db = DatabaseClient()  # HTTP to xgen-core

   # After:
   from xgen_sdk.db import XgenDB
   app_db = XgenDB()  # 직접 PostgreSQL 연결
   ```
3. `service/config/config_client.py` → `XgenConfig` 교체
4. `service/storage/minio_client.py` → `XgenStorage` 교체
5. `controller/helper/controllerHelper.py` → `xgen_sdk.auth` 교체
6. `service/database/database_client.py` 삭제
7. `service/config/config_client.py` 삭제
8. 기존 container init (app_container.py) 업데이트

### Phase 5: xgen-documents 마이그레이션

Phase 4와 동일한 패턴으로:
1. 의존성 추가
2. DatabaseClient → XgenDB
3. ConfigClient → XgenConfig
4. minio_client → XgenStorage
5. controllerHelper → xgen_sdk.auth
6. HTTP proxy 파일 삭제

### Phase 6: xgen-core 정리

1. `controller/database/databaseController.py` deprecated 마킹
2. `controller/database/configController.py` deprecated 마킹
3. xgen-core의 service/ 내 원본 파일들 → SDK shim 유지 (하위 호환)
4. 추후 버전에서 Data HTTP API 완전 제거

---

## 7. 각 컨테이너별 변경 요약

### xgen-core

| 영역 | 변경 | 상세 |
|------|------|------|
| `service/database/` | Shim으로 대체 | SDK import하는 1줄 파일 (하위 호환) |
| `service/database/models/` | **유지** | 모델 정의 + 마이그레이션은 xgen-core에서만 |
| `service/redis_client/` | Shim으로 대체 | SDK import |
| `service/storage/` | Shim으로 대체 | SDK import |
| `service/permission/` | Shim으로 대체 | SDK import |
| `controller/database/` | Deprecated 마킹 | 단계적 제거 |
| `controller/helper/` | SDK auth 사용 | 공통 부분만 교체 |
| `main.py` | import 경로 변경 | SDK에서 import |
| `pyproject.toml` | 의존성 추가 | `xgen-sdk` 추가, psycopg3 직접 의존 제거 |

### xgen-workflow

| 영역 | 변경 | 상세 |
|------|------|------|
| `service/database/` | **삭제** | HTTP proxy 전면 제거 |
| `service/config/` | **삭제** | HTTP proxy 전면 제거 |
| `service/storage/` | SDK로 교체 | minio_client → XgenStorage |
| `service/redis/` | SDK로 교체 | RedisClient → XgenRedis |
| `controller/helper/` | SDK auth 사용 | 권한 체크 통일 |
| `main.py` | 대폭 변경 | 직접 DB 연결 초기화 |
| `app_container.py` | Init step 수정 | DB/Config init step 변경 |
| `pyproject.toml` | 의존성 변경 | + xgen-sdk, - httpx 일부 |

### xgen-documents

| 영역 | 변경 | 상세 |
|------|------|------|
| `service/database/` | **삭제** | HTTP proxy 전면 제거 |
| `service/config/` | **삭제** | HTTP proxy 전면 제거 |
| `service/storage/minio_client.py` | SDK로 교체 | XgenStorage |
| `service/storage/storage_service.py` | **유지** | 문서 도메인 로직 (XgenStorage 위에 래핑) |
| `controller/helper/` | SDK auth 사용 | 권한 체크 추가 |
| `main.py` | 대폭 변경 | 직접 DB 연결 초기화 |
| `pyproject.toml` | 의존성 변경 | + xgen-sdk, - httpx 일부 |

---

## 8. DB 모델 접근 전략

### 문제
SDK에 DB 연결 로직은 있지만, 모델 정의(`User`, `Role`, `Permission` 등)는 xgen-core에만 존재. xgen-workflow에서 `db.find_by_condition(UserModel, ...)`을 호출하려면 UserModel 클래스가 필요.

### 해결책: 동적 테이블 접근

SDK의 XgenDB는 **두 가지 모드**를 지원:

#### 모드 1: 모델 기반 (xgen-core용)
```python
from service.database.models.user import User  # xgen-core 로컬 모델
records = db.find_by_condition(User, {"is_active": True})
```

#### 모드 2: 테이블명 직접 지정 (다른 컨테이너용)
```python
# 모델 없이 테이블명으로 직접 접근
records = db.query_table("users", conditions={"is_active": True}, limit=100)
# 또는
result = db.execute_raw_query("SELECT * FROM users WHERE is_active = %s", (True,))
```

**현재 xgen-workflow/documents의 DatabaseClient**도 이미 테이블명 기반으로 동작하므로 호환성 문제 없음.

---

## 9. 환경변수 통일

### 현재 (비일관적)

| 변수 | xgen-core | xgen-workflow | xgen-documents |
|------|-----------|---------------|----------------|
| DB 연결 | `DATABASE_*` (직접) | `CORE_SERVICE_BASE_URL` (HTTP) | `CORE_SERVICE_BASE_URL` (HTTP) |
| Redis | `REDIS_HOST/PORT/DB` | `REDIS_HOST/PASSWORD` | (없음) |
| MinIO | `MINIO_ENDPOINT/USER/PASSWORD` | `MINIO_ENDPOINT/USER/PASSWORD` | `MINIO_ENDPOINT/USER/PASSWORD` |

### 목표 (SDK 통일)

```bash
# 모든 컨테이너 동일
XGEN_DB_HOST=postgres
XGEN_DB_PORT=5432
XGEN_DB_NAME=xgen
XGEN_DB_USER=xgen
XGEN_DB_PASSWORD=...

XGEN_REDIS_HOST=redis
XGEN_REDIS_PORT=6379
XGEN_REDIS_PASSWORD=...
XGEN_REDIS_DB=0

XGEN_MINIO_ENDPOINT=minio:9000
XGEN_MINIO_ACCESS_KEY=minioadmin
XGEN_MINIO_SECRET_KEY=...
XGEN_MINIO_SECURE=false
```

**하위 호환**: SDK는 새 변수명 우선, 기존 변수명 fallback 지원.

---

## 10. 위험 요소 및 대응

| # | 위험 | 영향도 | 대응 |
|---|------|--------|------|
| 1 | DB 커넥션 수 증가 | 높음 | 3개 컨테이너가 각각 pool(min=2, max=10) → 최대 30 연결. PostgreSQL `max_connections` 확인 필요 (기본 100, pg_bouncer 도입 검토) |
| 2 | 모델 정의 동기화 | 중간 | 모델은 xgen-core에서만 정의 → 타 컨테이너는 테이블명 기반 접근으로 모델 의존 없음 |
| 3 | 마이그레이션 중 장애 | 중간 | Phase별 독립 배포 가능하도록 설계. HTTP proxy와 직접 연결 병행 기간 허용 |
| 4 | SDK 버전 불일치 | 낮음 | 시맨틱 버전 + `>=1.0.0,<2.0.0` 제약 사용 |
| 5 | 기존 테스트 깨짐 | 중간 | Shim 파일로 import 경로 호환 유지 |

---

## 11. 작업 우선순위 & 의존관계

```
Phase 1 (DB 모듈)
    ↓
Phase 2 (Config + Redis)     ← Phase 1에 의존 (config은 db_manager 참조)
    ↓
Phase 3 (Storage + Auth)     ← Phase 1, 2에 의존 (auth는 db 조회 필요)
    ↓
Phase 4 (xgen-workflow)      ← Phase 1, 2, 3 완료 필요
    ↓
Phase 5 (xgen-documents)     ← Phase 1, 2, 3 완료 필요 (Phase 4와 병렬 가능)
    ↓
Phase 6 (xgen-core 정리)     ← Phase 4, 5 완료 후
```

---

## 12. 성공 기준

- [ ] xgen-sdk를 pip install하면 DB/Config/Storage/Auth 기능 즉시 사용 가능
- [ ] xgen-workflow에서 `CORE_SERVICE_BASE_URL`로의 HTTP 호출 0건
- [ ] xgen-documents에서 `CORE_SERVICE_BASE_URL`로의 HTTP 호출 0건
- [ ] 3개 컨테이너 모두 동일한 `xgen-sdk` 버전 사용
- [ ] 권한 체크가 모든 컨테이너에서 통일된 방식으로 동작
- [ ] 기존 모든 API 엔드포인트 동일하게 작동 (기능 변경 없음)
