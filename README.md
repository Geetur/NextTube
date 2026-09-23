# NextTube

Local-first, end-to-end video upload, transcode, and HLS playback pipeline.
Tech stack: Next.js (App Router), FastAPI, Python worker (FFmpeg), Postgres, Kafka, Redis, and MinIO, all wired with Docker Compose.

---

## What it does

- **Upload** a video from the browser
- **Queue** a transcode job (240p / 480p / 720p by default) via Apache Kafka
- **Transcode** to HLS through a Python worker using FFmpeg
- **Store** source and HLS assets in MinIO (S3-compatible)
- **Stream** via the API (proxy to MinIO) to avoid CORS issues
- **Watch** with an HLS player (`hls.js`) at `/watch/<video_id>`
- **Inspect** Kafka topics live at `localhost:8080` (kafka-ui)

---

## Architecture

```
Browser -> FastAPI -> MinIO: source/<id>.mp4
Browser -> POST /jobs/transcode -> jobs + outbox_events (one transaction)
Publisher -> Kafka: media.transcode.requested
Worker -> FFmpeg -> MinIO: HLS/<id>/...
Worker -> Postgres: renditions ready, job done
Browser -> FastAPI HLS proxy -> MinIO
```

---

## Services

| Service | Image | Port(s) | Purpose |
|---------|-------|---------|---------|
| postgres | postgres:16 | 5432 | Relational database |
| redis | redis:7-alpine | 6379 | Available for caching |
| minio | minio/minio | 9000, 9001 | Object storage (S3-compatible) |
| createbuckets | minio/mc | - | Creates the `media` bucket on first run |
| kafka | bitnamilegacy/kafka:3.7 | 9092 | Job queue (KRaft, no ZooKeeper) |
| kafka-init | bitnamilegacy/kafka:3.7 | - | Creates request and dead-letter topics |
| kafka-ui | provectuslabs/kafka-ui | 8080 | Topic & message inspector |
| api | FastAPI | 8000 | REST API + HLS proxy |
| publisher | Python | - | Transactional outbox to Kafka publisher |
| worker | Python | - | Kafka consumer + FFmpeg transcoder, scaled 1-3 times |
| autoscaler | Python | - | Kafka-lag controller for local worker replicas |
| web | Next.js 14 | 3000 | Browser frontend |

---

## Repo layout

```
infra/
  docker-compose.yml        # local service topology
apps/
  config.py                 # shared pydantic-settings configuration
  api/
    app/
      db.py                 # SQLAlchemy engine
      main.py               # FastAPI routes and outbox creation
      s3.py                 # MinIO helpers
    alembic/                # DB migrations
    requirements.txt
    Dockerfile
  publisher/
    db.py                   # outbox claims and acknowledgements
    main.py                 # Kafka publishing loop
  worker/
    consumer.py             # Kafka consumer and S3 I/O
    db.py                   # job claims, retries, and terminal state
    transcoder.py           # FFmpeg HLS encoding
    main.py                 # entry point (calls consumer.run)
    requirements.txt
  autoscaler/               # Local Kafka-lag worker controller
  web/
    app/
      watch/[id]/page.tsx   # HLS player with MP4 fallback
    package.json
```

---
## Quick start

Prereq: **Docker Desktop** running.

```bash
cp .env.example .env
docker compose -f infra/docker-compose.yml up --build -d
```

| URL | Description |
|-----|-------------|
| http://localhost:3000 | Web UI |
| http://localhost:8000/docs | FastAPI interactive docs |
| http://localhost:8000/healthz | Liveness probe |
| http://localhost:8080 | Kafka UI (topic / message inspector) |
| http://localhost:9001 | MinIO Console (user: minioadmin) |

Follow logs:

```bash
docker compose -f infra/docker-compose.yml logs -f api publisher worker autoscaler
```

---

## Happy path

1. Go to `http://localhost:3000`
2. Choose a video file â†’ click **Upload & Transcode**
3. You are redirected to `/watch/<video_id>`
4. The player fetches the master playlist from the API once renditions are ready

Or use the API directly:

```bash
# Upload
curl -F "file=@myvideo.mp4" http://localhost:8000/upload
# â†’ { "video_id": "...", "key": "source/<id>.mp4" }

# Queue a transcode job (stored transactionally, then published to Kafka)
curl -H "Content-Type: application/json" \
  -d '{"video_id":"<id>","profiles":[240,480,720]}' \
  http://localhost:8000/jobs/transcode

# Poll job status
curl http://localhost:8000/jobs/<job_id>

# Master HLS playlist (API proxies MinIO)
curl http://localhost:8000/videos/<id>/playlist
```

