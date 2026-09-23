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
- Kafka UI: http://localhost:8080
- `media.transcode.requested` and `media.transcode.dlq` exist

### Local worker scaling
The `autoscaler` service uses consumer lag from `media.transcode.requested` to maintain one to three workers. It has access to `/var/run/docker.sock` so it can create and remove worker containers using the running Compose worker as a template. Docker Desktop must be running, and this socket access is appropriate only for a trusted local environment.

The request topic has three partitions, so more than three workers cannot increase local transcode concurrency. Worker scale-down waits for an empty queue to avoid interrupting FFmpeg.

### Alembic
The API container automatically runs `alembic upgrade head` on start.
You can also exec in:
```bash
docker exec -it media_api bash
alembic current
```
