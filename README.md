# xgen-sdk

**A general-purpose Python toolkit of powerful, reusable building blocks — the foundation the entire XGen platform is built on.**

[![PyPI](https://img.shields.io/pypi/v/xgen-sdk.svg)](https://pypi.org/project/xgen-sdk/)
[![Python](https://img.shields.io/pypi/pyversions/xgen-sdk.svg)](https://pypi.org/project/xgen-sdk/)
[![GitHub](https://img.shields.io/badge/source-PlateerLab%2Fxgen--sdk-181717?logo=github)](https://github.com/PlateerLab/xgen-sdk)

`xgen-sdk` is a curated library of **strong, general-purpose Python methods and
modules** — the kind of logic every serious backend, agent runtime, or RAG
pipeline ends up reinventing. Database pools, config management, object
storage, ABAC authorization, quota evaluation, structured logging, LLM model
discovery, in-app notifications — all consolidated into one canonical
implementation that any Python project can pick up and use.

The XGen platform itself (`xgen-core`, `xgen-workflow`, `xgen-documents`, …)
is essentially **a thin layer of business logic built on top of this SDK**.
The SDK is where the heavy lifting lives.

> One install. No optional extras. Everything you need, ready on `pip install xgen-sdk`.

---

## Philosophy

`xgen-sdk` follows three rules:

1. **Generalize relentlessly.** If a piece of logic is useful in more than one
   place, it belongs in the SDK — not duplicated in each service.
2. **Be opinionated where it matters, flexible where it doesn't.** Sensible
   defaults from environment variables, escape hatches for everything.
3. **Stay batteries-included.** No `[extras]` to remember, no optional
   dependency dance. `pip install xgen-sdk` and you have the full toolkit.

The platform services are intentionally small. `xgen-core` is admin and auth
business logic. `xgen-workflow` is workflow orchestration business logic.
`xgen-documents` is document-processing business logic. **All of the heavy
infrastructure code lives here.**

---

## What's in the box

| Module | Purpose |
|---|---|
| `xgen_sdk.db` | psycopg3 connection pool, model-based + table-name CRUD, raw SQL, retries |
| `xgen_sdk.config` | Redis-backed config with local-file fallback, typed `BaseConfig` registration |
| `xgen_sdk.storage` | MinIO / S3-compatible client — upload, download, listing, copy, presigned URLs |
| `xgen_sdk.auth` | Gateway header parsing, ABAC permissions with wildcards, FastAPI `Depends()` guards |
| `xgen_sdk.redis` | General-purpose Redis client for sessions, caching, and pub/sub |
| `xgen_sdk.logging` | `BackendLogger` for structured, DB-persisted application logs |
| `xgen_sdk.quota` | Pure-Python quota policy specs and evaluation (no DB / HTTP coupling) |
| `xgen_sdk.notification` | Generic per-user persistent in-app notifications with read tracking |
| `xgen_sdk.llm_catalog` | Dynamic model list for OpenAI / Anthropic / Gemini with TTL cache and fallback |
| `xgen_sdk.XgenApp` | One-call bootstrap that wires DB + Config + Storage together |

More general-purpose utilities are added with every release — tracing,
retry strategies, schema migration, agent and RAG primitives, prompt
templating, async task patterns. **If it's reusable, it belongs here.**

---

## Installation

```bash
pip install xgen-sdk
```

Requires **Python 3.11+**. That's it — every module is installed and ready
to import. No extras, no flags, no surprises.

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

You're not required to use `XgenApp` — every module is fully usable on its
own. `XgenApp` is the convenient default for "I want a FastAPI service with
the standard infrastructure stack."

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

# Table-name CRUD (no model class required)
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

## Where it fits

```
        ┌────────────────────────────────────────────────────┐
        │                  Your application                  │
        │   (xgen-core / xgen-workflow / xgen-documents /    │
        │    your own agent / RAG / API service)             │
        │                                                    │
        │              ──── business logic only ────         │
        └─────────────────────────┬──────────────────────────┘
                                  │
                                  ▼
        ┌────────────────────────────────────────────────────┐
        │                    xgen-sdk                        │
        │                                                    │
        │   db │ config │ storage │ auth │ redis │ logging   │
        │   quota │ notification │ llm_catalog │ XgenApp     │
        │              (+ more in every release)             │
        └─────────────────────────┬──────────────────────────┘
                                  │
                                  ▼
        ┌────────────────────────────────────────────────────┐
        │   PostgreSQL │ Redis │ MinIO │ Gateway │ LLM APIs  │
        └────────────────────────────────────────────────────┘
```

Every XGen service speaks **directly** to the underlying infrastructure
through the same SDK code path — no internal HTTP proxies, no duplicated
client code. The SDK is the only place those concerns live.

---

## Compatibility

- **Python:** 3.11, 3.12
- **PostgreSQL:** 13+
- **Redis:** 5+
- **MinIO:** any S3-API-compatible release

## Runtime dependencies

`psycopg[binary]`, `psycopg-pool`, `redis`, `minio`, `httpx`, `pydantic`,
`fastapi`. All installed automatically with `pip install xgen-sdk`.

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

Proprietary — maintained by PlateerLab as the foundation of the XGen platform.