---

## API surface

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/healthz` | Liveness (db Â· redis) |
| POST | `/upload` | Upload source to MinIO, insert videos row |
| POST | `/jobs/transcode` | Create job and transactional outbox event |
| GET | `/jobs/{job_id}` | Job status |
| GET | `/videos` | List recent uploads |
| GET | `/videos/{id}/summary` | Source + renditions and statuses |
| GET | `/videos/{id}/playlist` | Master HLS playlist (proxy) |
| GET | `/videos/{id}/{path}` | Variant playlists and fMP4 segments (proxy) |
| GET | `/metrics-player` | Embedded player with QoE metrics |

---

## Environment

Copy `.env.example` to `.env`. Key variables:

```
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DB=media
DATABASE_URL=postgresql://postgres:postgres@postgres:5432/media

REDIS_URL=redis://redis:6379/0

KAFKA_BOOTSTRAP_SERVERS=kafka:9092
KAFKA_TOPIC=media.transcode.requested
KAFKA_DEAD_LETTER_TOPIC=media.transcode.dlq
WORKER_MAX_RETRIES=3
WORKER_RETRY_DELAYS_SECONDS=30,120,600
AUTOSCALER_MIN_WORKERS=1
AUTOSCALER_MAX_WORKERS=3

S3_ENDPOINT=http://minio:9000
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin
S3_BUCKET=media
```

---

## Delivery and local scaling

`POST /jobs/transcode` writes the job and its outbox event in one Postgres transaction. The publisher claims outbox rows and marks them delivered only after Kafka acknowledges them. Acknowledgement failures return events to the outbox, so delivery is at-least-once rather than exactly-once.

Workers disable Kafka auto-commit. They claim a leased job, transcode one video at a time, upload rendition files before the master playlist, persist the terminal job state, and only then commit the source offset. Duplicate deliveries are safe no-ops once a job is active or complete.

Transient failures retry after 30 seconds, 2 minutes, and 10 minutes by default. A failure after the third retry marks the job failed and publishes an event to `media.transcode.dlq`. Inspect queued terminal failures with Kafka UI or the dead-letter topic, then create a new transcode job after resolving the cause.

The local autoscaler converts request-topic consumer lag into one to three worker containers. Three is a hard local limit because `media.transcode.requested` has three partitions. It scales up after a short cooldown and only removes workers after zero lag for five minutes. The autoscaler has Docker socket access so it can clone the running Compose worker configuration; treat this as a trusted local-development service, not a production deployment pattern.

---

## Useful commands

```bash
# DB inspection
docker exec -it media_postgres psql -U postgres -d media \
  -c "SELECT id, status, type FROM jobs ORDER BY created_at DESC LIMIT 5"

docker exec -it media_postgres psql -U postgres -d media \
  -c "SELECT video_id, height, status FROM renditions ORDER BY video_id, height"

# Kafka: list topics
docker exec -it media_kafka kafka-topics.sh \
  --bootstrap-server localhost:9092 --list

# Kafka: tail messages in the transcode topic
docker exec -it media_kafka kafka-console-consumer.sh \
  --bootstrap-server localhost:9092 \
  --topic media.transcode.requested \
  --from-beginning
```

---

## Troubleshooting

**405 Method Not Allowed on playlists** â€” `curl -I` sends HEAD; use `curl -G` (GET).

**Worker not consuming jobs** - Check `docker compose -f infra/docker-compose.yml logs worker publisher`. Ensure `kafka-init` completed and both topics exist (`kafka-topics.sh --list`).

**Autoscaler cannot create workers** - Docker Desktop must be running and the Docker socket must be available to the `autoscaler` container. Verify it can see the initial Compose worker before creating backlog.

**CORS errors** â€” The API proxies MinIO, so the browser should only talk to `localhost:8000`. Confirm the web player uses `/videos/...` not the MinIO endpoint directly.

**Worker crash: invalid DSN** â€” The worker `config.py` normalizes `postgresql+psycopg2://` to `postgresql://` automatically via a Pydantic validator.

---

## Roadmap

- Signed MinIO URLs for protected content
- Thumbnail extraction and preview sprites
- Configurable encoding ladder (quality vs. speed presets)
- Swap MinIO for AWS S3 (env-only change, no code change needed)

---

## License

MIT
