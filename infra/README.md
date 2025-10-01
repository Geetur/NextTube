# Day 1 — Walking Skeleton

## Prereqs
- Docker & Docker Compose

## Quickstart
```bash
cp .env.example .env
docker compose -f infra/docker-compose.yml up --build
```

### Verify
- API health: http://localhost:8000/healthz
- Web app: http://localhost:3000
- MinIO Console: http://localhost:9001 (minioadmin / minioadmin)
- S3 bucket created: `media`

### Alembic
The API container automatically runs `alembic upgrade head` on start.
You can also exec in:
```bash
docker exec -it media_api bash
alembic current
```
