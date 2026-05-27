# xgen-sdk

**The shared platform SDK for the XGen ecosystem — one dependency, one boot call, everything wired up.**

[![PyPI](https://img.shields.io/pypi/v/xgen-sdk.svg)](https://pypi.org/project/xgen-sdk/)
[![Python](https://img.shields.io/pypi/pyversions/xgen-sdk.svg)](https://pypi.org/project/xgen-sdk/)
[![GitHub](https://img.shields.io/badge/source-PlateerLab%2Fxgen--sdk-181717?logo=github)](https://github.com/PlateerLab/xgen-sdk)

`xgen-sdk` is the common infrastructure layer that powers every Python service in
the XGen platform (`xgen-core`, `xgen-workflow`, `xgen-documents`, and friends).
It provides direct, pooled access to PostgreSQL, Redis-backed configuration,
MinIO object storage, an ABAC permission system, structured backend logging,
quota policy evaluation, in-app notifications, and a dynamic LLM model catalog
— all behind a single `XgenApp` bootstrap class.

Instead of every service re-implementing connection pools, permission headers,
config loaders, and S3 clients, `xgen-sdk` ships one canonical implementation
and lets each service consume it through a stable, versioned API.

---

## Why xgen-sdk?

Before the SDK, the XGen platform had the same problem most multi-service
Python stacks have:

- `DatabaseClient` was duplicated across services with subtle drift.
- `ConfigClient`, `minio_client`, and gateway-header parsers were all
  copy-pasted, then slowly diverged.
- Most service-to-service traffic went over **HTTP proxies**, paying ~50 ms
  of serialization + network overhead per database call.
- Permission checks were enforced inconsistently across services.

`xgen-sdk` consolidates all of this into one package that every service
installs as a dependency. Services connect directly to PostgreSQL, Redis,
and MinIO through pooled clients — no internal HTTP hops — and share a single
source of truth for auth, configuration, and observability.

> **Result:** ~10× faster DB calls (direct psycopg3 pool vs. HTTP proxy),
> zero code duplication for shared infrastructure, and one place to fix
> bugs or roll out improvements.

---

## Highlights

| Module | What it gives you |
|---|---|
| `xgen_sdk.db` | psycopg3 connection pool, model-based + table-name CRUD, raw SQL, retries |
| `xgen_sdk.config` | Redis-backed config with local-file fallback, typed `BaseConfig` registration |
| `xgen_sdk.storage` | MinIO client with upload/download, listing, copy, presigned URLs |
| `xgen_sdk.auth` | Gateway header parsing, ABAC permissions with wildcards, FastAPI `Depends()` guards |
| `xgen_sdk.redis` | General-purpose Redis client (sessions, caching, pub/sub) |
| `xgen_sdk.logging` | `BackendLogger` for structured, DB-persisted application logs |
| `xgen_sdk.quota` | Pure-Python quota policy specs and evaluation (no DB/HTTP) |
| `xgen_sdk.notification` | Generic per-user in-app notifications with read tracking |
| `xgen_sdk.llm_catalog` | Dynamic model list fetching for OpenAI / Anthropic / Gemini with TTL cache |
| `xgen_sdk.XgenApp` | One-call bootstrap that wires DB + Config + Storage together |

---

## Installation

```bash
pip install xgen-sdk
```

Requires **Python 3.11+**.

### Optional extras

Install only the parts you need:

```bash
pip install "xgen-sdk[db]"        # PostgreSQL only
pip install "xgen-sdk[config]"    # Redis config only
pip install "xgen-sdk[storage]"   # MinIO only
pip install "xgen-sdk[auth]"      # FastAPI auth helpers
pip install "xgen-sdk[all]"       # Everything (recommended for services)
```

---

## Quick start

The fastest way to use `xgen-sdk` inside a FastAPI service:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from xgen_sdk import XgenApp

xgen = XgenApp()

@asynccontextmanager
async def lifespan(app: FastAPI):
    xgen.boot()           # Connect DB, Config, Storage
    app.state.xgen = xgen
    yield
    xgen.shutdown()       # Cleanly release all resources

app = FastAPI(lifespan=lifespan)

@app.get("/users/{user_id}")
async def get_user(user_id: int):
    return xgen.db.find_by_id_table("users", user_id)
```

`XgenApp.boot()` reads its configuration from environment variables
(see [Environment](#environment-variables) below) and initializes:

1. **PostgreSQL** — psycopg3 connection pool via `xgen.db`
2. **Config** — `RedisConfigManager` (or local-file fallback) via `xgen.config`
3. **Storage** — MinIO client via `xgen.minio_client`

Each subsystem can be disabled independently:

```python
xgen = XgenApp(enable_db=True, enable_config=True, enable_storage=False)
```

---

## Modules

### Database — `xgen_sdk.db`

A psycopg3-based connection pool with both model-class and table-name APIs.

```python
from xgen_sdk.db import XgenDB, database_config

db = XgenDB(database_config)
db.initialize_connection()

# Model-based CRUD
record_id = db.insert(user_model)
records = db.find_by_condition(UserModel, {"status": "active"}, limit=100)

# Table-name CRUD (no model class required — great for cross-service callers)
db.insert_record("users", {"name": "Ada", "email": "ada@example.com"})
rows = db.find_records_by_condition("users", {"is_active": True})

# Raw SQL with parameter binding
rows = db.execute_raw_query(
    "SELECT id, name FROM users WHERE created_at > %s", (cutoff,)
)
```

### Configuration — `xgen_sdk.config`

A two-tier configuration system: Redis is the source of truth, with local-file
fallback when Redis is unavailable. Typed config is declared with `BaseConfig`
and assembled by `ConfigComposer`.

```python
from xgen_sdk.config import create_config_manager, get_config_value

config = create_config_manager(db_manager=db)
api_key = get_config_value("OPENAI_API_KEY")
```

### Storage — `xgen_sdk.storage`

A MinIO client that covers the full surface area used across XGen services:
upload, download, existence checks, recursive listing, copying, and
presigned URL generation.

```python
from xgen_sdk.storage import (
    upload_file, download_file, get_presigned_url, list_files_in_path,
)

upload_file("documents", "reports/q4.pdf", "/tmp/q4.pdf")
url = get_presigned_url("documents", "reports/q4.pdf", expires=3600)
```

### Auth — `xgen_sdk.auth`

ABAC-style permission system with FastAPI integration. Permissions support
wildcards (`admin.*:*`, `workflow:*`) and superusers bypass all checks.
The decorator pattern doubles as a self-registering permission catalog —
the act of declaring an endpoint is the act of declaring its permission.

```python
from fastapi import Depends, Request
from xgen_sdk.auth import require_perm, require_any_perm, require_superuser

@router.get("/roles")
async def list_roles(
    request: Request,
    session=Depends(require_perm("admin.role:read", description="List roles")),
):
    ...

@router.delete("/system/reset")
async def reset(_=Depends(require_superuser())):
    ...
```

At startup, call `validate_and_sync(db)` to push all decorator-declared
permissions into the database — the frontend then reads them back to build
its role-assignment UI. No more drift between code and DB.

### Logging — `xgen_sdk.logging`

A `BackendLogger` that writes structured logs to the application database,
tied to the current user and request.

```python
from xgen_sdk.logging import create_logger

log = create_logger(db, user_id=session["user_id"], request=request)
log.info("Workflow started", metadata={"workflow_id": wf_id})
log.success("Workflow finished")
log.error("Step failed", exception=exc)
```

### Quota — `xgen_sdk.quota`

Pure-Python policy evaluation for token quotas. No DB, no HTTP — just
dataclasses and functions, so it can be imported safely from any service.

```python
from xgen_sdk.quota import (
    resolve_effective_policies, evaluate_quota, period_bounds_kst,
)

effective = resolve_effective_policies(user_id, role_ids, policies)
decision = evaluate_quota(effective, usage_by_period)
if not decision.allowed:
    raise HTTPException(429, detail=decision.reason)
```

### Notifications — `xgen_sdk.notification`

Generic per-user, persistent, in-app notifications. Failures in the
notification path never break the caller's main flow.

```python
from xgen_sdk.notification import (
    NotificationPayload, NotificationCategory, NotificationSeverity, publish,
)

publish(db, NotificationPayload(
    user_id=123,
    category=NotificationCategory.QUOTA,
    severity=NotificationSeverity.ERROR,
    title="Token quota exceeded",
    body="You have exceeded your monthly limit of 1,000,000 tokens.",
    link="/mypage?tab=quota",
    metadata={"policy_id": 7, "used": 1234567, "limit": 1000000},
))
```

### LLM Catalog — `xgen_sdk.llm_catalog`

Fetches the live list of available chat / vision models from OpenAI,
Anthropic, and Gemini, with an in-process TTL cache and a hardcoded
fallback so UI dropdowns never go empty.

```python
from xgen_sdk.llm_catalog import get_models, invalidate

models = get_models(provider="openai", capability="chat")
# [{"id": "gpt-4o", "label": "GPT-4o", ...}, ...]

invalidate("openai")   # Call after rotating the API key
```

---

## Environment variables

`XgenApp.boot()` reads these from the environment. All have sensible defaults
for local development.

### PostgreSQL

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_HOST` | `localhost` | DB host |
| `POSTGRES_PORT` | `5432` | DB port |
| `POSTGRES_DB` | `xgen` | Database name |
| `POSTGRES_USER` | `postgres` | DB user |
| `POSTGRES_PASSWORD` | `postgres` | DB password |

### Redis (optional — falls back to local file)

| Variable | Default | Description |
|---|---|---|
| `REDIS_HOST` | `localhost` | Redis host |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_PASSWORD` | _(empty)_ | Redis password |

### MinIO

| Variable | Default | Description |
|---|---|---|
| `MINIO_ENDPOINT` | `http://minio:9000` | MinIO endpoint URL |
| `MINIO_ROOT_USER` | `minioadmin` | Access key |
| `MINIO_ROOT_PASSWORD` | `minioadmin` | Secret key |

---

## Architecture at a glance

```
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│   xgen-workflow  │   │  xgen-documents  │   │    xgen-core     │
│                  │   │                  │   │                  │
│   ┌──────────┐   │   │   ┌──────────┐   │   │   ┌──────────┐   │
│   │ xgen-sdk │   │   │   │ xgen-sdk │   │   │   │ xgen-sdk │   │
│   └────┬─────┘   │   │   └────┬─────┘   │   │   └────┬─────┘   │
└────────┼─────────┘   └────────┼─────────┘   └────────┼─────────┘
         │                      │                      │
         ▼                      ▼                      ▼
   ┌─────────────────────────────────────────────────────────┐
   │   PostgreSQL   │   Redis   │   MinIO   │   Gateway      │
   └─────────────────────────────────────────────────────────┘
```

Every service speaks **directly** to the shared infrastructure through the
same SDK code path. Schema definitions and migrations remain centralized
in `xgen-core`; the SDK provides the connection and CRUD primitives.

---

## Compatibility

- **Python:** 3.11, 3.12
- **PostgreSQL:** 13+
- **Redis:** 5+
- **MinIO:** any S3-API-compatible release

## Runtime dependencies

`psycopg[binary]`, `psycopg-pool`, `redis`, `minio`, `httpx`, `pydantic`,
`fastapi`.

---

## Versioning

`xgen-sdk` follows [Semantic Versioning](https://semver.org/). Pin services
with a major-version range to receive bug fixes and additive features
automatically:

```toml
dependencies = ["xgen-sdk>=1.14,<2.0"]
```

## Links

- **Source code:** https://github.com/PlateerLab/xgen-sdk
- **Issue tracker:** https://github.com/PlateerLab/xgen-sdk/issues
- **Releases / Changelog:** https://github.com/PlateerLab/xgen-sdk/releases

## License

Proprietary — internal use within the XGen platform, maintained by PlateerLab.
