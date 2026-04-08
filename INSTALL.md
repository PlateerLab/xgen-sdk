# xgen-sdk Installation

## Local Development (editable install)

```bash
# From any container directory (e.g. xgen-core/, xgen-workflow/, xgen-documents/)
pip install -e ../xgen-sdk[all]
```

## Docker Build

In each container's Dockerfile, add before `pip install`:

```dockerfile
# Copy SDK first (cacheable layer)
COPY xgen-sdk /tmp/xgen-sdk
RUN pip install /tmp/xgen-sdk[all]

# Then install the container's own deps
COPY xgen-core/pyproject.toml .
RUN pip install -e .
```

## Docker Compose (monorepo context)

```yaml
services:
  xgen-core:
    build:
      context: .  # monorepo root (new-source/)
      dockerfile: xgen-core/Dockerfile

  xgen-workflow:
    build:
      context: .
      dockerfile: xgen-workflow/Dockerfile

  xgen-documents:
    build:
      context: .
      dockerfile: xgen-documents/Dockerfile
```

## Required Environment Variables

All containers using xgen-sdk need these env vars:

### Database (PostgreSQL)
- `POSTGRES_HOST` (default: localhost)
- `POSTGRES_PORT` (default: 5432)
- `POSTGRES_DB` (default: xgen)
- `POSTGRES_USER` (default: postgres)
- `POSTGRES_PASSWORD` (default: postgres)

### Config (Redis) — optional, falls back to local file
- `REDIS_HOST` (default: localhost)
- `REDIS_PORT` (default: 6379)
- `REDIS_PASSWORD` (optional)

### Storage (MinIO)
- `MINIO_ENDPOINT` (default: http://minio:9000)
- `MINIO_ROOT_USER` (default: minioadmin)
- `MINIO_ROOT_PASSWORD` (default: minioadmin)
